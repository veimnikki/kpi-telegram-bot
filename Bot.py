import os
import json
import random
import asyncio
from dataclasses import dataclass
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
REMINDER_MINUTE = int(os.getenv("REMINDER_MINUTE", "45"))  # window: minute-1..minute+1

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
    return now_dt().weekday() >= 5

def last_workday_str() -> str:
    """
    Mon -> Fri (minus 3 days)
    Tue-Fri -> previous day
    Sat/Sun -> previous day fallback (we skip weekends anyway)
    """
    d = now_dt().date()
    wd = d.weekday()
    if wd == 0:          # Mon
        d -= timedelta(days=3)
    elif wd == 6:        # Sun
        d -= timedelta(days=2)
    elif wd == 5:        # Sat
        d -= timedelta(days=1)
    else:                # Tue-Fri
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")

def pretty_ddmm(d_yyyy_mm_dd: str) -> str:
    return datetime.strptime(d_yyyy_mm_dd, "%Y-%m-%d").strftime("%d.%m")

# =========================================================
# SMALL UTILS
# =========================================================
def safe_int(x, default=0) -> int:
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
    return safe_int(x, default=0)

# =========================================================
# SAFE SHEET LAYER (NO get_all_records() CRASH)
# =========================================================
def ws_values(ws) -> List[List[str]]:
    return ws.get_all_values() or []

def ws_headers(ws) -> List[str]:
    values = ws_values(ws)
    if not values:
        return []
    return [str(h).strip() for h in values[0]]

def headers_map(ws) -> Dict[str, int]:
    hs = ws_headers(ws)
    # keep duplicates? we ignore empty headers, but keep first occurrence
    hm = {}
    for i, h in enumerate(hs, start=1):
        h = (h or "").strip()
        if not h:
            continue
        if h not in hm:
            hm[h] = i
    return hm

def safe_table(ws) -> Tuple[List[str], List[Dict[str, str]]]:
    """
    Returns (headers, rows_as_dict)
    - never raises because of duplicate headers
    - empty headers are ignored; still accessible by position via hm if needed
    """
    values = ws_values(ws)
    if not values:
        return [], []
    headers = [str(h).strip() for h in values[0]]
    rows = []
    for r in values[1:]:
        row = {}
        for i in range(len(headers)):
            key = headers[i]
            if i < len(r):
                val = r[i]
            else:
                val = ""
            # store even empty header under special key? no.
            if key and key.strip():
                # if duplicated header, keep first, ignore next
                k = key.strip()
                if k not in row:
                    row[k] = val
            # ignore empty header columns in dict form
        rows.append(row)
    return headers, rows

def get_cell(ws, row: int, col: int) -> str:
    try:
        return ws.cell(row, col).value or ""
    except Exception:
        return ""

def update_cell(ws, row: int, col: int, value: Any):
    ws.update_cell(row, col, "" if value is None else str(value))

def append_row(ws, row_values: List[Any]):
    ws.append_row([("" if v is None else v) for v in row_values])

# =========================================================
# REQUIRED COLUMNS VALIDATION (SO YOU SEE WHAT'S WRONG FAST)
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
ADMINS_REQUIRED = ["Admin user ID", "Username", "Chat ID", "Thread ID"]
RECORDS_REQUIRED = [
    "Date",
    "Chat ID", "Chat name",
    "Thread ID", "Thread name",
    "User ID", "Username",
    "Plan", "Plan time",
    "Fact", "Fact time",
    "Vacation"
]

def ensure_columns(ws, required: List[str]):
    hm = headers_map(ws)
    missing = [c for c in required if c not in hm]
    if missing:
        raise RuntimeError(f"Sheet '{ws.title}' missing columns: {missing}")

# Validate once at startup (fail fast)
ensure_columns(users_sheet, USERS_REQUIRED)
ensure_columns(chats_sheet, CHATS_REQUIRED)
ensure_columns(admins_sheet, ADMINS_REQUIRED)
ensure_columns(records_sheet, RECORDS_REQUIRED)

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
# ADMIN ACCESS (Admins sheet)
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
    returns list of (chat_id, thread_id) allowed for this admin.
    if admin has empty chat_id => 'ALL'
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

def admin_main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Отчет", callback_data="admin:report")],
        [InlineKeyboardButton("ℹ️ Помощь", callback_data="admin:help")],
    ])

# =========================================================
# CHATS REGISTRY (Chats sheet)
# Chats headers:
# Chat ID | Chat name | Thread ID | Thread name | Team | Active
# =========================================================
def upsert_chat(chat, thread_id: int):
    if not chat or chat.type == "private":
        return

    hm = headers_map(chats_sheet)
    _, rows = safe_table(chats_sheet)

    for idx, r in enumerate(rows, start=2):
        if safe_int(r.get("Chat ID")) == int(chat.id) and normalize_thread_id(r.get("Thread ID")) == int(thread_id):
            # update Chat name; do not touch Thread name/team/active
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
        if safe_int(r.get("Chat ID")) == int(chat_id) and normalize_thread_id(r.get("Thread ID")) == int(thread_id):
            return norm_bool(r.get("Active", "TRUE"))
    return True

