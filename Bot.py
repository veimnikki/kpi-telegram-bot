# =========================================================
# BOT-REPORT — SAFE PRODUCTION VERSION (Railway-proof)
# =========================================================

import os
import json
import random
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional, Any

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
REMINDER_MINUTE = int(os.getenv("REMINDER_MINUTE", "45"))

tz = pytz.timezone("Europe/Prague")

if not BOT_TOKEN:
    print("⚠️ BOT_TOKEN not set")

if DEFAULT_BOT_MODE not in ("friendly", "official"):
    DEFAULT_BOT_MODE = "friendly"

# =========================================================
# GOOGLE SHEETS (SAFE INIT)
# =========================================================
creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not creds_json:
    print("⚠️ GOOGLE_CREDENTIALS_JSON not set")

credentials = None
gc = None
spreadsheet = None

try:
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
except Exception as e:
    print("❌ Google Sheets init failed:", e)

# =========================================================
# SAFE WORKSHEET GETTER
# =========================================================
def get_ws(name: str):
    try:
        return spreadsheet.worksheet(name)
    except Exception:
        try:
            return spreadsheet.add_worksheet(title=name, rows=1000, cols=30)
        except Exception as e:
            print(f"❌ Cannot access sheet {name}:", e)
            return None

records_sheet = get_ws("Records")
users_sheet = get_ws("Users")
admins_sheet = get_ws("Admins")
chats_sheet = get_ws("Chats")

# =========================================================
# TIME HELPERS
# =========================================================
def now_dt():
    return datetime.now(tz)

def today_str():
    return now_dt().strftime("%Y-%m-%d")

def now_time_str():
    return now_dt().strftime("%H:%M")

def is_weekend():
    return now_dt().weekday() >= 5

def last_workday_str():
    d = now_dt().date()
    wd = d.weekday()
    if wd == 0:
        d -= timedelta(days=3)
    elif wd == 6:
        d -= timedelta(days=2)
    elif wd == 5:
        d -= timedelta(days=1)
    else:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")

def pretty_ddmm(s):
    return datetime.strptime(s, "%Y-%m-%d").strftime("%d.%m")

# =========================================================
# SAFE SHEET UTILS
# =========================================================
def ws_values(ws):
    try:
        return ws.get_all_values() or []
    except Exception:
        return []

def ws_headers(ws):
    values = ws_values(ws)
    return [h.strip() for h in values[0]] if values else []

def headers_map(ws):
    hm = {}
    for i, h in enumerate(ws_headers(ws), start=1):
        if h and h not in hm:
            hm[h] = i
    return hm

def safe_table(ws):
    values = ws_values(ws)
    if not values:
        return [], []
    headers = values[0]
    rows = []
    for r in values[1:]:
        row = {}
        for i, h in enumerate(headers):
            if h:
                row[h] = r[i] if i < len(r) else ""
        rows.append(row)
    return headers, rows

def update_cell(ws, r, c, v):
    try:
        ws.update_cell(r, c, "" if v is None else str(v))
    except Exception as e:
        print("⚠️ update_cell:", e)

def append_row(ws, row):
    try:
        ws.append_row([("" if v is None else v) for v in row])
    except Exception as e:
        print("⚠️ append_row:", e)

# =========================================================
# SELF-HEALING COLUMNS (NO CRASH EVER)
# =========================================================
def ensure_columns_safe(ws, required: List[str]):
    if not ws:
        return
    headers = ws_headers(ws)

    # empty sheet → create headers
    if not headers:
        ws.append_row(required)
        return

    missing = [c for c in required if c not in headers]
    if missing:
        for col in missing:
            ws.update_cell(1, len(headers) + 1, col)
            headers.append(col)

# =========================================================
# REQUIRED COLUMNS (FINAL)
# =========================================================
USERS_REQUIRED = [
    "User ID", "Username", "Name",
    "Chat ID", "Chat name",
    "Thread ID", "Thread name",
    "Team", "Active", "Mode"
]

CHATS_REQUIRED = [
    "Chat ID", "Chat name",
    "Thread ID", "Thread name",
    "Team", "Active"
]

ADMINS_REQUIRED = [
    "Admin user ID", "Username", "Chat ID", "Thread ID"
]

RECORDS_REQUIRED = [
    "Date",
    "Chat ID", "Chat name",
    "Thread ID", "Thread name",
    "User ID", "Username",
    "Plan", "Plan time",
    "Fact", "Fact time",
    "Vacation"
]

for ws, cols in [
    (users_sheet, USERS_REQUIRED),
    (chats_sheet, CHATS_REQUIRED),
    (admins_sheet, ADMINS_REQUIRED),
    (records_sheet, RECORDS_REQUIRED),
]:
    ensure_columns_safe(ws, cols)

# =========================================================
# NORMALIZERS
# =========================================================
def safe_int(x, d=0):
    try:
        return int(str(x).strip())
    except Exception:
        return d

def norm_bool(x):
    return str(x).strip().lower() in ("true", "1", "yes", "y", "да")

def norm_tid(x):
    return safe_int(x, 0)

