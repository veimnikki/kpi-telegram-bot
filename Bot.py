import os
import json
import random
import asyncio
from datetime import datetime, timedelta

import pytz
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

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
    raise RuntimeError("BOT_TOKEN not set")

# =========================================================
# GOOGLE SHEETS
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

try:
    user_settings_sheet = spreadsheet.worksheet("UserSettings")
except Exception:
    user_settings_sheet = None

# =========================================================
# TIME HELPERS
# =========================================================
def now():
    return datetime.now(tz)

def today_str():
    return now().strftime("%Y-%m-%d")

def now_time():
    return now().strftime("%H:%M")

def is_weekend():
    return now().weekday() >= 5

def last_workday_str():
    d = now().date()
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

def pretty_ddmm(d):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m")

# =========================================================
# SMALL UTILS
# =========================================================
def safe_int(x, default=0):
    try:
        return int(str(x).strip())
    except Exception:
        return default

def norm_bool(x):
    return str(x).strip().lower() in ("true", "1", "yes", "y", "да")

def headers_map(ws):
    row1 = ws.row_values(1)
    return {h.strip(): i + 1 for i, h in enumerate(row1) if h.strip()}

# =========================================================
# ADMIN CHECK
# =========================================================
def is_admin(user_id: int) -> bool:
    rows = admins_sheet.get_all_records()
    return any(safe_int(r.get("Admin user ID")) == user_id for r in rows)

# =========================================================
# CHAT + USER REGISTRATION
# =========================================================
def upsert_chat(chat):
    if chat.type == "private":
        return

    rows = chats_sheet.get_all_records()
    for i, r in enumerate(rows, start=2):
        if safe_int(r.get("Chat ID")) == chat.id:
            chats_sheet.update_cell(i, 2, chat.title or "")
            chats_sheet.update_cell(i, 3, chat.type)
            chats_sheet.update_cell(i, 4, f"{today_str()} {now_time()}")
            return

    chats_sheet.append_row([
        chat.id,
        chat.title or "",
        chat.type,
        f"{today_str()} {now_time()}",
    ])

def upsert_user(user, chat, thread_id):
    rows = users_sheet.get_all_records()
    for i, r in enumerate(rows, start=2):
        if safe_int(r.get("User ID")) == user.id:
            users_sheet.update_cell(i, 2, user.username or "")
            users_sheet.update_cell(i, 3, f"{user.first_name or ''} {user.last_name or ''}".strip())
            users_sheet.update_cell(i, 4, chat.id)
            users_sheet.update_cell(i, 5, thread_id or "")
            return

    users_sheet.append_row([
        user.id,
        user.username or "",
        f"{user.first_name or ''} {user.last_name or ''}".strip(),
        chat.id,
        thread_id or "",
        "",
        "TRUE",
    ])

# =========================================================
# RECORD HELPERS
# =========================================================
def find_record(records, date_, user_id, chat_id):
    for idx, r in enumerate(records, start=2):
        if (
            str(r.get("Date")) == date_
            and safe_int(r.get("User ID")) == user_id
            and safe_int(r.get("Chat ID")) == chat_id
        ):
            return idx, r
    return None, None

def ensure_today_row(chat, user, thread_id):
    records = records_sheet.get_all_records()
    idx, _ = find_record(records, today_str(), user.id, chat.id)
    if idx:
        return idx

    records_sheet.append_row([
        today_str(),
        chat.id,
        chat.title or "",
        user.id,
        user.username or "",
        "",
        "",
        "",
        "",
        "active",
        thread_id or "",
    ])
    return len(records_sheet.get_all_values())

def has_plan_today(records, uid, cid):
    _, r = find_record(records, today_str(), uid, cid)
    return bool(r and str(r.get("Plan", "")).strip())

def has_fact_last(records, uid, cid):
    _, r = find_record(records, last_workday_str(), uid, cid)
    return bool(r and str(r.get("Fact", "")).strip())

def in_vacation(records, uid, cid):
    _, r = find_record(records, today_str(), uid, cid)
    return bool(r and str(r.get("Vacation", "")).lower() == "vacation")

# =========================================================
# MESSAGES
# =========================================================
FRIENDLY = {
    "no_plan": "☀️ Доброе утро\nНе вижу план на сегодня 👀",
    "no_fact": "👀 Не вижу факт за прошлый рабочий день",
    "no_both": "☀️ Доброе утро\nНе вижу ни плана ни факта 👀",
}

OFFICIAL = {
    "no_plan": "План на сегодня отсутствует",
    "no_fact": "Факт за прошлый рабочий день не зафиксирован",
    "no_both": "Отсутствует план и факт",
}

def pick_message(case, mode):
    return FRIENDLY[case] if mode == "friendly" else OFFICIAL[case]

# =========================================================
# PRIVATE ENTRY
# =========================================================
SHOWN_PRIVATE = set()