def chat_label(chats_rows: List[Dict[str, str]], chat_id: int, thread_id: int) -> str:
    chat_name = ""
    thread_name = ""
    team = ""

    for r in chats_rows:
        if safe_int(r.get("Chat ID")) == int(chat_id) and normalize_thread_id(r.get("Thread ID")) == int(thread_id):
            chat_name = str(r.get("Chat name", "")).strip()
            thread_name = str(r.get("Thread name", "")).strip()
            team = str(r.get("Team", "")).strip()
            break

    base = chat_name or str(chat_id)
    if thread_id:
        base = f"{base} / thread {thread_id}"
        if thread_name:
            base = f"{base} — {thread_name}"
    if team:
        base = f"{base} — {team}"
    return base

# =========================================================
# USERS (ONLY on /plan)
# Users headers:
# User ID | Username | Name | Chat ID | Chat name | Thread ID | Thread name | Team | Active | Mode
# =========================================================
def upsert_user_binding(user, chat, thread_id: int):
    if not user or not chat:
        return

    hm = headers_map(users_sheet)
    _, rows = safe_table(users_sheet)

    uid = int(user.id)
    cid = int(chat.id)
    tid = int(thread_id or 0)

    username = user.username or ""
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()

    for idx, r in enumerate(rows, start=2):
        if (
            safe_int(r.get("User ID")) == uid
            and safe_int(r.get("Chat ID")) == cid
            and normalize_thread_id(r.get("Thread ID")) == tid
        ):
            # update identity + chat name (because chat can be renamed)
            update_cell(users_sheet, idx, hm["Username"], username)
            update_cell(users_sheet, idx, hm["Name"], name)
            update_cell(users_sheet, idx, hm["Chat name"], chat.title or "")
            # defaults if empty
            if not str(r.get("Active", "")).strip():
                update_cell(users_sheet, idx, hm["Active"], "TRUE")
            if not str(r.get("Mode", "")).strip():
                update_cell(users_sheet, idx, hm["Mode"], DEFAULT_BOT_MODE)
            return

    # new row
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
# RECORDS (ONE ROW PER day+chat+thread+user)
# Records headers recommended:
# Date | Chat ID | Chat name | Thread ID | Thread name | User ID | Username
# | Plan | Plan time | Fact | Fact time | Vacation
# =========================================================
def find_record_row_idx(date_: str, user_id: int, chat_id: int, thread_id: int) -> Optional[int]:
    _, rows = safe_table(records_sheet)
    for idx, r in enumerate(rows, start=2):
        if (
            str(r.get("Date")) == str(date_)
            and safe_int(r.get("User ID")) == int(user_id)
            and safe_int(r.get("Chat ID")) == int(chat_id)
            and normalize_thread_id(r.get("Thread ID")) == int(thread_id)
        ):
            return idx
    return None

def ensure_record(date_: str, chat, user, thread_id: int) -> int:
    idx = find_record_row_idx(date_, user.id, chat.id, thread_id)
    if idx:
        # keep Chat name updated
        hm = headers_map(records_sheet)
        update_cell(records_sheet, idx, hm["Chat name"], chat.title or "")
        return idx

    hm = headers_map(records_sheet)
    # Create new row for this unique key
    append_row(records_sheet, [
        date_,
        chat.id,
        chat.title or "",
        thread_id if thread_id != 0 else "",
        "",              # Thread name
        user.id,
        user.username or "",
        "", "",          # Plan, Plan time
        "", "",          # Fact, Fact time
        "active",        # Vacation
    ])
    # return last row index
    values = ws_values(records_sheet)
    return len(values)

def get_record_field(date_: str, user_id: int, chat_id: int, thread_id: int, field: str) -> str:
    _, rows = safe_table(records_sheet)
    for r in rows:
        if (
            str(r.get("Date")) == str(date_)
            and safe_int(r.get("User ID")) == int(user_id)
            and safe_int(r.get("Chat ID")) == int(chat_id)
            and normalize_thread_id(r.get("Thread ID")) == int(thread_id)
        ):
            return str(r.get(field, "") or "").strip()
    return ""

def has_plan_today(records_rows, uid, cid, tid) -> bool:
    return bool(get_record_field(today_str(), uid, cid, tid, "Plan"))

def has_fact_last_workday(records_rows, uid, cid, tid) -> bool:
    return bool(get_record_field(last_workday_str(), uid, cid, tid, "Fact"))

