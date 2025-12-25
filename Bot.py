import os
import json
import random
import asyncio
from datetime import datetime, timedelta, date

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

# ========================
# ENV + TIMEZONE
# ========================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DEFAULT_BOT_MODE = os.getenv("BOT_MODE", "friendly").lower().strip()

REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "10"))
REMINDER_MINUTE = int(os.getenv("REMINDER_MINUTE", "45"))

tz = pytz.timezone("Europe/Prague")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

# ========================
# GOOGLE SHEETS (ENV CREDS)
# ========================
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
# optional, if you want mode overrides by chat/user
try:
    user_settings_sheet = spreadsheet.worksheet("UserSettings")
except Exception:
    user_settings_sheet = None


# ========================
# SMALL UTILS
# ========================
def now_dt() -> datetime:
    return datetime.now(tz)

def today_str() -> str:
    return now_dt().strftime("%Y-%m-%d")

def now_time_str() -> str:
    return now_dt().strftime("%H:%M")

def is_weekend() -> bool:
    return now_dt().weekday() >= 5  # 5=Sat, 6=Sun

def pretty_ddmm(d_yyyy_mm_dd: str) -> str:
    return datetime.strptime(d_yyyy_mm_dd, "%Y-%m-%d").strftime("%d.%m")

def last_workday_str() -> str:
    """For reminders and /fact logic:
    - Mon -> Fri (minus 3 days)
    - Tue-Fri -> prev day
    - Sat/Sun won't be used for reminders (we skip weekends), but keep safe.
    """
    d = now_dt().date()
    wd = d.weekday()  # 0=Mon
    if wd == 0:
        d = d - timedelta(days=3)
    elif wd == 6:
        d = d - timedelta(days=2)
    elif wd == 5:
        d = d - timedelta(days=1)
    else:
        d = d - timedelta(days=1)
    return d.strftime("%Y-%m-%d")


# ========================
# MESSAGES / MODES
# ========================
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


# ========================
# SHEET HELPERS (header-based)
# ========================
def headers_map(ws):
    """Returns dict: header_name -> column_index (1-based)"""
    row1 = ws.row_values(1)
    return {h.strip(): i + 1 for i, h in enumerate(row1) if h.strip()}

def get_col(ws, name: str) -> int:
    hm = headers_map(ws)
    if name not in hm:
        raise RuntimeError(f"Sheet '{ws.title}' missing required column '{name}'")
    return hm[name]

def safe_int(x, default=0):
    try:
        return int(str(x).strip())
    except Exception:
        return default

def norm_bool(x) -> bool:
    return str(x).strip().lower() in ("true", "1", "yes", "y", "да")


# ========================
# ADMIN CHECK (Admins sheet)
# Admins sheet format (as you showed):
#   A: Admin user ID
#   B: Username
# Optional C: Chat IDs (comma-separated) if you want assignment later
# ========================
def is_admin_user(user_id: int) -> bool:
    rows = admins_sheet.get_all_records()
    for r in rows:
        if safe_int(r.get("Admin user ID")) == int(user_id):
            return True
    return False


# ========================
# CHATS REGISTRY (Chats sheet)
# We will upsert chat on any group/supergroup message
# Recommended headers in Chats:
#   Chat ID | Chat title | Chat type | Updated at
# ========================
def upsert_chat(chat):
    if not chat:
        return
    if chat.type == "private":
        return

    hm = headers_map(chats_sheet)
    # If user created sheet with different headers, we still try minimal by index.
    # Preferred:
    # Chat ID, Chat title, Chat type, Updated at
    chat_id_col = hm.get("Chat ID", 1)
    title_col = hm.get("Chat title", 2)
    type_col = hm.get("Chat type", 3)
    upd_col = hm.get("Updated at", 4)

    rows = chats_sheet.get_all_records()
    for idx, r in enumerate(rows, start=2):
        if safe_int(r.get("Chat ID")) == int(chat.id):
            chats_sheet.update_cell(idx, title_col, chat.title or "")
            chats_sheet.update_cell(idx, type_col, chat.type or "")
            chats_sheet.update_cell(idx, upd_col, f"{today_str()} {now_time_str()}")
            return

    chats_sheet.append_row([
        chat.id,
        chat.title or "",
        chat.type or "",
        f"{today_str()} {now_time_str()}",
    ])


