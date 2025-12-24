import os
import random
import asyncio
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import gspread
from datetime import datetime, timedelta
import pytz

# ========================
# ENV + TIMEZONE
# ========================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# friendly | official
BOT_MODE = os.getenv("BOT_MODE", "friendly").strip().lower()

tz = pytz.timezone("Europe/Prague")

# Время ежедневной проверки (Прага)
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "10"))
REMINDER_MINUTE = int(os.getenv("REMINDER_MINUTE", "45"))

# ========================
# GOOGLE SHEETS
# ========================
gc = gspread.service_account(filename="credentials.json")
spreadsheet = gc.open("KPI_Plans")

records_sheet = spreadsheet.worksheet("Records")
users_sheet = spreadsheet.worksheet("Users")

# ========================
# HELPERS
# ========================
def today_str() -> str:
    return datetime.now(tz).strftime("%Y-%m-%d")

def yesterday_str() -> str:
    return (datetime.now(tz) - timedelta(days=1)).strftime("%Y-%m-%d")

def now_time_str() -> str:
    return datetime.now(tz).strftime("%H:%M")

def is_weekend() -> bool:
    return datetime.now(tz).weekday() >= 5  # 5=Sat,6=Sun

def pretty_ddmm(date_yyyy_mm_dd: str) -> str:
    return datetime.strptime(date_yyyy_mm_dd, "%Y-%m-%d").strftime("%d.%m")

def pick_message(case: str) -> str:
    FRIENDLY = {
        "no_plan": [
            "☀️ Доброе утро\nУпс… не вижу план на сегодня 👀",
            "Доброе утро ☀️\nРасскажешь планы на сегодня?",
            "🌤️ Кажется план на сегодня где-то потерялся",
        ],
        "no_fact": [
            "👀 Кажется кто-то забыл факт за вчера",
            "📌 Вижу что факт за вчера ещё не написан 👀",
            "🧾 Вчерашний факт ещё не зафиксирован",
        ],
        "no_both": [
            "☀️ Доброе утро\nПока не вижу ни плана на сегодня ни факта за вчера 👀",
            "🧹 Кажется есть что закрыть 🙉\nНужны план на сегодня и факт за вчера",
        ],
        "vacation_no_fact": [
            "🏖️ Хорошего отдыха\nНо перед этим нужно закрыть факт за вчера ☺️",
            "🌴 Отпуск это прекрасно\nОсталось только написать факт за вчера",
        ],
    }

    OFFICIAL = {
        "no_plan": ["План на сегодня отсутствует"],
        "no_fact": ["Факт за предыдущий день не зафиксирован"],
        "no_both": ["Отсутствует план на сегодня и факт за предыдущий день"],
        "vacation_no_fact": ["Не зафиксирован факт за предыдущий день"],
    }

    src = FRIENDLY if BOT_MODE == "friendly" else OFFICIAL
    return random.choice(src[case])

def find_record_row(records: list[dict], date_: str, user_id: int):
    """Возвращает (row_index_in_sheet, row_dict) или (None, None)"""
    for idx, r in enumerate(records, start=2):  # sheet rows start at 2 (headers at 1)
        if str(r.get("Date")) == date_ and int(r.get("User ID")) == int(user_id):
            return idx, r
    return None, None

def user_in_vacation_today(records: list[dict], user_id: int) -> bool:
    idx, r = find_record_row(records, today_str(), user_id)
    if not r:
        return False
    return str(r.get("Vacation", "")).strip().lower() == "vacation"

def has_plan_today(records: list[dict], user_id: int) -> bool:
    _, r = find_record_row(records, today_str(), user_id)
    return bool(r and str(r.get("Plan", "")).strip())

def has_fact_yesterday(records: list[dict], user_id: int) -> bool:
    _, r = find_record_row(records, yesterday_str(), user_id)
    return bool(r and str(r.get("Fact", "")).strip())

def ensure_users_columns_hint():
    """
    Users sheet expected columns (пример):
    - User ID (number)
    - Active (TRUE/FALSE or 1/0)
    - Chat ID (number)
    - Thread ID (number)  # optional
    """
    pass

