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
    ContextTypes,
    filters,
)

# ========================
# ENV + TIMEZONE
# ========================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_MODE = os.getenv("BOT_MODE", "friendly").lower().strip()

REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "10"))
REMINDER_MINUTE = int(os.getenv("REMINDER_MINUTE", "45"))

tz = pytz.timezone("Europe/Prague")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

# ========================
# GOOGLE SHEETS
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

# ========================
# HELPERS
# ========================
def now():
    return datetime.now(tz)

def today_str():
    return now().strftime("%Y-%m-%d")

def last_workday_str():
    d = now().date()
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

def now_time():
    return now().strftime("%H:%M")

def is_weekend():
    return now().weekday() >= 5

def pretty_ddmm(d):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%d.%m")

# ========================
# MESSAGES
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
    ],
    "vacation_no_fact": [
        "🏖️ Хорошего отдыха\nНо факт за прошлый рабочий день всё ещё нужен",
    ],
}

OFFICIAL = {
    "no_plan": ["План на сегодня отсутствует"],
    "no_fact": ["Факт за прошлый рабочий день не зафиксирован"],
    "no_both": ["Отсутствует план и факт"],
    "vacation_no_fact": ["Факт за прошлый рабочий день не зафиксирован"],
}

def pick_message(case):
    src = FRIENDLY if BOT_MODE == "friendly" else OFFICIAL
    return random.choice(src[case])

# ========================
# RECORD HELPERS
# ========================
def find_record(records, date_, user_id):
    for idx, r in enumerate(records, start=2):
        if str(r.get("Date")) == date_ and int(r.get("User ID")) == int(user_id):
            return idx, r
    return None, None

def has_plan_today(records, user_id):
    _, r = find_record(records, today_str(), user_id)
    return bool(r and str(r.get("Plan", "")).strip())

def has_fact_last_workday(records, user_id):
    _, r = find_record(records, last_workday_str(), user_id)
    return bool(r and str(r.get("Fact", "")).strip())

def in_vacation_today(records, user_id):
    _, r = find_record(records, today_str(), user_id)
    return bool(r and str(r.get("Vacation", "")).lower() == "vacation")

# ========================
# THREAD CATCHER
# ========================
async def catch_thread(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    users = users_sheet.get_all_records()
    for idx, row in enumerate(users, start=2):
        if int(row.get("User ID", 0)) == user.id:
            users_sheet.update_cell(idx, 3, chat.id)
            users_sheet.update_cell(idx, 4, getattr(msg, "message_thread_id", ""))
            return

# ========================
# /PLAN
# ========================
async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    text = (msg.text or "").replace("/plan", "", 1).strip()
    if not text:
        await msg.reply_text("❗️План не может быть пустым")
        return

    records = records_sheet.get_all_records()
    idx, _ = find_record(records, today_str(), user.id)

    if idx:
        records_sheet.update_cell(idx, 6, text)
        records_sheet.update_cell(idx, 7, now_time())
    else:
        records_sheet.append_row([
            today_str(), chat.id, chat.title, user.id, user.username or "",
            text, now_time(), "", "", "active"
        ])

    await context.bot.set_message_reaction(chat.id, msg.message_id, "👍")

# ========================
# /FACT
# ========================
async def fact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    text = (msg.text or "").replace("/fact", "", 1).strip()
    if not text:
        await msg.reply_text("❗️Факт не может быть пустым")
        return

    records = records_sheet.get_all_records()
    d = last_workday_str()

    idx, row = find_record(records, d, user.id)
    if row and not str(row.get("Fact", "")).strip():
        records_sheet.update_cell(idx, 8, text)
        records_sheet.update_cell(idx, 9, now_time())
    else:
        t_idx, _ = find_record(records, today_str(), user.id)
        if t_idx:
            records_sheet.update_cell(t_idx, 8, text)
            records_sheet.update_cell(t_idx, 9, now_time())
        else:
            records_sheet.append_row([
                today_str(), chat.id, chat.title, user.id, user.username or "",
                "", "", text, now_time(), "active"
            ])

    await context.bot.set_message_reaction(chat.id, msg.message_id, "👍")

# ========================
# /VACATION
# ========================
async def vacation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    kb = [
        [InlineKeyboardButton("🌴 Уйти в отпуск", callback_data="vac_on")],
        [InlineKeyboardButton("🧑‍💼 Выйти из отпуска", callback_data="vac_off")],
    ]
    await msg.reply_text(
        "Статус отпуска",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def vacation_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user

    records = records_sheet.get_all_records()
    idx, _ = find_record(records, today_str(), user.id)
    if not idx:
        await q.answer("Сначала нужен план")
        return

    status = "vacation" if q.data == "vac_on" else "active"
    records_sheet.update_cell(idx, 10, status)
    await q.answer("Готово")

# ========================
# REMINDERS LOOP
# ========================
async def reminder_loop(app):
    last_run = None

    while True:
        try:
            if is_weekend():
                await asyncio.sleep(60)
                continue

            n = now()
            if n.hour == REMINDER_HOUR and n.minute == REMINDER_MINUTE:
                if last_run != today_str():
                    last_run = today_str()

                    users = users_sheet.get_all_records()
                    records = records_sheet.get_all_records()

                    for u in users:
                        uid = int(u.get("User ID", 0))
                        if not uid:
                            continue
                        if not str(u.get("Active", "")).lower() in ("true", "1", "yes"):
                            continue

                        chat_id = u.get("Chat ID")
                        if not chat_id:
                            continue

                        if in_vacation_today(records, uid):
                            continue

                        plan_ok = has_plan_today(records, uid)
                        fact_ok = has_fact_last_workday(records, uid)

                        if plan_ok and fact_ok:
                            continue

                        if not plan_ok and not fact_ok:
                            case = "no_both"
                        elif not plan_ok:
                            case = "no_plan"
                        else:
                            case = "no_fact"

                        lines = [pick_message(case)]
                        if not fact_ok:
                            lines.append(f"Факт нужен за {pretty_ddmm(last_workday_str())}")
                        if not plan_ok:
                            lines.append("План нужен за сегодня")

                        await app.bot.send_message(
                            chat_id=int(chat_id),
                            text="\n".join(lines)
                        )

            await asyncio.sleep(20)

        except Exception as e:
            print("⚠️ reminder error:", e)
            await asyncio.sleep(30)

# ========================
# HELP / PRIVATE MODE
# ========================
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Привет 👋\n"
        "Я бот для планов и фактов\n\n"
        "Команды:\n"
        "• /plan — план на день (в рабочем чате)\n"
        "• /fact — факт за день (в рабочем чате)\n"
        "• /vacation — отпуск\n\n"
        "Напоминания приходят автоматически"
    )

async def private_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await help_cmd(update, context)

# ========================
# START
# ========================
async def post_init(app):
    asyncio.create_task(reminder_loop(app))

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("plan", plan))
    app.add_handler(CommandHandler("fact", fact))
    app.add_handler(CommandHandler("vacation", vacation))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, catch_thread))
    app.add_handler(MessageHandler(filters.TEXT, private_fallback), group=100)

    app.run_polling()

if __name__ == "__main__":
    main()