# =========================================================
# RECORDS (UNIQUE KEY GUARANTEED)
# =========================================================
def find_record_idx(date_, uid, cid, tid):
    _, rows = safe_table(records_sheet)
    for i, r in enumerate(rows, start=2):
        if (
            r.get("Date") == date_
            and safe_int(r.get("User ID")) == uid
            and safe_int(r.get("Chat ID")) == cid
            and norm_tid(r.get("Thread ID")) == tid
        ):
            return i
    return None

def ensure_record(date_, chat, user, tid):
    idx = find_record_idx(date_, user.id, chat.id, tid)
    hm = headers_map(records_sheet)

    if idx:
        update_cell(records_sheet, idx, hm["Chat name"], chat.title or "")
        return idx

    append_row(records_sheet, [
        date_,
        chat.id,
        chat.title or "",
        tid if tid else "",
        "",
        user.id,
        user.username or "",
        "", "",
        "", "",
        "active",
    ])
    return len(ws_values(records_sheet))

# =========================================================
# (ДАЛЕЕ ЛОГИКА /plan /fact /vacation /admin /reminder)
# =========================================================
# =========================================================
# MODES / MESSAGES
# =========================================================
FRIENDLY = {
    "no_plan": [
        "☀️ Доброе утро!\nУпс… не вижу план на сегодня 👀",
        "🌤️ План на сегодня пока прячется 🙈",
    ],
    "no_fact": [
        "👀 Кажется забыли факт за прошлый рабочий день",
        "🧾 Факт за прошлый рабочий день ещё не записан",
    ],
    "no_both": [
        "☀️ Доброе утро!\nНе вижу ни плана, ни факта 👀",
        "🧹 Нужно закрыть план и факт 🙂",
    ],
}

OFFICIAL = {
    "no_plan": ["План на сегодня отсутствует."],
    "no_fact": ["Факт за прошлый рабочий день не зафиксирован."],
    "no_both": ["Отсутствует план и факт."],
}

def pick_message(case: str, mode: str) -> str:
    src = FRIENDLY if mode == "friendly" else OFFICIAL
    return random.choice(src.get(case, [""]))

# =========================================================
# PRIVATE TEXTS
# =========================================================
USER_HELP_TEXT = (
    "Привет 👋\n"
    "Я бот для планов и фактов.\n\n"
    "📌 ВАЖНО: команды нужно писать в *рабочем чате/ветке*, где добавлен бот.\n\n"
    "Команды:\n"
    "• /plan — план на день (также активирует тебя в системе)\n"
    "• /fact — факт за прошлый рабочий день\n"
    "• /vacation — отпуск\n\n"
    "⏰ Напоминания приходят автоматически по будням."
)

ADMIN_WELCOME_TEXT = (
    "Привет 👋 Это *админ-версия*.\n\n"
    "Функции:\n"
    "• 📊 Отчёты по чатам/веткам\n"
    "• 🏖 Переключение отпуска сотрудникам\n"
    "• 🙂 Mode (friendly/official) на команду/сотрудника\n\n"
    "Выбери действие кнопкой 👇"
)

SHOWN_HELP_PRIVATE = set()

# =========================================================
# ADMIN ACCESS (Admins sheet) — FINAL ARCH:
# Admin user ID | Username | Chat ID | Thread ID
# - empty Chat ID => ALL
# - Chat ID set, Thread ID empty/0 => whole chat (all threads)
# - Chat ID set, Thread ID set => only that thread
# =========================================================
def admin_rows() -> List[Dict[str, str]]:
    _, rows = safe_table(admins_sheet)
    return rows

def is_admin_user(user_id: int) -> bool:
    for r in admin_rows():
        if safe_int(r.get("Admin user ID")) == int(user_id):
            return True
    return False

def admin_scopes(user_id: int):
    """
    Returns:
      - ["ALL"]  if any row has empty Chat ID for this admin
      - list of tuples (chat_id, thread_id) where thread_id may be 0 meaning "all threads in chat"
    """
    scopes: List[Tuple[int, int]] = []
    for r in admin_rows():
        if safe_int(r.get("Admin user ID")) != int(user_id):
            continue

        cid = safe_int(r.get("Chat ID"), 0)
        tid = norm_tid(r.get("Thread ID"))

        if cid == 0:
            return ["ALL"]

        # if thread empty/0 => whole chat
        scopes.append((cid, tid if tid else 0))

    # unique
    out: List[Tuple[int, int]] = []
    seen = set()
    for s in scopes:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out

def admin_scope_allows(admin_id: int, cid: int, tid: int) -> bool:
    scopes = admin_scopes(admin_id)
    if scopes == ["ALL"]:
        return True
    for scid, stid in scopes:
        if scid != int(cid):
            continue
        if stid == 0:
            return True  # whole chat
        if int(stid) == int(tid):
            return True
    return False

def admin_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Отчет", callback_data="admin:report")],
        [InlineKeyboardButton("ℹ️ Помощь", callback_data="admin:help")],
    ])