# ========================
# THREAD CATCHER (сохраняет chat/thread для напоминаний)
# ========================
async def catch_thread(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    # Запоминаем thread_id, если он есть (topics). Если нет — всё равно сохраняем chat_id
    thread_id = getattr(msg, "message_thread_id", None)

    users = users_sheet.get_all_records()
    for idx, row in enumerate(users, start=2):
        try:
            if int(row.get("User ID")) == int(user.id):
                users_sheet.update_cell(idx, 3, chat.id)  # Chat ID (колонка C)
                users_sheet.update_cell(idx, 4, thread_id or "")  # Thread ID (колонка D)
                return
        except Exception:
            continue

# ========================
# /PLAN
# ========================
async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not (msg and user and chat):
        return

    # сохраняем переносы строк: берем весь текст сообщения, убираем только команду /plan
    text = (msg.text or "").replace("/plan", "", 1).strip()

    if not text:
        await msg.reply_text("❗️План не может быть пустым")
        return

    records = records_sheet.get_all_records()
    row_idx, existing = find_record_row(records, today_str(), user.id)

    if row_idx:
        records_sheet.update_cell(row_idx, 6, text)       # Plan
        records_sheet.update_cell(row_idx, 7, now_time_str())  # Plan time
    else:
        records_sheet.append_row([
            today_str(), chat.id, chat.title, user.id, user.username or "",
            text, now_time_str(), "", "", "active"
        ])

    await context.bot.set_message_reaction(chat.id, msg.message_id, "👍")

# ========================
# /FACT  (НОВАЯ ЛОГИКА)
# 1) если за ВЧЕРА есть запись и Fact пустой -> записываем туда
# 2) если за вчера Fact уже есть -> записываем в СЕГОДНЯ (обновляем или добавляем)
# ========================
async def fact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not (msg and user and chat):
        return

    text = (msg.text or "").replace("/fact", "", 1).strip()
    if not text:
        await msg.reply_text("❗️Факт не может быть пустым")
        return

    records = records_sheet.get_all_records()
    y = yesterday_str()
    t = today_str()

    # 1) пробуем закрыть вчера
    y_idx, y_row = find_record_row(records, y, user.id)

    if y_row and not str(y_row.get("Fact", "")).strip():
        # есть вчерашняя строка и факт пустой -> пишем туда
        records_sheet.update_cell(y_idx, 8, text)          # Fact
        records_sheet.update_cell(y_idx, 9, now_time_str())  # Fact time
    else:
        # 2) иначе пишем в сегодня (обновляем/добавляем)
        t_idx, t_row = find_record_row(records, t, user.id)

        if t_idx:
            records_sheet.update_cell(t_idx, 8, text)
            records_sheet.update_cell(t_idx, 9, now_time_str())
        else:
            # если строки за сегодня нет — создаем
            records_sheet.append_row([
                t, chat.id, chat.title, user.id, user.username or "",
                "", "", text, now_time_str(), "active"
            ])

    await context.bot.set_message_reaction(chat.id, msg.message_id, "👍")

# ========================
# MORNING CHECK (10:45 Prague) — РЕАЛЬНАЯ ЛОГИКА
# Правила из твоего ТЗ:
# 1) до 10:45 нет плана и нет факта за вчера, не в отпуске -> напоминание про оба
# 2) есть факт за вчера, нет плана, не в отпуске -> напоминание про план
# 3) нет факта за вчера, нет плана, но сегодня в отпуске -> напоминание только про факт
# 4) выходные -> не шлем
# ========================
async def send_reminder_to_user(app, user_row: dict, records: list[dict]):
    # Users sheet expected: User ID, Active, Chat ID, Thread ID
    try:
        uid = int(user_row.get("User ID"))
    except Exception:
        return

    active_val = user_row.get("Active")
    is_active = str(active_val).strip().lower() in ("true", "1", "yes", "y", "да")
    if not is_active:
        return

    chat_id = user_row.get("Chat ID") or user_row.get("ChatID") or user_row.get("Chat Id")
    thread_id = user_row.get("Thread ID") or user_row.get("ThreadID") or user_row.get("Thread Id")

    if not chat_id:
        return

    try:
        chat_id = int(chat_id)
    except Exception:
        return

    thread_id_int = None
    if thread_id not in (None, "", " "):
        try:
            thread_id_int = int(thread_id)
        except Exception:
            thread_id_int = None

    vacation_today = user_in_vacation_today(records, uid)
    plan_ok = has_plan_today(records, uid)
    fact_ok = has_fact_yesterday(records, uid)

    # вычисляем кейс по твоим правилам
    case = None
    if (not plan_ok) and (not fact_ok) and (not vacation_today):
        case = "no_both"
    elif (not plan_ok) and fact_ok and (not vacation_today):
        case = "no_plan"
    elif (not fact_ok) and (not plan_ok) and vacation_today:
        case = "vacation_no_fact"
    elif (not fact_ok) and plan_ok:
        case = "no_fact"
    elif (not fact_ok) and vacation_today:
        case = "vacation_no_fact"

    if not case:
        return

    # добавляем конкретику (без точек в конце)
    text = pick_message(case)
    lines = [text]

    if case in ("no_fact", "no_both", "vacation_no_fact"):
        lines.append(f"Факт нужен за {pretty_ddmm(yesterday_str())}")

    if case in ("no_plan", "no_both"):
        lines.append("План нужен за сегодня")

    send_kwargs = {"chat_id": chat_id, "text": "\n".join(lines)}
    if thread_id_int:
        send_kwargs["message_thread_id"] = thread_id_int

    await app.bot.send_message(**send_kwargs)

async def reminder_loop(app):
    last_run_date = None

    while True:
        try:
            now = datetime.now(tz)

            # выходные — вообще не шлем
            if is_weekend():
                await asyncio.sleep(60)
                continue

            # запуск ровно один раз в день в заданную минуту
            if now.hour == REMINDER_HOUR and now.minute == REMINDER_MINUTE:
                run_date = now.strftime("%Y-%m-%d")
                if last_run_date != run_date:
                    last_run_date = run_date

                    users = users_sheet.get_all_records()
                    records = records_sheet.get_all_records()

                    for u in users:
                        await send_reminder_to_user(app, u, records)

            await asyncio.sleep(20)

        except Exception as e:
            print("⚠️ reminder_loop error:", e)
            await asyncio.sleep(30)

# ========================
# TEST COMMANDS (ТОЛЬКО ТЕКСТЫ)
# ========================
async def send_case(update: Update, context: ContextTypes.DEFAULT_TYPE, case: str):
    msg = update.effective_message
    chat = update.effective_chat
    if not (msg and chat):
        return

    text = pick_message(case)

    kwargs = {"chat_id": chat.id, "text": text}
    if getattr(msg, "message_thread_id", None):
        kwargs["message_thread_id"] = msg.message_thread_id

    await context.bot.send_message(**kwargs)

async def no_plan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_case(update, context, "no_plan")

async def no_fact_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_case(update, context, "no_fact")

async def no_both_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_case(update, context, "no_both")

async def no_fact_vac_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_case(update, context, "vacation_no_fact")

# ========================
# START
# ========================
async def post_init(app):
    # фоновый запуск напоминаний
    app.create_task(reminder_loop(app))

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # основные команды
    app.add_handler(CommandHandler("plan", plan))
    app.add_handler(CommandHandler("fact", fact))

    # тестовые команды для текста
    app.add_handler(CommandHandler("no_plan", no_plan_cmd))
    app.add_handler(CommandHandler("no_fact", no_fact_cmd))
    app.add_handler(CommandHandler("no_both", no_both_cmd))
    app.add_handler(CommandHandler("no_fact_vac", no_fact_vac_cmd))

    # ловим сообщения чтобы запоминать chat/thread в Users (можно оставить)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, catch_thread))

    app.run_polling()

if __name__ == "__main__":
    main()
