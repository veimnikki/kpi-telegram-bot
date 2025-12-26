import os
import json
import random
import asyncio
from datetime import datetime, timedelta

import pytz
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest

# =========================================================
# ENV + TIMEZONE
# =========================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_BOT_MODE = os.getenv("BOT_MODE", "friendly").lower().strip()

REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "10"))
# окно: minute-1 .. minute+1
REMINDER_MINUTE = int(os.getenv("REMINDER_MINUTE", "45"))

tz = pytz.timezone("Europe/Prague")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

if DEFAULT_BOT_MODE not in ("friendly", "official"):
    DEFAULT_BOT_MODE = "friendly"

# =========================================================
# GOOGLE SHEETS (ENV CREDS)
# =========================================================
creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not creds_json:
    raise RuntimeError("GOOGLE_CREDENTIALS_JSON not set")

creds_dict = json.loads(creds_json)

credentials = Credentials.from_service_account_info(
    creds_dict,
    scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ],
)

gc = gspread.authorize(credentials)
spreadsheet = gc.open("KPI_Plans")

records_sheet = spreadsheet.worksheet("Records")
users_sheet = spreadsheet.worksheet("Users")
admins_sheet = spreadsheet.worksheet("Admins")
chats_sheet = spreadsheet.worksheet("Chats")

# =========================================================
# TIME HELPERS
# =========================================================
def now_dt() -> datetime:
    return datetime.now(tz)

def today_str() -> str:
    return now_dt().strftime("%Y-%m-%d")

def now_time_str() -> str:
    return now_dt().strftime("%H:%M")

def is_weekend() -> bool:
    return now_dt().weekday() >= 5  # 5=Sat, 6=Sun

def last_workday_str() -> str:
    """
    Mon -> Fri (minus 3 days)
    Tue-Fri -> previous day
    Sat/Sun -> previous day fallback (we skip weekends anyway)
    """
    d = now_dt().date()
    wd = d.weekday()  # 0=Mon
    if wd == 0:
        d -= timedelta(days=3)
    elif wd == 6:
        d -= timedelta(days=2)
    elif wd == 5:
        d -= timedelta(days=1)
    else:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")

def pretty_ddmm(d_yyyy_mm_dd: str) -> str:
    return datetime.strptime(d_yyyy_mm_dd, "%Y-%m-%d").strftime("%d.%m")

# =========================================================
# SMALL UTILS
# =========================================================
def safe_int(x, default=0):
    try:
        s = str(x).strip()
        if s == "":
            return default
        return int(s)
    except Exception:
        return default

def norm_bool(x) -> bool:
    return str(x).strip().lower() in ("true", "1", "yes", "y", "да")

def normalize_thread_id(x) -> int:
    # в таблицах Thread ID может быть "" — считаем это 0
    return safe_int(x, default=0)

def headers_map(ws):
    row1 = ws.row_values(1)
    return {h.strip(): i + 1 for i, h in enumerate(row1) if h.strip()}

def get_col(ws, name: str) -> int:
    hm = headers_map(ws)
    if name not in hm:
        raise RuntimeError(f"Sheet '{ws.title}' missing required column '{name}'")
    return hm[name]

# =========================================================
# MESSAGES / MODES
# =========================================================
FRIENDLY = {
    "no_plan": [
        "☀️ Доброе утро\nУпс… не вижу план на сегодня 👀",
        "🌤️ План на сегодня пока прячется",
    ],
    "no_fact": [
        "👀 Кажется забыли факт за прошлый рабочий день",
        "🧾 Факт за прошлый рабочий день ещё не записан",
    ],
    "no_both": [
        "☀️ Доброе утро\nНе вижу ни плана ни факта 👀",
        "🧹 Нужно закрыть план и факт",
    ],
}

OFFICIAL = {
    "no_plan": ["План на сегодня отсутствует"],
    "no_fact": ["Факт за прошлый рабочий день не зафиксирован"],
    "no_both": ["Отсутствует план и факт"],
}

def pick_message(case: str, mode: str) -> str:
    src = FRIENDLY if mode == "friendly" else OFFICIAL
    return random.choice(src[case])