# ========================
# USERS SHEET UPSERT (chat/thread binding)
# Users sheet (your screenshot) headers:
#   User ID | Username | Full name | Chat ID | Thread ID | Team | Active
# ========================
def upsert_user_binding(user, chat, thread_id):
    if not user or not chat:
        return

    hm = headers_map(users_sheet)
    uid_col = hm.get("User ID", 1)
    username_col = hm.get("Username", 2)
    fullname_col = hm.get("Full name", 3)
    chatid_col = hm.get("Chat ID", 4)
    thread_col = hm.get("Thread ID", 5)
    team_col = hm.get("Team", 6)
    active_col = hm.get("Active", 7)

    rows = users_sheet.get_all_records()
    for idx, r in enumerate(rows, start=2):
        if safe_int(r.get("User ID")) == int(user.id):
            # update minimal fields (do not overwrite Team if filled)
            users_sheet.update_cell(idx, username_col, user.username or "")
            users_sheet.update_cell(idx, fullname_col, f"{user.first_name or ''} {user.last_name or ''}".strip())
            users_sheet.update_cell(idx, chatid_col, chat.id)
            users_sheet.update_cell(idx, thread_col, thread_id or "")
            if "Active" in hm and not str(r.get("Active", "")).strip():
                users_sheet.update_cell(idx, active_col, "TRUE")
            return

    users_sheet.append_row([
        user.id,
        user.username or "",
        f"{user.first_name or ''} {user.last_name or ''}".strip(),
        chat.id,
        thread_id or "",
        "",       # Team
        "TRUE",   # Active
    ])


# ========================
# RECORDS HELPERS
# Records recommended headers:
# Date | Chat ID | Chat title | User ID | Username | Plan | Plan time | Fact | Fact time | Vacation
# + optional Thread ID column (if you add it)
# ========================
def find_record(records, date_, user_id, chat_id=None):
    """Find record for a user/date. If chat_id given, require it too."""
    for idx, r in enumerate(records, start=2):
        if str(r.get("Date")) == str(date_) and safe_int(r.get("User ID")) == int(user_id):
            if chat_id is None or safe_int(r.get("Chat ID")) == int(chat_id):
                return idx, r
    return None, None

def ensure_record_today(chat, user, thread_id=None):
    """Ensure there is a row for today/user/chat"""
    records = records_sheet.get_all_records()
    idx, r = find_record(records, today_str(), user.id, chat.id)
    if idx:
        return idx

    # Find header map
    hm = headers_map(records_sheet)
    # We append in the expected order from headers if possible.
    # If headers are exactly as recommended, this matches.
    # If not, it still appends columns in a fixed order (keep your sheet consistent).
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

    # If you have "Thread ID" column, extend
    if "Thread ID" in hm:
        # Insert after Chat ID maybe – but safest: append at the end if sheet expects it.
        row.append(thread_id or "")

    records_sheet.append_row(row)
    # return new row idx
    return len(records_sheet.get_all_values())

def has_plan_today(records, user_id, chat_id):
    _, r = find_record(records, today_str(), user_id, chat_id)
    return bool(r and str(r.get("Plan", "")).strip())

def has_fact_last_workday(records, user_id, chat_id):
    _, r = find_record(records, last_workday_str(), user_id, chat_id)
    return bool(r and str(r.get("Fact", "")).strip())

def in_vacation_today(records, user_id, chat_id):
    _, r = find_record(records, today_str(), user_id, chat_id)
    return bool(r and str(r.get("Vacation", "")).strip().lower() == "vacation")


# ========================
# MODE OVERRIDES (optional UserSettings sheet)
# UserSettings suggested headers:
#   Scope | Scope ID | Mode
# where Scope in ("user","chat"), Scope ID = user_id/chat_id, Mode in ("friendly","official")
# ========================
def resolve_mode(chat_id: int, user_id: int) -> str:
    # default
    mode = DEFAULT_BOT_MODE if DEFAULT_BOT_MODE in ("friendly", "official") else "friendly"
    if not user_settings_sheet:
        return mode

    rows = user_settings_sheet.get_all_records()
    # user override first
    for r in rows:
        if str(r.get("Scope", "")).strip().lower() == "user" and safe_int(r.get("Scope ID")) == int(user_id):
            m = str(r.get("Mode", "")).strip().lower()
            if m in ("friendly", "official"):
                return m

    for r in rows:
        if str(r.get("Scope", "")).strip().lower() == "chat" and safe_int(r.get("Scope ID")) == int(chat_id):
            m = str(r.get("Mode", "")).strip().lower()
            if m in ("friendly", "official"):
                return m

    return mode