# =========================================================
# CHATS REGISTRY (Chats sheet)
# One row = one thread (Thread ID 0/empty for no threads)
# =========================================================
def upsert_chat(chat, thread_id: int):
    if not chat or chat.type == "private" or not chats_sheet:
        return

    hm = headers_map(chats_sheet)
    _, rows = safe_table(chats_sheet)

    for idx, r in enumerate(rows, start=2):
        if safe_int(r.get("Chat ID")) == int(chat.id) and norm_tid(r.get("Thread ID")) == int(thread_id):
            if "Chat name" in hm:
                update_cell(chats_sheet, idx, hm["Chat name"], chat.title or "")
            return

    append_row(chats_sheet, [
        chat.id,
        chat.title or "",
        thread_id if thread_id != 0 else "",
        "",       # Thread name (optional manual)
        "",       # Team
        "TRUE",   # Active
    ])

def chat_is_active(chats_rows: List[Dict[str, str]], chat_id: int, thread_id: int) -> bool:
    for r in chats_rows:
        if safe_int(r.get("Chat ID")) == int(chat_id) and norm_tid(r.get("Thread ID")) == int(thread_id):
            return norm_bool(r.get("Active", "TRUE"))
    return True

def thread_display_name(chats_rows: List[Dict[str, str]], chat_id: int, thread_id: int) -> str:
    if int(thread_id) == 0:
        return "Чат (без веток)"
    tname = ""
    for r in chats_rows:
        if safe_int(r.get("Chat ID")) == int(chat_id) and norm_tid(r.get("Thread ID")) == int(thread_id):
            tname = str(r.get("Thread name", "")).strip()
            break
    return tname if tname else f"thread {int(thread_id)}"

def chat_label(chats_rows: List[Dict[str, str]], chat_id: int, thread_id: int) -> str:
    chat_name = ""
    team = ""
    for r in chats_rows:
        if safe_int(r.get("Chat ID")) == int(chat_id) and norm_tid(r.get("Thread ID")) == int(thread_id):
            chat_name = str(r.get("Chat name", "")).strip()
            team = str(r.get("Team", "")).strip()
            break
    base = chat_name or str(chat_id)
    if thread_id:
        base = f"{base} / {thread_display_name(chats_rows, chat_id, thread_id)}"
    if team:
        base = f"{base} — {team}"
    return base

def chat_label_chat_only(chats_rows: List[Dict[str, str]], chat_id: int) -> str:
    chat_name = ""
    teams = set()
    has_threads = False
    for r in chats_rows:
        if safe_int(r.get("Chat ID")) != int(chat_id):
            continue
        if not chat_name:
            chat_name = str(r.get("Chat name", "")).strip()
        t = str(r.get("Team", "")).strip()
        if t:
            teams.add(t)
        if norm_tid(r.get("Thread ID")) != 0:
            has_threads = True
    base = chat_name or str(chat_id)
    if teams:
        base = f"{base} — {list(teams)[0]}" if len(teams) == 1 else f"{base} — multi-team"
    if has_threads:
        base = f"{base} (threads)"
    return base

def chat_has_threads(chats_rows: List[Dict[str, str]], chat_id: int) -> bool:
    for r in chats_rows:
        if safe_int(r.get("Chat ID")) == int(chat_id) and norm_tid(r.get("Thread ID")) != 0:
            return True
    return False

def chat_threads_for_chat(chats_rows: List[Dict[str, str]], chat_id: int) -> List[int]:
    tids = set()
    for r in chats_rows:
        if safe_int(r.get("Chat ID")) != int(chat_id):
            continue
        tid = norm_tid(r.get("Thread ID"))
        if tid != 0:
            tids.add(tid)
    return sorted(tids)

# =========================================================
# USERS (ONLY on /plan)
# One row = one user in one chat+thread
# =========================================================
def upsert_user_binding(user, chat, thread_id: int):
    if not user or not chat or not users_sheet:
        return

    hm = headers_map(users_sheet)
    _, rows = safe_table(users_sheet)

    uid = int(user.id)
    cid = int(chat.id)
    tid = int(thread_id or 0)

    username = user.username or ""
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()

    for idx, r in enumerate(rows, start=2):
        if safe_int(r.get("User ID")) == uid and safe_int(r.get("Chat ID")) == cid and norm_tid(r.get("Thread ID")) == tid:
            if "Username" in hm: update_cell(users_sheet, idx, hm["Username"], username)
            if "Name" in hm: update_cell(users_sheet, idx, hm["Name"], name)
            if "Chat name" in hm: update_cell(users_sheet, idx, hm["Chat name"], chat.title or "")
            if not str(r.get("Active", "")).strip() and "Active" in hm:
                update_cell(users_sheet, idx, hm["Active"], "TRUE")
            if not str(r.get("Mode", "")).strip() and "Mode" in hm:
                update_cell(users_sheet, idx, hm["Mode"], DEFAULT_BOT_MODE)
            return

    append_row(users_sheet, [
        uid,
        username,
        name,
        cid,
        chat.title or "",
        tid if tid != 0 else "",
        "",      # Thread name
        "",      # Team
        "TRUE",  # Active
        DEFAULT_BOT_MODE,  # Mode
    ])

def resolve_user_mode(users_rows: List[Dict[str, str]], user_id: int, chat_id: int, thread_id: int) -> str:
    for r in users_rows:
        if safe_int(r.get("User ID")) == int(user_id) and safe_int(r.get("Chat ID")) == int(chat_id) and norm_tid(r.get("Thread ID")) == int(thread_id):
            m = str(r.get("Mode", "")).strip().lower()
            if m in ("friendly", "official"):
                return m
    return DEFAULT_BOT_MODE