async def private_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    if is_admin(user.id):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Отчет", callback_data="admin:report")]
        ])
        await update.message.reply_text(
            "Привет 👋\nЭто админ-версия бота.\n\nВыбери действие:",
            reply_markup=kb,
        )
        return

    if user.id in SHOWN_PRIVATE:
        return

    SHOWN_PRIVATE.add(user.id)
    await update.message.reply_text(
        "Привет 👋\n"
        "Я бот для планов и фактов.\n\n"
        "❗️Команды нужно писать **в рабочем чате**, где добавлен бот.\n\n"
        "/plan — план на день\n"
        "/fact — факт за прошлый рабочий день\n"
        "/vacation — отпуск\n\n"
        "Напоминания приходят автоматически."
    )

# =========================================================
# GROUP ACTIVITY (SERVICE)
# =========================================================
async def catch_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return

    upsert_chat(chat)
    upsert_user(user, chat, getattr(msg, "message_thread_id", ""))

# =========================================================
# /PLAN
# =========================================================
async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user

    if chat.type == "private":
        return

    text = msg.text.replace("/plan", "", 1).strip()
    if not text:
        await msg.reply_text("❗️План не может быть пустым")
        return

    idx = ensure_today_row(chat, user, getattr(msg, "message_thread_id", ""))

    hm = headers_map(records_sheet)
    records_sheet.update_cell(idx, hm["Plan"], text)
    records_sheet.update_cell(idx, hm["Plan time"], now_time())

    await context.bot.set_message_reaction(chat.id, msg.message_id, "👍")

# =========================================================
# /FACT
# =========================================================
async def fact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user

    if chat.type == "private":
        return

    text = msg.text.replace("/fact", "", 1).strip()
    if not text:
        await msg.reply_text("❗️Факт не может быть пустым")
        return

    records = records_sheet.get_all_records()
    d = last_workday_str()
    idx, row = find_record(records, d, user.id, chat.id)

    hm = headers_map(records_sheet)
    if idx and not str(row.get("Fact", "")).strip():
        records_sheet.update_cell(idx, hm["Fact"], text)
        records_sheet.update_cell(idx, hm["Fact time"], now_time())
    else:
        t_idx = ensure_today_row(chat, user, getattr(msg, "message_thread_id", ""))
        records_sheet.update_cell(t_idx, hm["Fact"], text)
        records_sheet.update_cell(t_idx, hm["Fact time"], now_time())

    await context.bot.set_message_reaction(chat.id, msg.message_id, "👍")

# =========================================================
# /VACATION
# =========================================================
async def vacation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌴 Уйти в отпуск", callback_data="vac:on")],
        [InlineKeyboardButton("🧑‍💼 Выйти из отпуска", callback_data="vac:off")],
    ])
    await update.message.reply_text("Статус отпуска:", reply_markup=kb)

async def vacation_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    msg = q.message
    chat = msg.chat
    user = q.from_user

    idx = ensure_today_row(chat, user, getattr(msg, "message_thread_id", ""))

    hm = headers_map(records_sheet)
    status = "vacation" if q.data == "vac:on" else "active"
    records_sheet.update_cell(idx, hm["Vacation"], status)

    await q.answer("Готово ✅")

# =========================================================
# REMINDER LOOP
# =========================================================
async def reminder_loop(app):
    last_day = None
    while True:
        try:
            if is_weekend():
                await asyncio.sleep(60)
                continue

            if now().hour == REMINDER_HOUR and now().minute == REMINDER_MINUTE:
                if last_day != today_str():
                    last_day = today_str()

                    users = users_sheet.get_all_records()
                    records = records_sheet.get_all_records()

                    for u in users:
                        uid = safe_int(u.get("User ID"))
                        if not uid or not norm_bool(u.get("Active", "TRUE")):
                            continue

                        cid = safe_int(u.get("Chat ID"))
                        if not cid:
                            continue

                        if in_vacation(records, uid, cid):
                            continue

                        plan_ok = has_plan_today(records, uid, cid)
                        fact_ok = has_fact_last(records, uid, cid)

                        if plan_ok and fact_ok:
                            continue

                        case = "no_both" if not plan_ok and not fact_ok else ("no_plan" if not plan_ok else "no_fact")
                        text = pick_message(case, DEFAULT_BOT_MODE)

                        await app.bot.send_message(chat_id=cid, text=text)

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
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # BUSINESS COMMANDS
    app.add_handler(CommandHandler("plan", plan), group=0)
    app.add_handler(CommandHandler("fact", fact), group=0)
    app.add_handler(CommandHandler("vacation", vacation), group=0)

    # SERVICE GROUP
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.ChatType.PRIVATE, catch_group),
        group=1
    )

    # CALLBACKS
    app.add_handler(CallbackQueryHandler(vacation_cb, pattern=r"^vac:(on|off)$"))

    # PRIVATE
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE, private_entry), group=100)

    app.run_polling()

if __name__ == "__main__":
    main()
