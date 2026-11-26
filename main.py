import os
import threading

from flask import Flask
import telebot

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)


@app.get("/")
def keep_alive():
    return "OK", 200


@app.get("/health")
def health():
    return {"status": "ok"}, 200


@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    bot.reply_to(message, "البوت يعمل ✅")


@bot.message_handler(func=lambda message: True)
def echo_all(message):
    bot.reply_to(message, message.text)


def run_bot():
    bot.infinity_polling(skip_pending=True, timeout=60)


if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