# ========================
# PRIVATE MODE (silent for non-admin)
# ========================
SHOWN_HELP_PRIVATE = set()  # in-memory, resets after restart

USER_HELP_TEXT = (
    "Привет 👋\n"
    "Я бот для планов и фактов.\n\n"
    "📌 ВАЖНО: команды нужно писать в *рабочем чате*, где добавлен бот.\n\n"
    "Команды:\n"
    "• /plan — план на день\n"
    "• /fact — факт за прошлый рабочий день\n"
    "• /vacation — отпуск\n\n"
    "⏰ Напоминания приходят автоматически в 10:45 (по Праге), только по будням."
)

ADMIN_HELP_TEXT = (
    "Привет 👋 Это *админ-версия*.\n\n"
    "Что умею:\n"
    "• 📊 Отчёты по чатам\n"
    "• 🏖 Переключать отпуск сотрудникам (через кнопки)\n"
    "• 🙂 Менять стиль общения (friendly/official) через UserSettings (если включишь)\n\n"
    "Нажми кнопку ниже:"
)


async def private_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Any message in private goes here. Non-admins: show help once then ignore."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or chat.type != "private" or not user:
        return

    if is_admin_user(user.id):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Отчет", callback_data="admin:report")],
            [InlineKeyboardButton("ℹ️ Помощь", callback_data="admin:help")],
        ])
        await update.effective_message.reply_text(ADMIN_HELP_TEXT, reply_markup=kb, parse_mode="Markdown")
        return

    # non-admin
    if user.id in SHOWN_HELP_PRIVATE:
        return
    SHOWN_HELP_PRIVATE.add(user.id)
    await update.effective_message.reply_text(USER_HELP_TEXT, parse_mode="Markdown")


# ========================
# CHAT ROUTER (catch messages in groups)
# - update Chats sheet
# - update Users binding (chat/thread)
# ========================
async def catch_group_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return
    if chat.type == "private":
        return

    # 1) upsert chat
    try:
        upsert_chat(chat)
    except Exception as e:
        print("⚠️ upsert_chat error:", e)

    # 2) upsert user binding (chat_id + thread_id)
    thread_id = getattr(msg, "message_thread_id", "")
    try:
        upsert_user_binding(user, chat, thread_id)
    except Exception as e:
        print("⚠️ upsert_user_binding error:", e)


# ========================
# /PLAN  (GROUP ONLY)
# ========================
async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user

    if not chat or chat.type == "private":
        # ignore in private
        return

    text = (msg.text or "").replace("/plan", "", 1).strip()
    if not text:
        await msg.reply_text("❗️План не может быть пустым")
        return

    thread_id = getattr(msg, "message_thread_id", "")
    idx = ensure_record_today(chat, user, thread_id=thread_id)

    # columns by header (recommended)
    hm = headers_map(records_sheet)
    plan_col = hm.get("Plan", 6)
    plan_time_col = hm.get("Plan time", 7)

    records_sheet.update_cell(idx, plan_col, text)
    records_sheet.update_cell(idx, plan_time_col, now_time_str())

    await context.bot.set_message_reaction(chat.id, msg.message_id, "👍")


# ========================
# /FACT  (GROUP ONLY)
# ========================
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

    records = records_sheet.get_all_records()
    d = last_workday_str()

    # Try write into last workday row (same chat)
    idx, row = find_record(records, d, user.id, chat.id)
    hm = headers_map(records_sheet)
    fact_col = hm.get("Fact", 8)
    fact_time_col = hm.get("Fact time", 9)

    if row and not str(row.get("Fact", "")).strip():
        records_sheet.update_cell(idx, fact_col, text)
        records_sheet.update_cell(idx, fact_time_col, now_time_str())
    else:
        # else write into today (same chat)
        thread_id = getattr(msg, "message_thread_id", "")
        t_idx = ensure_record_today(chat, user, thread_id=thread_id)
        records_sheet.update_cell(t_idx, fact_col, text)
        records_sheet.update_cell(t_idx, fact_time_col, now_time_str())

    await context.bot.set_message_reaction(chat.id, msg.message_id, "👍")