# =========================================================
# RECORD FIELDS
# =========================================================
def get_record_field(date_: str, user_id: int, chat_id: int, thread_id: int, field: str) -> str:
    _, rows = safe_table(records_sheet)
    for r in rows:
        if (
            str(r.get("Date")) == str(date_)
            and safe_int(r.get("User ID")) == int(user_id)
            and safe_int(r.get("Chat ID")) == int(chat_id)
            and norm_tid(r.get("Thread ID")) == int(thread_id)
        ):
            return str(r.get(field, "") or "").strip()
    return ""

def has_plan_today(uid, cid, tid) -> bool:
    return bool(get_record_field(today_str(), uid, cid, tid, "Plan"))

def has_fact_last_workday(uid, cid, tid) -> bool:
    return bool(get_record_field(last_workday_str(), uid, cid, tid, "Fact"))

def in_vacation_today(uid, cid, tid) -> bool:
    v = get_record_field(today_str(), uid, cid, tid, "Vacation").lower()
    return v == "vacation"

# =========================================================
# GROUP ACTIVITY CATCHER
# =========================================================
async def catch_group_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or chat.type == "private":
        return
    tid = norm_tid(getattr(msg, "message_thread_id", 0))
    try:
        upsert_chat(chat, tid)
    except Exception as e:
        print("⚠️ upsert_chat error:", e)

# =========================================================
# /PLAN
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

    tid = norm_tid(getattr(msg, "message_thread_id", 0))

    try:
        upsert_chat(chat, tid)
    except Exception as e:
        print("⚠️ upsert_chat in /plan error:", e)

    try:
        upsert_user_binding(user, chat, tid)
    except Exception as e:
        print("⚠️ upsert_user_binding in /plan error:", e)
        await msg.reply_text("⚠️ Не смог записать тебя в Users. Проверь заголовки Users.")
        return

    idx = ensure_record(today_str(), chat, user, tid)
    hm = headers_map(records_sheet)
    if "Plan" in hm: update_cell(records_sheet, idx, hm["Plan"], text)
    if "Plan time" in hm: update_cell(records_sheet, idx, hm["Plan time"], now_time_str())

    try:
        await context.bot.set_message_reaction(chat.id, msg.message_id, "👍")
    except Exception:
        pass

# =========================================================
# /FACT
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

    tid = norm_tid(getattr(msg, "message_thread_id", 0))
    d = last_workday_str()

    idx = ensure_record(d, chat, user, tid)
    hm = headers_map(records_sheet)
    if "Fact" in hm: update_cell(records_sheet, idx, hm["Fact"], text)
    if "Fact time" in hm: update_cell(records_sheet, idx, hm["Fact time"], now_time_str())

    try:
        await context.bot.set_message_reaction(chat.id, msg.message_id, "👍")
    except Exception:
        pass

# =========================================================
# /VACATION
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

    tid = norm_tid(getattr(msg, "message_thread_id", 0))

    idx = ensure_record(today_str(), chat, user, tid)
    hm = headers_map(records_sheet)

    if q.data == "vac:on":
        if "Vacation" in hm: update_cell(records_sheet, idx, hm["Vacation"], "vacation")
        text_after = "🌴 Хорошего отдыха! Отпуск включён ✅"
    else:
        if "Vacation" in hm: update_cell(records_sheet, idx, hm["Vacation"], "active")
        text_after = "🧑‍💼 Добро пожаловать обратно в строй! Работа включена ✅"

    try:
        await q.message.edit_text(text_after, reply_markup=None)
    except Exception as e:
        print("⚠️ edit_message error:", e)
        try:
            await q.message.reply_text(text_after)
        except Exception:
            pass

    await q.answer("Готово ✅")

# =========================================================
# PRIVATE /START ENTRY
# =========================================================
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
        await update.effective_message.reply_text(
            ADMIN_WELCOME_TEXT,
            reply_markup=admin_main_keyboard(),
            parse_mode="Markdown",
        )
        return

    if user.id in SHOWN_HELP_PRIVATE:
        return
    SHOWN_HELP_PRIVATE.add(user.id)
    await update.effective_message.reply_text(USER_HELP_TEXT, parse_mode="Markdown")

# =========================================================
# ADMIN PANEL HELPERS (CHAT+THREAD aware)
# =========================================================
def admin_get_chats_for_admin(admin_id: int) -> List[int]:
    _, chats_rows = safe_table(chats_sheet)
    scopes = admin_scopes(admin_id)
    if scopes == ["ALL"]:
        cids = [safe_int(r.get("Chat ID")) for r in chats_rows if safe_int(r.get("Chat ID"))]
        return sorted(list(set(cids)))

    # from scopes, return the chat ids that exist in Chats table
    allowed_cids = sorted(list(set([cid for cid, _ in scopes])))
    return allowed_cids