# =========================================================
# PRIVATE MODE TEXTS
# =========================================================
USER_HELP_TEXT = (
    "Привет 👋\n"
    "Я бот для планов и фактов.\n\n"
    "📌 ВАЖНО: команды нужно писать в *рабочем чате*, где добавлен бот.\n\n"
    "Команды:\n"
    "• /plan — план на день\n"
    "• /fact — факт за прошлый рабочий день\n"
    "• /vacation — отпуск\n\n"
    "⏰ Напоминания приходят автоматически по будням."
)

ADMIN_WELCOME_TEXT = (
    "Привет 👋 Это *админ-версия* бота.\n\n"
    "Функции:\n"
    "• 📊 Отчёты по чатам/веткам\n"
    "• 🏖 Переключение отпуска\n"
    "• 🙂 Переключение Mode (friendly/official)\n\n"
    "Выбери действие кнопкой 👇"
)

# показываем обычному пользователю 1 раз за запуск
SHOWN_HELP_PRIVATE = set()

# =========================================================
# ADMIN ACCESS (Admins sheet)
# Admins: Admin user ID | Username | Chat ID | Thread ID
# ChatID/ThreadID могут быть пустыми -> доступ ко всем чатам
# =========================================================
def admin_rows():
    return admins_sheet.get_all_records()

def is_admin_user(user_id: int) -> bool:
    for r in admin_rows():
        if safe_int(r.get("Admin user ID")) == int(user_id):
            return True
    return False

def admin_scopes(user_id: int):
    """
    returns list of (chat_id, thread_id) allowed for this admin.
    if admin has empty chat_id => special flag 'ALL'
    """
    scopes = []
    for r in admin_rows():
        if safe_int(r.get("Admin user ID")) != int(user_id):
            continue
        cid = safe_int(r.get("Chat ID"), default=0)
        tid = normalize_thread_id(r.get("Thread ID"))
        if cid == 0:
            return ["ALL"]
        scopes.append((cid, tid))
    return scopes

# =========================================================
# CHATS REGISTRY (Chats sheet)
# Chats: Chat ID | Thread ID | Chat title | Team | Active
# One row per (chat_id, thread_id)
# =========================================================
def upsert_chat(chat, thread_id: int):
    if not chat or chat.type == "private":
        return

    thread_id = int(thread_id or 0)
    hm = headers_map(chats_sheet)

    col_chat_id = hm.get("Chat ID", 1)
    col_thread_id = hm.get("Thread ID", 2)
    col_title = hm.get("Chat title", 3)
    col_team = hm.get("Team", 4)
    col_active = hm.get("Active", 5)

    rows = chats_sheet.get_all_records()
    for idx, r in enumerate(rows, start=2):
        if safe_int(r.get("Chat ID")) == int(chat.id) and normalize_thread_id(r.get("Thread ID")) == thread_id:
            # update title only, keep team/active as user-managed
            chats_sheet.update_cell(idx, col_title, chat.title or "")
            return

    # add new
    chats_sheet.append_row([
        chat.id,
        thread_id if thread_id != 0 else "",
        chat.title or "",
        "",       # Team
        "TRUE",   # Active
    ])

def chat_is_active(chats_records, chat_id: int, thread_id: int) -> bool:
    for r in chats_records:
        if safe_int(r.get("Chat ID")) == int(chat_id) and normalize_thread_id(r.get("Thread ID")) == int(thread_id):
            return norm_bool(r.get("Active", "TRUE"))
    return True  # если строки нет — считаем активным

def chat_team_name(chats_records, chat_id: int, thread_id: int) -> str:
    for r in chats_records:
        if safe_int(r.get("Chat ID")) == int(chat_id) and normalize_thread_id(r.get("Thread ID")) == int(thread_id):
            return str(r.get("Team", "")).strip()
    return ""

