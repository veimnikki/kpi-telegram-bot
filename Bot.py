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

credentials = Credentials.from_service_account_info(
    json.loads(creds_json),
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
# TIME HELPERS
# ========================
def now():
    return datetime.now(tz)

def today_str():
    return now().strftime("%Y-%m-%d")

def now_time_str():
    return now().strftime("%H:%M")

def is_weekend():
    return now().weekday() >= 5

def last_workday_str():
    wd = now().weekday()
    if wd == 0:
        d = now() - timedelta(days=3)
    elif wd in (5, 6):
        d = now() - timedelta(days=wd - 4)
    else:
        d = now() - timedelta(days=1)
    return d.strftime("%Y-%m-%d")

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
        "👀 Кажется забыли факт",
        "🧾 Факт за прошлый рабочий день ещё не записан",
    ],
    "no_both": [
        "☀️ Доброе утро\nПока не вижу ни плана ни факта 👀",
        "🧹 Нужно закрыть план и факт",
    ],
}

def pick_message(case):
    return random.choice(FRIENDLY[case])

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

def is_on_vacation(records, user_id):
    _, r = find_record(records, today_str(), user_id)
    return bool(r and str(r.get("Vacation", "")).lower() == "vacation")

# ========================
# THREAD CATCHER
# ========================
async def catch_thread(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not (msg and user and chat):
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
            text, now_time_str(), "", "", ""
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
    date_ = last_workday_str()

    idx, r = find_record(records, date_, user.id)
    if r and not str(r.get("Fact", "")).strip():
        records_sheet.update_cell(idx, 8, text)
        records_sheet.update_cell(idx, 9, now_time_str())
    else:
        idx, _ = find_record(records, today_str(), user.id)
        if idx:
            records_sheet.update_cell(idx, 8, text)
            records_sheet.update_cell(idx, 9, now_time_str())
        else:
            records_sheet.append_row([
                today_str(), chat.id, chat.title, user.id, user.username or "",
                "", "", text, now_time_str(), ""
            ])

    await context.bot.set_message_reaction(chat.id, msg.message_id, "👍")

# ========================
# /VACATION
# ========================
async def vacation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    records = records_sheet.get_all_records()

    on_vac = is_on_vacation(records, user.id)

    if on_vac:
        text = "🏖 Ты сейчас **в отпуске**\nХочешь вернуться?"
        keyboard = [[InlineKeyboardButton("🧑‍💼 Выйти из отпуска", callback_data="vac_off")]]
    else:
        text = "🏖 Ты сейчас **НЕ в отпуске**\nЧто сделать?"
        keyboard = [[InlineKeyboardButton("🌴 Уйти в отпуск", callback_data="vac_on")]]

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def vacation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    records = records_sheet.get_all_records()

    idx, _ = find_record(records, today_str(), user.id)
    if not idx:
        records_sheet.append_row([
            today_str(), query.message.chat.id, query.message.chat.title,
            user.id, user.username or "", "", "", "", "", ""
        ])
        records = records_sheet.get_all_records()
        idx, _ = find_record(records, today_str(), user.id)

    value = "vacation" if query.data == "vac_on" else ""
    records_sheet.update_cell(idx, 10, value)

    await query.answer("Готово 👍")
    await query.message.edit_text("Статус отпуска обновлён")

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

            if now().hour == REMINDER_HOUR and now().minute == REMINDER_MINUTE:
                today = today_str()
                if last_run != today:
                    last_run = today
                    users = users_sheet.get_all_records()
                    records = records_sheet.get_all_records()

                    for u in users:
                        uid = int(u.get("User ID", 0))
                        if not uid:
                            continue
                        if str(u.get("Active", "")).lower() not in ("true", "1", "yes"):
                            continue
                        if is_on_vacation(records, uid):
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
                            chat_id=int(u["Chat ID"]),
                            text="\n".join(lines)
                        )

            await asyncio.sleep(20)

        except Exception as e:
            print("⚠️ reminder error:", e)
            await asyncio.sleep(30)

# ========================
# START
# ========================
async def start_reminders(context: ContextTypes.DEFAULT_TYPE):
    context.application.create_task(reminder_loop(context.application))

async def post_init(app):
    app.job_queue.run_once(start_reminders, when=1)

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("plan", plan))
    app.add_handler(CommandHandler("fact", fact))
    app.add_handler(CommandHandler("vacation", vacation))
    app.add_handler(CallbackQueryHandler(vacation_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, catch_thread))

    app.run_polling()

if __name__ == "__main__":
    main()
