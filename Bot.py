import os
import json
import random
import asyncio
from datetime import datetime, timedelta

import pytz
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

from telegram import Update
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
BOT_MODE = os.getenv("BOT_MODE", "friendly").strip().lower()

REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "10"))
REMINDER_MINUTE = int(os.getenv("REMINDER_MINUTE", "45"))

tz = pytz.timezone("Europe/Prague")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

# ========================
# GOOGLE SHEETS (ENV CREDS)
# ========================
creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not creds_json:
    raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not set")

creds_dict = json.loads(creds_json)

credentials = Credentials.from_service_account_info(
    creds_dict,
    scopes=["https://www.googleapis.com/auth/spreadsheets"],
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

def yesterday_str():
    return (now() - timedelta(days=1)).strftime("%Y-%m-%d")

def now_time_str():
    return now().strftime("%H:%M")

def is_weekend():
    return now().weekday() >= 5

def pretty_ddmm(date_str):
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%d.%m")

# ========================
# MESSAGES
# ========================
FRIENDLY = {
    "no_plan": [
        "☀️ Доброе утро\nУпс… не вижу план на сегодня 👀",
        "🌤️ План на сегодня пока прячется",
    ],
    "no_fact": [
        "👀 Кажется забыли факт за вчера",
        "🧾 Вчерашний факт ещё не записан",
    ],
    "no_both": [
        "☀️ Доброе утро\nПока не вижу ни плана ни факта 👀",
        "🧹 Нужно закрыть план и факт",
    ],
    "vacation_no_fact": [
        "🏖️ Хорошего отдыха\nНо факт за вчера всё ещё нужен",
    ],
}

OFFICIAL = {
    "no_plan": ["План на сегодня отсутствует"],
    "no_fact": ["Факт за предыдущий день не зафиксирован"],
    "no_both": ["Отсутствует план и факт"],
    "vacation_no_fact": ["Факт за предыдущий день не зафиксирован"],
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

def has_fact_yesterday(records, user_id):
    _, r = find_record(records, yesterday_str(), user_id)
    return bool(r and str(r.get("Fact", "")).strip())

def vacation_today(records, user_id):
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

    thread_id = getattr(msg, "message_thread_id", "")

    users = users_sheet.get_all_records()
    for idx, row in enumerate(users, start=2):
        if int(row.get("User ID", 0)) == user.id:
            users_sheet.update_cell(idx, 3, chat.id)
            users_sheet.update_cell(idx, 4, thread_id)
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
        records_sheet.update_cell(idx, 7, now_time_str())
    else:
        records_sheet.append_row([
            today_str(), chat.id, chat.title, user.id, user.username or "",
            text, now_time_str(), "", "", "active"
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

    y_idx, y_row = find_record(records, yesterday_str(), user.id)
    if y_row and not str(y_row.get("Fact", "")).strip():
        records_sheet.update_cell(y_idx, 8, text)
        records_sheet.update_cell(y_idx, 9, now_time_str())
    else:
        t_idx, _ = find_record(records, today_str(), user.id)
        if t_idx:
            records_sheet.update_cell(t_idx, 8, text)
            records_sheet.update_cell(t_idx, 9, now_time_str())
        else:
            records_sheet.append_row([
                today_str(), chat.id, chat.title, user.id, user.username or "",
                "", "", text, now_time_str(), "active"
            ])

    await context.bot.set_message_reaction(chat.id, msg.message_id, "👍")

# ========================
# REMINDERS
# ========================
async def reminder_loop(app):
    last_run = None

    while True:
        try:
            if is_weekend():
                await asyncio.sleep(60)
                continue

            now_dt = now()
            if now_dt.hour == REMINDER_HOUR and now_dt.minute == REMINDER_MINUTE:
                today = today_str()
                if last_run != today:
                    last_run = today
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

                        plan_ok = has_plan_today(records, uid)
                        fact_ok = has_fact_yesterday(records, uid)
                        vac = vacation_today(records, uid)

                        case = None
                        if not plan_ok and not fact_ok and not vac:
                            case = "no_both"
                        elif not plan_ok and fact_ok and not vac:
                            case = "no_plan"
                        elif not fact_ok and vac:
                            case = "vacation_no_fact"
                        elif not fact_ok:
                            case = "no_fact"

                        if not case:
                            continue

                        text = pick_message(case)
                        lines = [text]

                        if case in ("no_fact", "no_both", "vacation_no_fact"):
                            lines.append(f"Факт нужен за {pretty_ddmm(yesterday_str())}")
                        if case in ("no_plan", "no_both"):
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
# START
# ========================
async def post_init(app):
    app.create_task(reminder_loop(app))

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("plan", plan))
    app.add_handler(CommandHandler("fact", fact))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, catch_thread))

    app.run_polling()

if __name__ == "__main__":
    main()