# ========================
# /VACATION  (GROUP ONLY)
# ========================
async def vacation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user

    if not chat or chat.type == "private":
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌴 Уйти в отпуск", callback_data="vac:on")],
        [InlineKeyboardButton("🧑‍💼 Выйти из отпуска", callback_data="vac:off")],
    ])
    await msg.reply_text("Статус отпуска:", reply_markup=kb)

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

    # ensure today's row exists even if no plan yet
    idx = ensure_record_today(chat, user, thread_id=getattr(msg, "message_thread_id", ""))

    hm = headers_map(records_sheet)
    vac_col = hm.get("Vacation", 10)

    status = "vacation" if q.data == "vac:on" else "active"
    records_sheet.update_cell(idx, vac_col, status)

    await q.answer("Готово ✅")


# ========================
# ADMIN PANEL (PRIVATE ONLY)
# ========================
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
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Отчет", callback_data="admin:report")],
        ])
        await q.message.reply_text(ADMIN_HELP_TEXT, reply_markup=kb, parse_mode="Markdown")
        await q.answer()
        return

    if data == "admin:report":
        # Show list of chats (from Chats sheet)
        chats = chats_sheet.get_all_records()
        if not chats:
            await q.message.reply_text("Во вкладке Chats пока пусто. Напиши любое сообщение в рабочем чате, где есть бот — и он добавится.")
            await q.answer()
            return

        buttons = []
        for r in chats[:25]:
            cid = safe_int(r.get("Chat ID"))
            title = str(r.get("Chat title", "")).strip() or str(cid)
            buttons.append([InlineKeyboardButton(f"📌 {title}", callback_data=f"admin:chat:{cid}")])

        await q.message.reply_text("Выбери чат для отчёта:", reply_markup=InlineKeyboardMarkup(buttons))
        await q.answer()
        return

    if data.startswith("admin:chat:"):
        chat_id = safe_int(data.split(":")[-1])
        users = users_sheet.get_all_records()
        records = records_sheet.get_all_records()

        # users bound to this chat
        team_users = [u for u in users if safe_int(u.get("Chat ID")) == chat_id and norm_bool(u.get("Active", "TRUE"))]

        if not team_users:
            await q.message.reply_text("Не вижу пользователей с этим Chat ID во вкладке Users. Нужно, чтобы они написали что-то в том чате (любое сообщение), тогда бот запишет привязку.")
            await q.answer()
            return

        vac_count = 0
        plan_ok = 0
        fact_ok = 0
        missing_plan = []
        missing_fact = []

        lw = last_workday_str()
        for u in team_users:
            uid = safe_int(u.get("User ID"))
            if in_vacation_today(records, uid, chat_id):
                vac_count += 1
                continue

            if has_plan_today(records, uid, chat_id):
                plan_ok += 1
            else:
                missing_plan.append(u)

            if has_fact_last_workday(records, uid, chat_id):
                fact_ok += 1
            else:
                missing_fact.append(u)

        active_non_vac = len(team_users) - vac_count

        text = (
            f"📊 Отчет по чату: *{chat_id}*\n"
            f"Дата: *{today_str()}*\n\n"
            f"👥 Активных (без отпуска): *{active_non_vac}*\n"
            f"🏖 В отпуске сегодня: *{vac_count}*\n\n"
            f"✅ План есть: *{plan_ok}/{active_non_vac}*\n"
            f"✅ Факт есть (за {pretty_ddmm(lw)}): *{fact_ok}/{active_non_vac}*\n"
        )

        # Add buttons to toggle vacation quickly (for users who forgot)
        buttons = []
        # show up to 10 missing users to avoid huge keyboards
        for u in (missing_plan + missing_fact)[:10]:
            uid = safe_int(u.get("User ID"))
            name = (u.get("Full name") or u.get("Username") or str(uid)).strip()
            buttons.append([
                InlineKeyboardButton(f"🏖 {name} → отпуск", callback_data=f"admin:vac_on:{chat_id}:{uid}"),
                InlineKeyboardButton(f"🧑‍💼 {name} → работа", callback_data=f"admin:vac_off:{chat_id}:{uid}"),
            ])

        buttons.append([InlineKeyboardButton("⬅️ Назад к чатам", callback_data="admin:report")])

        await q.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
        await q.answer()
        return

    if data.startswith("admin:vac_on:") or data.startswith("admin:vac_off:"):
        parts = data.split(":")
        action = parts[1]  # vac_on / vac_off
        chat_id = safe_int(parts[2])
        uid = safe_int(parts[3])

        # ensure today's record exists for that user/chat (admin can create row)
        # we don't have a User object here; so we update record by searching Users sheet for username and making row manually if missing.
        users = users_sheet.get_all_records()
        urow = None
        for u in users:
            if safe_int(u.get("User ID")) == uid and safe_int(u.get("Chat ID")) == chat_id:
                urow = u
                break

        records = records_sheet.get_all_records()
        idx, row = find_record(records, today_str(), uid, chat_id)

        hm = headers_map(records_sheet)
        vac_col = hm.get("Vacation", 10)

        status = "vacation" if action == "vac_on" else "active"

        if idx:
            records_sheet.update_cell(idx, vac_col, status)
        else:
            # create minimal row
            chat_title = ""
            chats = chats_sheet.get_all_records()
            for c in chats:
                if safe_int(c.get("Chat ID")) == chat_id:
                    chat_title = c.get("Chat title", "") or ""
                    break

            username = (urow.get("Username") if urow else "") or ""
            # append row in expected order
            records_sheet.append_row([
                today_str(), chat_id, chat_title, uid, username,
                "", "", "", "", status
            ])

        await q.answer("Готово ✅")
        return

    await q.answer("Неизвестная команда")