def build_chat_buttons(admin_id: int) -> InlineKeyboardMarkup:
    _, chats_rows = safe_table(chats_sheet)
    chat_ids = admin_get_chats_for_admin(admin_id)

    buttons = []
    for cid in chat_ids[:40]:
        label = chat_label_chat_only(chats_rows, cid)
        buttons.append([InlineKeyboardButton(f"📌 {label}", callback_data=f"admin:chatpick:{cid}")])

    if not buttons:
        buttons = [[InlineKeyboardButton("ℹ️ Нет чатов", callback_data="admin:help")]]

    return InlineKeyboardMarkup(buttons)

def build_thread_buttons_for_chat(admin_id: int, cid: int) -> InlineKeyboardMarkup:
    _, chats_rows = safe_table(chats_sheet)

    tids = chat_threads_for_chat(chats_rows, cid)
    buttons: List[List[InlineKeyboardButton]] = []

    buttons.append([InlineKeyboardButton("📌 Все ветки (общий отчёт)", callback_data=f"admin:thread_all:{cid}")])

    for tid in tids[:45]:
        tlabel = thread_display_name(chats_rows, cid, tid)
        buttons.append([InlineKeyboardButton(f"🧵 {tlabel}", callback_data=f"admin:thread:{cid}:{tid}")])

    buttons.append([InlineKeyboardButton("⬅️ Назад к чатам", callback_data="admin:report")])
    return InlineKeyboardMarkup(buttons)

def team_users_for_thread(cid: int, tid: int) -> List[Dict[str, str]]:
    _, users_rows = safe_table(users_sheet)
    return [
        u for u in users_rows
        if safe_int(u.get("Chat ID")) == cid
        and norm_tid(u.get("Thread ID")) == tid
        and norm_bool(u.get("Active", "TRUE"))
    ]
