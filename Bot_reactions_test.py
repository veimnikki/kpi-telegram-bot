import os
from dotenv import load_dotenv
from telegram import Update, ReactionTypeEmoji
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")


async def react(update: Update, emoji: str, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    await context.bot.set_message_reaction(
        chat_id=msg.chat_id,
        message_id=msg.message_id,
        reaction=[ReactionTypeEmoji(emoji)]
    )


async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # никаких сообщений — только реакция
    await react(update, "👍", context)


async def fact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await react(update, "👍", context)


# тестовые реакции
async def test_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await react(update, "👀", context)


import pytz
from telegram.ext import ApplicationBuilder

def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .job_queue(None)   # ⬅️ ВАЖНО: отключаем JobQueue
        .build()
    )

    app.add_handler(CommandHandler("plan", plan))
    app.add_handler(CommandHandler("fact", fact))
    app.add_handler(CommandHandler("test", test_ok))

    print("🤖 Bot with native reactions started")
    app.run_polling()


if __name__ == "__main__":
    main()