# ========================
# REMINDERS LOOP
# ========================
async def reminder_loop(app):
    last_run_day = None

    while True:
        try:
            if is_weekend():
                await asyncio.sleep(60)
                continue

            n = now_dt()
            if n.hour == REMINDER_HOUR and n.minute == REMINDER_MINUTE:
                today = today_str()
                if last_run_day != today:
                    last_run_day = today

                    users = users_sheet.get_all_records()
                    records = records_sheet.get_all_records()
                    lw = last_workday_str()

                    for u in users:
                        uid = safe_int(u.get("User ID"))
                        if not uid:
                            continue
                        if not norm_bool(u.get("Active", "TRUE")):
                            continue

                        chat_id = safe_int(u.get("Chat ID"))
                        if not chat_id:
                            continue

                        thread_id = u.get("Thread ID", "")
                        thread_id_int = safe_int(thread_id, default=0) if str(thread_id).strip() else 0

                        # skip if vacation today
                        if in_vacation_today(records, uid, chat_id):
                            continue

                        plan_ok = has_plan_today(records, uid, chat_id)
                        fact_ok = has_fact_last_workday(records, uid, chat_id)

                        if plan_ok and fact_ok:
                            continue

                        case = "no_both" if (not plan_ok and not fact_ok) else ("no_plan" if not plan_ok else "no_fact")
                        mode = resolve_mode(chat_id=chat_id, user_id=uid)

                        lines = [pick_message(case, mode)]
                        if not fact_ok:
                            lines.append(f"Факт нужен за {pretty_ddmm(lw)}")
                        if not plan_ok:
                            lines.append("План нужен за сегодня")

                        kwargs = {"chat_id": chat_id, "text": "\n".join(lines)}
                        if thread_id_int:
                            kwargs["message_thread_id"] = thread_id_int

                        await app.bot.send_message(**kwargs)

            await asyncio.sleep(20)

        except Exception as e:
            print("⚠️ reminder error:", e)
            await asyncio.sleep(30)


# ========================
# START
# ========================
async def post_init(app):
    # start reminders loop
    asyncio.create_task(reminder_loop(app))

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # GROUP activity catcher (register chats + bind users)
    app.add_handler(MessageHandler(filters.ALL & ~filters.ChatType.PRIVATE, catch_group_activity), group=0)

    # commands (work chats only)
    app.add_handler(CommandHandler("plan", plan))
    app.add_handler(CommandHandler("fact", fact))
    app.add_handler(CommandHandler("vacation", vacation))

    # callbacks
    app.add_handler(CallbackQueryHandler(vacation_cb, pattern=r"^vac:(on|off)$"))
    app.add_handler(CallbackQueryHandler(admin_cb, pattern=r"^admin:"))

    # private: any text/command -> entry (non-admin: help once then silent)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE, private_entry), group=100)

    app.run_polling()

if __name__ == "__main__":
    main()
