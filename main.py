import os
import threading
import re
import glob

from flask import Flask
import telebot
from yt_dlp import YoutubeDL

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

URL_RE = re.compile(r"https?://\S+")

SUPPORTED = ("youtube.com", "youtu.be", "tiktok.com", "instagram.com")


@app.get("/")
def keep_alive():
    return "OK", 200


@app.get("/health")
def health():
    return {"status": "ok"}, 200


@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    bot.reply_to(
        message,
        "ابعت لينك من يوتيوب أو تيك توك أو انستجرام، وهحمله وابعتهولك هنا.\n"
        "مثال: ابعت لينك فيديو، واستنى شوية لحد ما يتحمل.\n"
        "ملحوظة: الفيديوهات الكبيرة أكتر من 50 ميجا مش هتتبعت من التيليجرام.",
    )


def is_supported(url):
    return any(host in url for host in SUPPORTED)


@bot.message_handler(func=lambda message: True)
def download_handler(message):
    text = message.text or ""
    match = URL_RE.search(text)
    if not match:
        bot.reply_to(message, "ابعت لينك صحيح من يوتيوب أو تيك توك أو انستجرام.")
        return
    url = match.group(0)
    if not is_supported(url):
        bot.reply_to(message, "اللينك ده مش مدعوم. ابعت لينك من يوتيوب أو تيك توك أو انستجرام.")
        return

    bot.reply_to(message, "بحمل الفيديو... استنى شوية ⏳")
    outtmpl = os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s")
    ydl_opts = {
        "format": "best[ext=mp4][filesize<=45M]/best[filesize<=45M]/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "max_filesize": 50 * 1024 * 1024,
        "geo_bypass": True,
        "no_check_certificate": True,
        # YouTube blocks datacenter IPs with "Sign in to confirm you're not a bot".
        # The Android/iOS player clients usually bypass that check without cookies.
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "web"],
                "player_skip": ["webpage", "configs"],
            }
        },
    }
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
        if not os.path.exists(filename):
            matches = glob.glob(os.path.join(DOWNLOAD_DIR, "*"))
            filename = max(matches, key=os.path.getctime) if matches else None
        if not filename or not os.path.exists(filename):
            bot.reply_to(message, "معرفتش أحمل الفيديو. جرب لينك تاني.")
            return
        size = os.path.getsize(filename)
        if size > 50 * 1024 * 1024:
            bot.reply_to(message, "الفيديو كبير أكتر من 50 ميجا، والتيليجرام مش هيقبله.")
            return
        with open(filename, "rb") as f:
            if filename.lower().endswith((".mp4", ".mov", ".webm", ".mkv")):
                bot.send_video(message.chat.id, f, caption=info.get("title", "")[:200])
            else:
                bot.send_document(message.chat.id, f, caption=info.get("title", "")[:200])
        bot.reply_to(message, "تم ✅")
    except Exception as e:
        bot.reply_to(message, f"حصل خطأ في التحميل: {e}")
    finally:
        for path in glob.glob(os.path.join(DOWNLOAD_DIR, "*")):
            try:
                os.remove(path)
            except OSError:
                pass


def run_bot():
    bot.infinity_polling(skip_pending=True, timeout=60)


if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