# =========================================================
# USERS UPSERT (Users sheet)
# Users: User ID | Username | Full name | Chat ID | Thread ID | Team | Active | Mode
# One row per (user_id, chat_id, thread_id) — это важно для веток
# =========================================================
def upsert_user_binding(user, chat, thread_id: int):
    if not user or not chat:
        return

    thread_id = int(thread_id or 0)

    hm = headers_map(users_sheet)
    col_uid = hm.get("User ID", 1)
    col_username = hm.get("Username", 2)
    col_fullname = hm.get("Full name", 3)
    col_chatid = hm.get("Chat ID", 4)
    col_thread = hm.get("Thread ID", 5)
    col_team = hm.get("Team", 6)
    col_active = hm.get("Active", 7)
    col_mode = hm.get("Mode", 8)  # важно: mode в конце

    rows = users_sheet.get_all_records()
    for idx, r in enumerate(rows, start=2):
        if (
            safe_int(r.get("User ID")) == int(user.id)
            and safe_int(r.get("Chat ID")) == int(chat.id)
            and normalize_thread_id(r.get("Thread ID")) == thread_id
        ):
            users_sheet.update_cell(idx, col_username, user.username or "")
            users_sheet.update_cell(idx, col_fullname, f"{user.first_name or ''} {user.last_name or ''}".strip())
            # Team не трогаем, Active не трогаем, Mode не трогаем
            return

    # new binding row
    users_sheet.append_row([
        user.id,
        user.username or "",
        f"{user.first_name or ''} {user.last_name or ''}".strip(),
        chat.id,
        thread_id if thread_id != 0 else "",
        "",                 # Team
        "TRUE",             # Active
        DEFAULT_BOT_MODE,   # Mode default
    ])

def resolve_user_mode(users_records, user_id: int, chat_id: int, thread_id: int) -> str:
    """
    Mode берём из Users для конкретной связки (user_id, chat_id, thread_id)
    Если нет — DEFAULT_BOT_MODE
    """
    for r in users_records:
        if (
            safe_int(r.get("User ID")) == int(user_id)
            and safe_int(r.get("Chat ID")) == int(chat_id)
            and normalize_thread_id(r.get("Thread ID")) == int(thread_id)
        ):
            m = str(r.get("Mode", "")).strip().lower()
            if m in ("friendly", "official"):
                return m
    return DEFAULT_BOT_MODE

# =========================================================
# RECORDS HELPERS (Records sheet)
# Records: Date | Chat ID | Chat title | User ID | Username | Plan | Plan time | Fact | Fact time | Vacation | Thread ID (если есть)
# We считаем, что "Thread ID" колонка есть. Если нет — код тоже переживёт (будет 0).
# =========================================================
def find_record(records, date_, user_id, chat_id, thread_id: int):
    thread_id = int(thread_id or 0)
    for idx, r in enumerate(records, start=2):
        if (
            str(r.get("Date")) == str(date_)
            and safe_int(r.get("User ID")) == int(user_id)
            and safe_int(r.get("Chat ID")) == int(chat_id)
            and normalize_thread_id(r.get("Thread ID")) == thread_id
        ):
            return idx, r
    return None, None

def ensure_record_today(chat, user, thread_id: int):
    thread_id = int(thread_id or 0)
    records = records_sheet.get_all_records()
    idx, _ = find_record(records, today_str(), user.id, chat.id, thread_id)
    if idx:
        return idx

    hm = headers_map(records_sheet)

    # Собираем строку по ожидаемому набору.
    # Если у тебя в Records реально 11 колонок (с Thread ID) — это идеально.
    row = [
        today_str(),
        chat.id,
        chat.title or "",
        user.id,
        user.username or "",
        "",  # Plan
        "",  # Plan time
        "",  # Fact
        "",  # Fact time
        "active",  # Vacation
    ]

    if "Thread ID" in hm:
        row.append(thread_id if thread_id != 0 else "")

    records_sheet.append_row(row)
    return len(records_sheet.get_all_values())

def has_plan_today(records, user_id, chat_id, thread_id):
    _, r = find_record(records, today_str(), user_id, chat_id, thread_id)
    return bool(r and str(r.get("Plan", "")).strip())

def has_fact_last_workday(records, user_id, chat_id, thread_id):
    _, r = find_record(records, last_workday_str(), user_id, chat_id, thread_id)
    return bool(r and str(r.get("Fact", "")).strip())

def in_vacation_today(records, user_id, chat_id, thread_id):
    _, r = find_record(records, today_str(), user_id, chat_id, thread_id)
    return bool(r and str(r.get("Vacation", "")).strip().lower() == "vacation")

# =========================================================
# GROUP ACTIVITY CATCHER
# - upsert chat (chat+thread)
# - upsert user binding (user+chat+thread)
# =========================================================
async def catch_group_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return
    if chat.type == "private":
        return

    thread_id = normalize_thread_id(getattr(msg, "message_thread_id", 0))

    try:
        upsert_chat(chat, thread_id=thread_id)
    except Exception as e:
        print("⚠️ upsert_chat error:", e)

    try:
        upsert_user_binding(user, chat, thread_id=thread_id)
    except Exception as e:
        print("⚠️ upsert_user_binding error:", e)