def in_vacation_today(records_rows, uid, cid, tid) -> bool:
    v = get_record_field(today_str(), uid, cid, tid, "Vacation").lower()
    return v == "vacation"

# =========================================================
# GROUP ACTIVITY CATCHER
# IMPORTANT:
# - ONLY Chats is auto-filled
# - Users appears ONLY when user uses /plan in that thread
# =========================================================
async def catch_group_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or chat.type == "private":
        return
    tid = normalize_thread_id(getattr(msg, "message_thread_id", 0))
    try:
        upsert_chat(chat, tid)
    except Exception as e:
        print("⚠️ upsert_chat error:", e)

# =========================================================
# /PLAN — activation + write plan
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

    tid = normalize_thread_id(getattr(msg, "message_thread_id", 0))

    # register chat+thread
    try:
        upsert_chat(chat, tid)
    except Exception as e:
        print("⚠️ upsert_chat in /plan error:", e)

    # activate user binding ONLY HERE
    try:
        upsert_user_binding(user, chat, tid)
    except Exception as e:
        print("⚠️ upsert_user_binding in /plan error:", e)
        await msg.reply_text("⚠️ Не смог записать тебя в Users. Проверь заголовки Users.")
        return

    # records: ensure strict row + update fields
    idx = ensure_record(today_str(), chat, user, tid)
    hm = headers_map(records_sheet)
    update_cell(records_sheet, idx, hm["Plan"], text)
    update_cell(records_sheet, idx, hm["Plan time"], now_time_str())

    try:
        await context.bot.set_message_reaction(chat.id, msg.message_id, "👍")
    except Exception:
        pass

# =========================================================
# /FACT — writes to last_workday row (same chat+thread+user)
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

    tid = normalize_thread_id(getattr(msg, "message_thread_id", 0))
    d = last_workday_str()

    idx = ensure_record(d, chat, user, tid)
    hm = headers_map(records_sheet)
    update_cell(records_sheet, idx, hm["Fact"], text)
    update_cell(records_sheet, idx, hm["Fact time"], now_time_str())

    try:
        await context.bot.set_message_reaction(chat.id, msg.message_id, "👍")
    except Exception:
        pass

# =========================================================
# /VACATION — buttons then callback edits message and updates one row
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

    tid = normalize_thread_id(getattr(msg, "message_thread_id", 0))

    idx = ensure_record(today_str(), chat, user, tid)
    hm = headers_map(records_sheet)

    if q.data == "vac:on":
        update_cell(records_sheet, idx, hm["Vacation"], "vacation")
        text_after = "🌴 Хорошего отдыха! Отпуск включён ✅"
    else:
        update_cell(records_sheet, idx, hm["Vacation"], "active")
        text_after = "🧑‍💼 Добро пожаловать обратно в строй! Работа включена ✅"

    # remove buttons by editing the same message
    try:
        await q.message.edit_text(text_after, reply_markup=None)
    except Exception as e:
        print("⚠️ edit_message error:", e)
        # fallback: send new
        try:
            await q.message.reply_text(text_after)
        except Exception:
            pass

    await q.answer("Готово ✅")

# =========================================================
# PRIVATE ENTRY / START
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
# ADMIN PANEL HELPERS
# =========================================================
def admin_scope_allows(admin_id: int, cid: int, tid: int) -> bool:
    scopes = admin_scopes(admin_id)
    if scopes == ["ALL"]:
        return True
    return (cid, tid) in set(scopes)

def admin_get_chats_for_admin(admin_id: int) -> List[Tuple[int, int]]:
    _, chats_rows = safe_table(chats_sheet)
    scopes = admin_scopes(admin_id)

    pairs = []
    for r in chats_rows:
        cid = safe_int(r.get("Chat ID"))
        tid = normalize_thread_id(r.get("Thread ID"))
        if scopes == ["ALL"]:
            pairs.append((cid, tid))
        else:
            if (cid, tid) in set(scopes):
                pairs.append((cid, tid))

    # unique + stable
    seen = set()
    out = []
    for p in pairs:
        if p not in seen and p[0] != 0:
            seen.add(p)
            out.append(p)
    return out

def build_chat_buttons(admin_id: int) -> InlineKeyboardMarkup:
    _, chats_rows = safe_table(chats_sheet)
    pairs = admin_get_chats_for_admin(admin_id)

    buttons = []
    for (cid, tid) in pairs[:40]:
        label = chat_label(chats_rows, cid, tid)
        buttons.append([InlineKeyboardButton(f"📌 {label}", callback_data=f"admin:chat:{cid}:{tid}")])

    if not buttons:
        buttons = [[InlineKeyboardButton("ℹ️ Нет чатов", callback_data="admin:help")]]

    return InlineKeyboardMarkup(buttons)