# =========================================================
# ADMIN CALLBACK (REPORTS + VACATION + MODE)
# =========================================================
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

    # HELP
    if data == "admin:help":
        await q.message.reply_text(ADMIN_WELCOME_TEXT, reply_markup=admin_main_keyboard(), parse_mode="Markdown")
        await q.answer()
        return

    # REPORT LIST (CHAT PICK)
    if data == "admin:report":
        kb = build_chat_buttons(user.id)
        await q.message.reply_text("Выбери чат для отчёта:", reply_markup=kb)
        await q.answer()
        return

    # pick chat -> show thread picker if has threads, else direct report tid=0
    if data.startswith("admin:chatpick:"):
        try:
            _, _, cid_s = data.split(":", 2)
            cid = safe_int(cid_s)
        except Exception:
            await q.answer("Bad data")
            return

        # chat-level scope check: allow if admin has that chat in scope (any thread) or ALL
        if not admin_scope_allows(user.id, cid, 0):
            await q.answer("Нет доступа к этому чату")
            return

        _, chats_rows = safe_table(chats_sheet)
        if chat_has_threads(chats_rows, cid):
            kb = build_thread_buttons_for_chat(user.id, cid)
            await q.message.reply_text("В этом чате есть ветки. Выбери ветку или общий отчёт:", reply_markup=kb)
            await q.answer()
            return

        # no threads -> direct report for tid=0
        await q.answer()
        data = f"admin:thread:{cid}:0"

    # ALL THREADS REPORT (chat summary across threads)
    if data.startswith("admin:thread_all:"):
        try:
            _, _, cid_s = data.split(":", 2)
            cid = safe_int(cid_s)
        except Exception:
            await q.answer("Bad data")
            return

        if not admin_scope_allows(user.id, cid, 0):
            await q.answer("Нет доступа к этому чату")
            return

        _, chats_rows = safe_table(chats_sheet)
        _, records_rows = safe_table(records_sheet)
        lw = last_workday_str()

        tids = chat_threads_for_chat(chats_rows, cid)
        if not tids:
            tids = [0]

        per_thread_lines = []
        total_active_non_vac = 0
        total_vac = 0
        total_plan_ok = 0
        total_fact_ok = 0

        for tid in tids:
            # scope: if admin limited to a specific thread, hide others
            if not admin_scope_allows(user.id, cid, tid):
                continue

            team_users = team_users_for_thread(cid, tid)
            if not team_users:
                continue

            vac_count = 0
            plan_ok = 0
            fact_ok = 0

            for urow in team_users:
                uid = safe_int(urow.get("User ID"))
                if in_vacation_today(uid, cid, tid):
                    vac_count += 1
                    continue
                if has_plan_today(uid, cid, tid):
                    plan_ok += 1
                if has_fact_last_workday(uid, cid, tid):
                    fact_ok += 1

            active_non_vac = max(len(team_users) - vac_count, 0)

            total_active_non_vac += active_non_vac
            total_vac += vac_count
            total_plan_ok += plan_ok
            total_fact_ok += fact_ok

            label = chat_label(chats_rows, cid, tid)
            per_thread_lines.append(
                f"• {label}: план *{plan_ok}/{active_non_vac}*, факт *{fact_ok}/{active_non_vac}*, отпуск *{vac_count}*"
            )

        title_chat = chat_label_chat_only(chats_rows, cid)
        text = (
            f"📊 Отчет (все ветки): *{title_chat}*\n"
            f"Дата/время: *{today_str()} {now_time_str()}*\n\n"
            f"👥 Активных (без отпуска): *{total_active_non_vac}*\n"
            f"🏖 В отпуске сегодня: *{total_vac}*\n\n"
            f"✅ План есть: *{total_plan_ok}/{total_active_non_vac}*\n"
            f"✅ Факт есть (за {pretty_ddmm(lw)}): *{total_fact_ok}/{total_active_non_vac}*\n\n"
            f"*Разбивка по веткам:*\n" + ("\n".join(per_thread_lines) if per_thread_lines else "—")
        )

        buttons: List[List[InlineKeyboardButton]] = []

        # quick navigation to each allowed thread report
        for tid in tids[:10]:
            if not admin_scope_allows(user.id, cid, tid):
                continue
            tlabel = thread_display_name(chats_rows, cid, tid)
            buttons.append([InlineKeyboardButton(f"🧵 {tlabel}", callback_data=f"admin:thread:{cid}:{tid}")])

        buttons.append([InlineKeyboardButton("🙂 Mode (команда) — выбрать ветку", callback_data=f"admin:mode_team_pickchat:{cid}")])
        buttons.append([InlineKeyboardButton("🙂 Mode (сотрудник) — выбрать ветку", callback_data=f"admin:mode_user_pickchat:{cid}")])
        buttons.append([InlineKeyboardButton("⬅️ Назад к веткам", callback_data=f"admin:chatpick:{cid}")])
        buttons.append([InlineKeyboardButton("⬅️ Назад к чатам", callback_data="admin:report")])

        await q.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
        await q.answer()
        return

    # THREAD REPORT (single thread)
    if data.startswith("admin:thread:"):
        try:
            _, _, cid_s, tid_s = data.split(":", 3)
            cid = safe_int(cid_s)
            tid = safe_int(tid_s)
        except Exception:
            await q.answer("Bad data")
            return

        if not admin_scope_allows(user.id, cid, tid):
            await q.answer("Нет доступа к этой ветке")
            return

        _, chats_rows = safe_table(chats_sheet)
        _, records_rows = safe_table(records_sheet)

        chat_active = chat_is_active(chats_rows, cid, tid)
        lw = last_workday_str()

        team_users = team_users_for_thread(cid, tid)
        if not team_users:
            await q.message.reply_text(
                "В этой ветке пока нет активированных сотрудников.\n"
                "👉 Сотрудник появится в Users только после команды /plan в этой ветке."
            )
            await q.answer()
            return

        vac_count = 0
        plan_ok = 0
        fact_ok = 0
        missing_plan = []
        missing_fact = []

        for urow in team_users:
            uid = safe_int(urow.get("User ID"))
            if in_vacation_today(uid, cid, tid):
                vac_count += 1
                continue

            if has_plan_today(uid, cid, tid):
                plan_ok += 1
            else:
                missing_plan.append(urow)

            if has_fact_last_workday(uid, cid, tid):
                fact_ok += 1
            else:
                missing_fact.append(urow)

        active_non_vac = max(len(team_users) - vac_count, 0)

        title = chat_label(chats_rows, cid, tid)
        status_line = "✅ Активен" if chat_active else "⛔️ Выключен (Active=FALSE в Chats)"

        text = (
            f"📊 Отчет: *{title}*\n"
            f"Дата/время: *{today_str()} {now_time_str()}*\n"
            f"Статус напоминаний: {status_line}\n\n"
            f"👥 Активных (без отпуска): *{active_non_vac}*\n"
            f"🏖 В отпуске сегодня: *{vac_count}*\n\n"
            f"✅ План есть: *{plan_ok}/{active_non_vac}*\n"
            f"✅ Факт есть (за {pretty_ddmm(lw)}): *{fact_ok}/{active_non_vac}*\n"
        )

        buttons = []

        candidates = (missing_plan + missing_fact)
        uniq = []
        seen = set()
        for urow in candidates:
            uid = safe_int(urow.get("User ID"))
            if uid and uid not in seen:
                uniq.append(urow)
                seen.add(uid)

        for urow in uniq[:10]:
            uid = safe_int(urow.get("User ID"))
            name = (urow.get("Name") or urow.get("Username") or str(uid)).strip()
            buttons.append([
                InlineKeyboardButton(f"🏖 {name} → отпуск", callback_data=f"admin:vac_on:{cid}:{tid}:{uid}"),
                InlineKeyboardButton(f"🧑‍💼 {name} → работа", callback_data=f"admin:vac_off:{cid}:{tid}:{uid}"),
            ])

        buttons.append([InlineKeyboardButton("🙂 Mode: изменить для команды", callback_data=f"admin:mode_team:{cid}:{tid}")])
        buttons.append([InlineKeyboardButton("🙂 Mode: изменить для сотрудника", callback_data=f"admin:mode_user_pick:{cid}:{tid}")])

        if chat_has_threads(chats_rows, cid):
            buttons.append([InlineKeyboardButton("⬅️ Назад к веткам", callback_data=f"admin:chatpick:{cid}")])
        buttons.append([InlineKeyboardButton("⬅️ Назад к чатам", callback_data="admin:report")])

        await q.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
        await q.answer()
        return

    # ADMIN VACATION TOGGLE
    if data.startswith("admin:vac_on:") or data.startswith("admin:vac_off:"):
        parts = data.split(":")
        action = parts[1]   # vac_on / vac_off
        cid = safe_int(parts[2])
        tid = safe_int(parts[3])
        uid = safe_int(parts[4])

        if not admin_scope_allows(user.id, cid, tid):
            await q.answer("Нет доступа")
            return

        status = "vacation" if action == "vac_on" else "active"

        idx = find_record_idx(today_str(), uid, cid, tid)
        hm = headers_map(records_sheet)

        if idx:
            if "Vacation" in hm:
                update_cell(records_sheet, idx, hm["Vacation"], status)
        else:
            _, chats_rows = safe_table(chats_sheet)
            _, users_rows = safe_table(users_sheet)

            chat_name = ""
            thread_name = ""
            for r in chats_rows:
                if safe_int(r.get("Chat ID")) == cid and norm_tid(r.get("Thread ID")) == tid:
                    chat_name = str(r.get("Chat name", "")).strip()
                    thread_name = str(r.get("Thread name", "")).strip()
                    break

            username = ""
            for urow in users_rows:
                if safe_int(urow.get("User ID")) == uid and safe_int(urow.get("Chat ID")) == cid and norm_tid(urow.get("Thread ID")) == tid:
                    username = str(urow.get("Username", "")).strip()
                    break

            append_row(records_sheet, [
                today_str(),
                cid,
                chat_name,
                tid if tid != 0 else "",
                thread_name,
                uid,
                username,
                "", "",  # Plan, Plan time
                "", "",  # Fact, Fact time
                status
            ])

        await q.answer("Готово ✅")
        return

    # MODE TEAM FROM ALL THREADS -> PICK THREAD
    if data.startswith("admin:mode_team_pickchat:"):
        try:
            _, _, cid_s = data.split(":", 2)
            cid = safe_int(cid_s)
        except Exception:
            await q.answer("Bad data")
            return

        if not admin_scope_allows(user.id, cid, 0):
            await q.answer("Нет доступа")
            return

        _, chats_rows = safe_table(chats_sheet)
        tids = chat_threads_for_chat(chats_rows, cid) or [0]

        buttons = []
        for tid in tids[:25]:
            if not admin_scope_allows(user.id, cid, tid):
                continue
            tlabel = thread_display_name(chats_rows, cid, tid)
            buttons.append([InlineKeyboardButton(f"🧵 {tlabel}", callback_data=f"admin:mode_team:{cid}:{tid}")])

        buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:thread_all:{cid}")])
        await q.message.reply_text("Выбери ветку для изменения Mode команды:", reply_markup=InlineKeyboardMarkup(buttons))
        await q.answer()
        return

    # MODE USER FROM ALL THREADS -> PICK THREAD
    if data.startswith("admin:mode_user_pickchat:"):
        try:
            _, _, cid_s = data.split(":", 2)
            cid = safe_int(cid_s)
        except Exception:
            await q.answer("Bad data")
            return

        if not admin_scope_allows(user.id, cid, 0):
            await q.answer("Нет доступа")
            return

        _, chats_rows = safe_table(chats_sheet)
        tids = chat_threads_for_chat(chats_rows, cid) or [0]

        buttons = []
        for tid in tids[:25]:
            if not admin_scope_allows(user.id, cid, tid):
                continue
            tlabel = thread_display_name(chats_rows, cid, tid)
            buttons.append([InlineKeyboardButton(f"🧵 {tlabel}", callback_data=f"admin:mode_user_pick:{cid}:{tid}")])

        buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:thread_all:{cid}")])
        await q.message.reply_text("Выбери ветку для изменения Mode сотрудника:", reply_markup=InlineKeyboardMarkup(buttons))
        await q.answer()
        return

    # MODE TEAM MENU
    if data.startswith("admin:mode_team:"):
        _, _, cid_s, tid_s = data.split(":", 3)
        cid = safe_int(cid_s)
        tid = safe_int(tid_s)

        if not admin_scope_allows(user.id, cid, tid):
            await q.answer("Нет доступа")
            return

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🙂 friendly (для команды)", callback_data=f"admin:set_mode_team:{cid}:{tid}:friendly")],
            [InlineKeyboardButton("📎 official (для команды)", callback_data=f"admin:set_mode_team:{cid}:{tid}:official")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:thread:{cid}:{tid}")],
        ])
        await q.message.reply_text("Выбери Mode для *всей команды*:", reply_markup=kb, parse_mode="Markdown")
        await q.answer()
        return

    if data.startswith("admin:set_mode_team:"):
        _, _, cid_s, tid_s, mode = data.split(":", 4)
        cid = safe_int(cid_s)
        tid = safe_int(tid_s)
        mode = (mode or "").strip().lower()

        if mode not in ("friendly", "official"):
            await q.answer("Некорректный Mode")
            return

        if not admin_scope_allows(user.id, cid, tid):
            await q.answer("Нет доступа")
            return

        hm = headers_map(users_sheet)
        _, rows = safe_table(users_sheet)

        changed = 0
        for idx, r in enumerate(rows, start=2):
            if safe_int(r.get("Chat ID")) == cid and norm_tid(r.get("Thread ID")) == tid:
                if "Mode" in hm:
                    update_cell(users_sheet, idx, hm["Mode"], mode)
                    changed += 1

        await q.answer(f"Готово ✅ ({changed})")
        return

    # MODE USER PICK
    if data.startswith("admin:mode_user_pick:"):
        _, _, cid_s, tid_s = data.split(":", 3)
        cid = safe_int(cid_s)
        tid = safe_int(tid_s)

        if not admin_scope_allows(user.id, cid, tid):
            await q.answer("Нет доступа")
            return

        team_users = team_users_for_thread(cid, tid)
        if not team_users:
            await q.message.reply_text("Сотрудники появятся здесь только после /plan в этой ветке.")
            await q.answer()
            return

        buttons = []
        for urow in team_users[:25]:
            uid = safe_int(urow.get("User ID"))
            name = (urow.get("Name") or urow.get("Username") or str(uid)).strip()
            buttons.append([InlineKeyboardButton(f"👤 {name}", callback_data=f"admin:mode_user:{cid}:{tid}:{uid}")])

        buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:thread:{cid}:{tid}")])
        await q.message.reply_text("Выбери сотрудника:", reply_markup=InlineKeyboardMarkup(buttons))
        await q.answer()
        return

    if data.startswith("admin:mode_user:"):
        _, _, cid_s, tid_s, uid_s = data.split(":", 4)
        cid = safe_int(cid_s)
        tid = safe_int(tid_s)
        uid = safe_int(uid_s)

        if not admin_scope_allows(user.id, cid, tid):
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
        mode = (mode or "").strip().lower()

        if mode not in ("friendly", "official"):
            await q.answer("Некорректный Mode")
            return

        if not admin_scope_allows(user.id, cid, tid):
            await q.answer("Нет доступа")
            return

        hm = headers_map(users_sheet)
        _, rows = safe_table(users_sheet)

        for idx, r in enumerate(rows, start=2):
            if safe_int(r.get("User ID")) == uid and safe_int(r.get("Chat ID")) == cid and norm_tid(r.get("Thread ID")) == tid:
                if "Mode" in hm:
                    update_cell(users_sheet, idx, hm["Mode"], mode)
                await q.answer("Готово ✅")
                return

        await q.answer("Пользователь не найден в Users (нужен /plan в этой ветке)")
        return

    await q.answer("Неизвестная команда")