# =========================================================
# /PLAN (GROUP ONLY)
# =========================================================
async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user
    if not chat or chat.type == "private":
        return

    text = (msg.text or "").replace("/plan", "", 1).strip()
    if not text:
        await msg.reply_text("❗️План не может быть пустым")
        return

    thread_id = normalize_thread_id(getattr(msg, "message_thread_id", 0))
    idx = ensure_record_today(chat, user, thread_id=thread_id)

    hm = headers_map(records_sheet)
    records_sheet.update_cell(idx, hm["Plan"], text)
    records_sheet.update_cell(idx, hm["Plan time"], now_time_str())

    try:
        await context.bot.set_message_reaction(chat.id, msg.message_id, "👍")
    except Exception:
        pass

# =========================================================
# /FACT (GROUP ONLY)
# =========================================================
async def fact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user
    if not chat or chat.type == "private":
        return

    text = (msg.text or "").replace("/fact", "", 1).strip()
    if not text:
        await msg.reply_text("❗️Факт не может быть пустым")
        return

    thread_id = normalize_thread_id(getattr(msg, "message_thread_id", 0))

    records = records_sheet.get_all_records()
    d = last_workday_str()

    hm = headers_map(records_sheet)
    fact_col = hm["Fact"]
    fact_time_col = hm["Fact time"]

    idx, row = find_record(records, d, user.id, chat.id, thread_id)

    if row and not str(row.get("Fact", "")).strip():
        records_sheet.update_cell(idx, fact_col, text)
        records_sheet.update_cell(idx, fact_time_col, now_time_str())
    else:
        t_idx = ensure_record_today(chat, user, thread_id=thread_id)
        records_sheet.update_cell(t_idx, fact_col, text)
        records_sheet.update_cell(t_idx, fact_time_col, now_time_str())

    try:
        await context.bot.set_message_reaction(chat.id, msg.message_id, "👍")
    except Exception:
        pass

# =========================================================
# /VACATION (GROUP ONLY)
# =========================================================
async def vacation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat or chat.type == "private":
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌴 Уйти в отпуск", callback_data="vac:on")],
        [InlineKeyboardButton("🧑‍💼 Выйти из отпуска", callback_data="vac:off")],
    ])
    await update.effective_message.reply_text("Статус отпуска:", reply_markup=kb)

async def vacation_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    msg = q.message
    chat = msg.chat if msg else None
    user = q.from_user
    if not chat or chat.type == "private":
        await q.answer("Эта кнопка работает только в рабочих чатах")
        return

    thread_id = normalize_thread_id(getattr(msg, "message_thread_id", 0))
    idx = ensure_record_today(chat, user, thread_id=thread_id)

    hm = headers_map(records_sheet)
    vac_col = hm["Vacation"]

    status = "vacation" if q.data == "vac:on" else "active"
    records_sheet.update_cell(idx, vac_col, status)
    await q.answer("Готово ✅")

# =========================================================
# PRIVATE ENTRY / START
# - non-admin: show help once then ignore
# - admin: show admin menu
# =========================================================
def admin_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Отчет", callback_data="admin:report")],
        [InlineKeyboardButton("ℹ️ Помощь", callback_data="admin:help")],
    ])

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type != "private" or not user:
        return

    if is_admin_user(user.id):
        await update.effective_message.reply_text(
            ADMIN_WELCOME_TEXT,
            reply_markup=admin_main_keyboard(),
            parse_mode="Markdown",
        )
        return

    await update.effective_message.reply_text(USER_HELP_TEXT, parse_mode="Markdown")
    SHOWN_HELP_PRIVATE.add(user.id)

async def private_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type != "private" or not user:
        return

    if is_admin_user(user.id):
        # админ-панель: можно отвечать и всегда показывать меню
        await update.effective_message.reply_text(
            ADMIN_WELCOME_TEXT,
            reply_markup=admin_main_keyboard(),
            parse_mode="Markdown",
        )
        return

    # обычный пользователь: показать один раз и дальше молча игнорировать
    if user.id in SHOWN_HELP_PRIVATE:
        return
    SHOWN_HELP_PRIVATE.add(user.id)
    await update.effective_message.reply_text(USER_HELP_TEXT, parse_mode="Markdown")