def team_users_for_thread(cid: int, tid: int) -> List[Dict[str, str]]:
    _, users_rows = safe_table(users_sheet)
    return [
        u for u in users_rows
        if safe_int(u.get("Chat ID")) == cid
        and normalize_thread_id(u.get("Thread ID")) == tid
        and norm_bool(u.get("Active", "TRUE"))
    ]

# =========================================================
# ADMIN CALLBACK
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

    # REPORT LIST
    if data == "admin:report":
        kb = build_chat_buttons(user.id)
        await q.message.reply_text("Выбери чат/ветку для отчёта:", reply_markup=kb)
        await q.answer()
        return

    # CHAT REPORT
    if data.startswith("admin:chat:"):
        try:
            _, _, cid_s, tid_s = data.split(":", 3)
            cid = safe_int(cid_s)
            tid = safe_int(tid_s)
        except Exception:
            await q.answer("Bad data")
            return

        if not admin_scope_allows(user.id, cid, tid):
            await q.answer("Нет доступа к этому чату")
            return

        _, chats_rows = safe_table(chats_sheet)
        _, records_rows = safe_table(records_sheet)
        _, users_rows = safe_table(users_sheet)

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
            if in_vacation_today(records_rows, uid, cid, tid):
                vac_count += 1
                continue

            if has_plan_today(records_rows, uid, cid, tid):
                plan_ok += 1
            else:
                missing_plan.append(urow)

            if has_fact_last_workday(records_rows, uid, cid, tid):
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

        # vacation toggles for up to 10 users (prioritize missing)
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

        # mode menus
        buttons.append([InlineKeyboardButton("🙂 Mode: изменить для команды", callback_data=f"admin:mode_team:{cid}:{tid}")])
        buttons.append([InlineKeyboardButton("🙂 Mode: изменить для сотрудника", callback_data=f"admin:mode_user_pick:{cid}:{tid}")])
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

        # Ensure today's record exists for that user in that chat/thread
        # We need a "fake" user object? We'll update by row index directly.
        idx = find_record_row_idx(today_str(), uid, cid, tid)

        hm = headers_map(records_sheet)
        if idx:
            update_cell(records_sheet, idx, hm["Vacation"], status)
        else:
            # Need chat name and username for row
            _, chats_rows = safe_table(chats_sheet)
            _, users_rows = safe_table(users_sheet)

            chat_name = ""
            thread_name = ""
            for r in chats_rows:
                if safe_int(r.get("Chat ID")) == cid and normalize_thread_id(r.get("Thread ID")) == tid:
                    chat_name = str(r.get("Chat name", "")).strip()
                    thread_name = str(r.get("Thread name", "")).strip()
                    break

            username = ""
            for urow in users_rows:
                if safe_int(urow.get("User ID")) == uid and safe_int(urow.get("Chat ID")) == cid and normalize_thread_id(urow.get("Thread ID")) == tid:
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
                "", "", "", "", status
            ])

        await q.answer("Готово ✅")
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
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:chat:{cid}:{tid}")],
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
            if safe_int(r.get("Chat ID")) == cid and normalize_thread_id(r.get("Thread ID")) == tid:
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

        buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"admin:chat:{cid}:{tid}")])
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
            if (
                safe_int(r.get("User ID")) == uid
                and safe_int(r.get("Chat ID")) == cid
                and normalize_thread_id(r.get("Thread ID")) == tid
            ):
                update_cell(users_sheet, idx, hm["Mode"], mode)
                await q.answer("Готово ✅")
                return

        await q.answer("Пользователь не найден в Users (нужен /plan в этой ветке)")
        return

    await q.answer("Неизвестная команда")

# =========================================================
# REMINDERS LOOP
# - window 10:44–10:46
# - Mon checks fact for Friday via last_workday_str()
# - respects:
#   Chats.Active per (chat_id, thread_id)
#   Users.Active per (user_id, chat_id, thread_id)
#   Records.Vacation == vacation for today
#   Users.Mode
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
                    _, records_rows = safe_table(records_sheet)

                    lw = last_workday_str()

                    for u in users_rows:
                        uid = safe_int(u.get("User ID"))
                        if not uid:
                            continue

                        if not norm_bool(u.get("Active", "TRUE")):
                            continue

                        cid = safe_int(u.get("Chat ID"))
                        tid = normalize_thread_id(u.get("Thread ID"))
                        if not cid:
                            continue

                        # chat/thread inactive?
                        if not chat_is_active(chats_rows, cid, tid):
                            continue

                        # vacation today?
                        if in_vacation_today(records_rows, uid, cid, tid):
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
# START
# =========================================================
async def post_init(app):
    asyncio.create_task(reminder_loop(app))

def main():
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

    # SERVICE: fill Chats for any group activity
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.ChatType.PRIVATE, catch_group_activity),
        group=0
    )

    # COMMANDS (work chats)
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