# =========================================================
# REMINDERS LOOP
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

                    _, users_rows = safe_table(users_sheet)
                    _, chats_rows = safe_table(chats_sheet)
                    lw = last_workday_str()

                    for u in users_rows:
                        uid = safe_int(u.get("User ID"))
                        if not uid:
                            continue
                        if not norm_bool(u.get("Active", "TRUE")):
                            continue

                        cid = safe_int(u.get("Chat ID"))
                        tid = norm_tid(u.get("Thread ID"))
                        if not cid:
                            continue

                        # respect Chats.Active per chat+thread
                        if not chat_is_active(chats_rows, cid, tid):
                            continue

                        # vacation is per-day in Records
                        if in_vacation_today(uid, cid, tid):
                            continue

                        plan_ok = bool(get_record_field(today_str(), uid, cid, tid, "Plan"))
                        fact_ok = bool(get_record_field(lw, uid, cid, tid, "Fact"))

                        if plan_ok and fact_ok:
                            continue

                        case = "no_both" if (not plan_ok and not fact_ok) else ("no_plan" if not plan_ok else "no_fact")
                        mode = resolve_user_mode(users_rows, uid, cid, tid)

                        lines = [pick_message(case, mode)]
                        if not fact_ok:
                            lines.append(f"Факт нужен за {pretty_ddmm(lw)}")
                        if not plan_ok:
                            lines.append("План нужен за сегодня")

                        kwargs = {"chat_id": cid, "text": "\n".join(lines)}
                        if tid:
                            kwargs["message_thread_id"] = tid

                        try:
                            await app.bot.send_message(**kwargs)
                        except Exception as e:
                            print("⚠️ send_message error:", e)

            await asyncio.sleep(20)

        except Exception as e:
            print("⚠️ reminder error:", e)
            await asyncio.sleep(30)

# =========================================================
# STARTUP / POST INIT
# =========================================================
async def post_init(app):
    # do not crash even if reminder loop fails
    try:
        asyncio.create_task(reminder_loop(app))
    except Exception as e:
        print("⚠️ cannot start reminder loop:", e)

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set")
    if not spreadsheet:
        raise RuntimeError("Google Sheets not initialized")

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

    # registry catcher
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.ChatType.PRIVATE, catch_group_activity),
        group=0
    )

    # user commands
    app.add_handler(CommandHandler("plan", plan), group=1)
    app.add_handler(CommandHandler("fact", fact), group=1)
    app.add_handler(CommandHandler("vacation", vacation), group=1)

    # callbacks
    app.add_handler(CallbackQueryHandler(vacation_cb, pattern=r"^vac:(on|off)$"))
    app.add_handler(CallbackQueryHandler(admin_cb, pattern=r"^admin:"))

    # private entry
    app.add_handler(CommandHandler("start", start_cmd), group=90)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE, private_entry), group=100)

    app.run_polling()

if __name__ == "__main__":
    main()