# =========================================================
# ADMIN PANEL (PRIVATE ONLY)
# =========================================================
def scope_label(chats_records, cid: int, tid: int) -> str:
    title = ""
    team = ""
    for r in chats_records:
        if safe_int(r.get("Chat ID")) == int(cid) and normalize_thread_id(r.get("Thread ID")) == int(tid):
            title = str(r.get("Chat title", "")).strip()
            team = str(r.get("Team", "")).strip()
            break
    base = title or str(cid)
    if tid:
        base = f"{base} (thread {tid})"
    if team:
        base = f"{base} — {team}"
    return base

async def admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    user = q.from_user
    chat = q.message.chat if q.message else None

    if not user or not chat or chat.type != "private":
        await q.answer("Только в личке")
        return
    if not is_admin_user(user.id):
        await q.answer("Нет доступа")
        return

    data = q.data or ""

    if data == "admin:help":
        await q.message.reply_text(
            ADMIN_WELCOME_TEXT,
            reply_markup=admin_main_keyboard(),
            parse_mode="Markdown",
        )
        await q.answer()
        return

    if data == "admin:report":
        chats_records = chats_sheet.get_all_records()
        if not chats_records:
            await q.message.reply_text(
                "Во вкладке Chats пока пусто.\n"
                "👉 Напиши любое сообщение в рабочем чате/ветке, где есть бот — и оно появится."
            )
            await q.answer()
            return

        scopes = admin_scopes(user.id)
        if scopes != ["ALL"]:
            allowed = set(scopes)
            chats_records = [r for r in chats_records
                             if (safe_int(r.get("Chat ID")), normalize_thread_id(r.get("Thread ID"))) in allowed]

        if not chats_records:
            await q.message.reply_text(
                "У тебя нет привязанных чатов/веток в Admins.\n"
                "Добавь строки в Admins: Admin user ID | Username | Chat ID | Thread ID"
            )
            await q.answer()
            return

        buttons = []
        # показываем до 40 кнопок, иначе Телеграм начнёт капризничать
        for r in chats_records[:40]:
            cid = safe_int(r.get("Chat ID"))
            tid = normalize_thread_id(r.get("Thread ID"))
            label = scope_label(chats_records, cid, tid)
            buttons.append([InlineKeyboardButton(f"📌 {label}", callback_data=f"admin:chat:{cid}:{tid}")])

        await q.message.reply_text("Выбери чат/ветку для отчёта:", reply_markup=InlineKeyboardMarkup(buttons))
        await q.answer()
        return

    if data.startswith("admin:chat:"):
        _, _, cid_s, tid_s = data.split(":", 3)
        cid = safe_int(cid_s)
        tid = safe_int(tid_s)

        # safety: respect admin scope
        scopes = admin_scopes(user.id)
        if scopes != ["ALL"] and (cid, tid) not in set(scopes):
            await q.answer("Нет доступа к этому чату")
            return

        users_records = users_sheet.get_all_records()
        chats_records = chats_sheet.get_all_records()
        records = records_sheet.get_all_records()

        # чат должен быть активен, иначе отчёт можно показывать, но напоминания не идут
        chat_active = chat_is_active(chats_records, cid, tid)

        # пользователи этого чата+ветки
        team_users = [
            u for u in users_records
            if safe_int(u.get("Chat ID")) == cid
            and normalize_thread_id(u.get("Thread ID")) == tid
            and norm_bool(u.get("Active", "TRUE"))
        ]

        if not team_users:
            await q.message.reply_text(
                "Не вижу пользователей с этим Chat ID + Thread ID во вкладке Users.\n"
                "👉 Нужно, чтобы сотрудники написали *любое* сообщение в этой ветке/чате."
            )
            await q.answer()
            return

        lw = last_workday_str()

        vac_count = 0
        plan_ok = 0
        fact_ok = 0
        missing_plan = []
        missing_fact = []

        for u in team_users:
            uid = safe_int(u.get("User ID"))
            if in_vacation_today(records, uid, cid, tid):
                vac_count += 1
                continue

            if has_plan_today(records, uid, cid, tid):
                plan_ok += 1
            else:
                missing_plan.append(u)

            if has_fact_last_workday(records, uid, cid, tid):
                fact_ok += 1
            else:
                missing_fact.append(u)

        active_non_vac = max(len(team_users) - vac_count, 0)

        title = scope_label(chats_records, cid, tid)
        status_line = "✅ Активен" if chat_active else "⛔️ Выключен (Active=FALSE в Chats)"

        text = (
            f"📊 Отчет: *{title}*\n"
            f"Статус напоминаний: {status_line}\n"
            f"Дата: *{today_str()}*\n\n"
            f"👥 Активных (без отпуска): *{active_non_vac}*\n"
            f"🏖 В отпуске сегодня: *{vac_count}*\n\n"
            f"✅ План есть: *{plan_ok}/{active_non_vac}*\n"
            f"✅ Факт есть (за {pretty_ddmm(lw)}): *{fact_ok}/{active_non_vac}*\n"
        )

        buttons = []

        # Vacation toggle for top 8 users (чтобы клавиатура не стала гигантской)
        candidates = (missing_plan + missing_fact)[:8]
        for u in candidates:
            uid = safe_int(u.get("User ID"))
            name = (u.get("Full name") or u.get("Username") or str(uid)).strip()
            buttons.append([
                InlineKeyboardButton(f"🏖 {name} → отпуск", callback_data=f"admin:vac_on:{cid}:{tid}:{uid}"),
                InlineKeyboardButton(f"🧑‍💼 {name} → работа", callback_data=f"admin:vac_off:{cid}:{tid}:{uid}"),
            ])

        # Mode menu
        buttons.append([InlineKeyboardButton("🙂 Mode: изменить для команды", callback_data=f"admin:mode_team:{cid}:{tid}")])
        buttons.append([InlineKeyboardButton("🙂 Mode: изменить для сотрудника", callback_data=f"admin:mode_user_pick:{cid}:{tid}")])
        buttons.append([InlineKeyboardButton("⬅️ Назад к чатам", callback_data="admin:report")])

        await q.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
        await q.answer()
        return

    # Toggle vacation
    if data.startswith("admin:vac_on:") or data.startswith("admin:vac_off:"):
        parts = data.split(":")
        action = parts[1]        # vac_on / vac_off
        cid = safe_int(parts[2])
        tid = safe_int(parts[3])
        uid = safe_int(parts[4])

        scopes = admin_scopes(user.id)
        if scopes != ["ALL"] and (cid, tid) not in set(scopes):
            await q.answer("Нет доступа")
            return

        status = "vacation" if action == "vac_on" else "active"

        # find/create today's record for that user in that chat/thread
        records = records_sheet.get_all_records()
        idx, row = find_record(records, today_str(), uid, cid, tid)

        hm = headers_map(records_sheet)
        vac_col = hm["Vacation"]

        if idx:
            records_sheet.update_cell(idx, vac_col, status)
        else:
            # create minimal row for today
            chats_records = chats_sheet.get_all_records()
            title = ""
            for r in chats_records:
                if safe_int(r.get("Chat ID")) == cid and normalize_thread_id(r.get("Thread ID")) == tid:
                    title = str(r.get("Chat title", "")).strip()
                    break

            # username from Users
            users_records = users_sheet.get_all_records()
            username = ""
            for u in users_records:
                if safe_int(u.get("User ID")) == uid and safe_int(u.get("Chat ID")) == cid and normalize_thread_id(u.get("Thread ID")) == tid:
                    username = str(u.get("Username", "")).strip()
                    break

            row_to_add = [
                today_str(), cid, title, uid, username,
                "", "", "", "", status
            ]
            if "Thread ID" in hm:
                row_to_add.append(tid if tid != 0 else "")
            records_sheet.append_row(row_to_add)

        await q.answer("Готово ✅")
        return

    # Mode team menu
    if data.startswith("admin:mode_team:"):
        _, _, cid_s, tid_s = data.split(":", 3)
        cid = safe_int(cid_s)
        tid = safe_int(tid_s)

        scopes = admin_scopes(user.id)
        if scopes != ["ALL"] and (cid, tid) not in set(scopes):
            await q.answer("Нет доступа")
            return

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🙂 friendly (для команды)", callback_data=f"admin:set_mode_team:{cid}:{tid}:friendly")],
            [InlineKeyboardButton("📎 official (для команды)", callback_data=f"admin:set_mode_team:{cid}:{tid}:official")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:chat:{cid}:{tid}")],
        ])
        await q.message.reply_text("Выбери Mode для *всей команды*:", reply_markup=kb, parse_mode="Markdown")
        await q.answer()
        return

    if data.startswith("admin:set_mode_team:"):
        _, _, cid_s, tid_s, mode = data.split(":", 4)
        cid = safe_int(cid_s)
        tid = safe_int(tid_s)
        mode = mode.strip().lower()
        if mode not in ("friendly", "official"):
            await q.answer("Некорректный mode")
            return

        scopes = admin_scopes(user.id)
        if scopes != ["ALL"] and (cid, tid) not in set(scopes):
            await q.answer("Нет доступа")
            return

        # update all Users rows for this chat/thread
        hm = headers_map(users_sheet)
        col_mode = hm.get("Mode")
        if not col_mode:
            await q.answer("Нет колонки Mode в Users")
            return

        rows = users_sheet.get_all_records()
        changed = 0
        for idx, r in enumerate(rows, start=2):
            if safe_int(r.get("Chat ID")) == cid and normalize_thread_id(r.get("Thread ID")) == tid:
                users_sheet.update_cell(idx, col_mode, mode)
                changed += 1

        await q.answer(f"Готово ✅ ({changed})")
        return

    # Mode user pick list
    if data.startswith("admin:mode_user_pick:"):
        _, _, cid_s, tid_s = data.split(":", 3)
        cid = safe_int(cid_s)
        tid = safe_int(tid_s)

        scopes = admin_scopes(user.id)
        if scopes != ["ALL"] and (cid, tid) not in set(scopes):
            await q.answer("Нет доступа")
            return

        users_records = users_sheet.get_all_records()
        team_users = [
            u for u in users_records
            if safe_int(u.get("Chat ID")) == cid
            and normalize_thread_id(u.get("Thread ID")) == tid
            and norm_bool(u.get("Active", "TRUE"))
        ]

        if not team_users:
            await q.message.reply_text("Сначала пусть сотрудники что-то напишут в этой ветке — чтобы попали в Users.")
            await q.answer()
            return

        buttons = []
        for u in team_users[:25]:
            uid = safe_int(u.get("User ID"))
            name = (u.get("Full name") or u.get("Username") or str(uid)).strip()
            buttons.append([InlineKeyboardButton(f"👤 {name}", callback_data=f"admin:mode_user:{cid}:{tid}:{uid}")])

        buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:chat:{cid}:{tid}")])
        await q.message.reply_text("Выбери сотрудника:", reply_markup=InlineKeyboardMarkup(buttons))
        await q.answer()
        return

    if data.startswith("admin:mode_user:"):
        _, _, cid_s, tid_s, uid_s = data.split(":", 4)
        cid = safe_int(cid_s)
        tid = safe_int(tid_s)
        uid = safe_int(uid_s)

        scopes = admin_scopes(user.id)
        if scopes != ["ALL"] and (cid, tid) not in set(scopes):
            await q.answer("Нет доступа")
            return

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🙂 friendly", callback_data=f"admin:set_mode_user:{cid}:{tid}:{uid}:friendly")],
            [InlineKeyboardButton("📎 official", callback_data=f"admin:set_mode_user:{cid}:{tid}:{uid}:official")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:mode_user_pick:{cid}:{tid}")],
        ])
        await q.message.reply_text("Выбери Mode для сотрудника:", reply_markup=kb)
        await q.answer()
        return

    if data.startswith("admin:set_mode_user:"):
        _, _, cid_s, tid_s, uid_s, mode = data.split(":", 5)
        cid = safe_int(cid_s)
        tid = safe_int(tid_s)
        uid = safe_int(uid_s)
        mode = mode.strip().lower()
        if mode not in ("friendly", "official"):
            await q.answer("Некорректный mode")
            return

        scopes = admin_scopes(user.id)
        if scopes != ["ALL"] and (cid, tid) not in set(scopes):
            await q.answer("Нет доступа")
            return

        hm = headers_map(users_sheet)
        col_mode = hm.get("Mode")
        if not col_mode:
            await q.answer("Нет колонки Mode")
            return

        rows = users_sheet.get_all_records()
        for idx, r in enumerate(rows, start=2):
            if (
                safe_int(r.get("User ID")) == uid
                and safe_int(r.get("Chat ID")) == cid
                and normalize_thread_id(r.get("Thread ID")) == tid
            ):
                users_sheet.update_cell(idx, col_mode, mode)
                await q.answer("Готово ✅")
                return

        await q.answer("Пользователь не найден в Users")
        return

    await q.answer("Неизвестная команда")

# =========================================================
# REMINDERS LOOP
# - окно 10:44–10:46 (если REMINDER_MINUTE=45)
# - учитываем Chat Active из Chats
# - учитываем User Active из Users
# - учитываем Vacation из Records (сегодня)
# - учитываем Mode из Users
# - отправляем в chat_id + message_thread_id (если tid != 0)
# =========================================================
def in_reminder_window(n: datetime) -> bool:
    if n.hour != REMINDER_HOUR:
        return False
    return (REMINDER_MINUTE - 1) <= n.minute <= (REMINDER_MINUTE + 1)

async def reminder_loop(app):
    last_run_day = None

    while True:
        try:
            if is_weekend():
                await asyncio.sleep(60)
                continue

            n = now_dt()
            if in_reminder_window(n):
                today = today_str()
                if last_run_day != today:
                    last_run_day = today

                    users_records = users_sheet.get_all_records()
                    chats_records = chats_sheet.get_all_records()
                    records = records_sheet.get_all_records()

                    lw = last_workday_str()

                    for u in users_records:
                        uid = safe_int(u.get("User ID"))
                        if not uid:
                            continue
                        if not norm_bool(u.get("Active", "TRUE")):
                            continue

                        cid = safe_int(u.get("Chat ID"))
                        tid = normalize_thread_id(u.get("Thread ID"))

                        if not cid:
                            continue

                        # чат/ветка выключены?
                        if not chat_is_active(chats_records, cid, tid):
                            continue

                        # отпуск сегодня?
                        if in_vacation_today(records, uid, cid, tid):
                            continue

                        plan_ok = has_plan_today(records, uid, cid, tid)
                        fact_ok = has_fact_last_workday(records, uid, cid, tid)

                        if plan_ok and fact_ok:
                            continue

                        case = "no_both" if (not plan_ok and not fact_ok) else ("no_plan" if not plan_ok else "no_fact")
                        mode = resolve_user_mode(users_records, uid, cid, tid)

                        lines = [pick_message(case, mode)]
                        if not fact_ok:
                            lines.append(f"Факт нужен за {pretty_ddmm(lw)}")
                        if not plan_ok:
                            lines.append("План нужен за сегодня")

                        kwargs = {"chat_id": cid, "text": "\n".join(lines)}
                        if tid:
                            kwargs["message_thread_id"] = tid

                        # сети иногда падают -> ловим, но цикл живёт
                        try:
                            await app.bot.send_message(**kwargs)
                        except Exception as e:
                            print("⚠️ send_message error:", e)

            await asyncio.sleep(20)

        except Exception as e:
            print("⚠️ reminder error:", e)
            await asyncio.sleep(30)

# =========================================================
# START
# =========================================================
async def post_init(app):
    asyncio.create_task(reminder_loop(app))

def main():
    # важное: таймауты/сеть. ConnectTimeout бывает из-за сети/хостинга.
    request = HTTPXRequest(
        connect_timeout=20.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=20.0,
    )

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(request)
        .post_init(post_init)
        .build()
    )

    # SERVICE: ловим любую активность в группах/ветках, чтобы заполнить Chats/Users
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.ChatType.PRIVATE, catch_group_activity),
        group=0
    )

    # COMMANDS (work chats only)
    app.add_handler(CommandHandler("plan", plan), group=1)
    app.add_handler(CommandHandler("fact", fact), group=1)
    app.add_handler(CommandHandler("vacation", vacation), group=1)

    # CALLBACKS
    app.add_handler(CallbackQueryHandler(vacation_cb, pattern=r"^vac:(on|off)$"))
    app.add_handler(CallbackQueryHandler(admin_cb, pattern=r"^admin:"))

    # PRIVATE
    app.add_handler(CommandHandler("start", start_cmd), group=90)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE, private_entry), group=100)

    app.run_polling()

if __name__ == "__main__":
    main()
