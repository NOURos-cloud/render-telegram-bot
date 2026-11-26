import os
import threading
import time
import re
import glob

from flask import Flask
import requests
import telebot
from yt_dlp import YoutubeDL

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# YouTube cookies (optional, from Railway Variable YOUTUBE_COOKIES):
# paste the contents of a youtube.com cookies.txt export here as a secret.
# yt-dlp uses them to pass YouTube's "Sign in to confirm you're not a bot"
# check, which blocks datacenter IPs like Railway's. Never commit cookies.
_COOKIES_PATH = os.path.join(DOWNLOAD_DIR, "youtube_cookies.txt")
if os.environ.get("YOUTUBE_COOKIES"):
    with open(_COOKIES_PATH, "w") as _f:
        _f.write(os.environ["YOUTUBE_COOKIES"])
    print("YouTube cookies loaded from YOUTUBE_COOKIES", flush=True)
elif os.path.exists(_COOKIES_PATH):
    print("YouTube cookies loaded from file", flush=True)
else:
    _COOKIES_PATH = None
    print("No YouTube cookies configured", flush=True)

URL_RE = re.compile(r"https?://\S+")


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
        "ابعت أي لينك فيديو من أي موقع، وهحمله وابعتهولك هنا.\n"
        "مثال: ابعت لينك فيديو، واستنى شوية لحد ما يتحمل.\n"
        "ملحوظة: الفيديوهات الكبيرة أكتر من 50 ميجا مش هتتبعت من التيليجرام.",
    )


def try_cobalt(url):
    """Fallback for YouTube: try keyless cobalt instances in order.
    api.cobalt.tools now needs a JWT/API key, so try community instances.
    Returns (filename, title) or None."""
    instances = [
        "https://dwnld.nichind.dev",
        "https://cobaltapi.kittycat.boo",
        "https://api.dl.woof.monster",
        "https://cobaltapi.squair.xyz",
        "https://lime.clxxped.lol",
    ]
    for base in instances:
        try:
            r = requests.post(
                base + "/",
                json={
                    "url": url,
                    "videoQuality": "720",
                    "filenameStyle": "basic",
                    "downloadMode": "auto",
                    "youtubeVideoCodec": "h264",
                },
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=30,
            )
            data = r.json()
            status = data.get("status")
            if status == "picker":
                items = data.get("picker") or []
                if not items:
                    continue
                dl_url = items[0].get("url")
                filename = items[0].get("filename") or "video.mp4"
            elif status in ("tunnel", "redirect"):
                dl_url = data.get("url")
                filename = data.get("filename") or "video.mp4"
            else:
                continue
            if not dl_url:
                continue
            filename = os.path.join(DOWNLOAD_DIR, os.path.basename(filename))
            with requests.get(dl_url, stream=True, timeout=120) as resp:
                resp.raise_for_status()
                with open(filename, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
            title = os.path.splitext(os.path.basename(filename))[0]
            return filename, title
        except Exception:
            continue
    return None


def send_file(chat_id, filename, title):
    with open(filename, "rb") as f:
        if filename.lower().endswith((".mp4", ".mov", ".webm", ".mkv")):
            bot.send_video(chat_id, f, caption=(title or "")[:200])
        else:
            bot.send_document(chat_id, f, caption=(title or "")[:200])


@bot.message_handler(func=lambda message: True)
def download_handler(message):
    text = message.text or ""
    match = URL_RE.search(text)
    if not match:
        bot.reply_to(message, "ابعت لينك صحيح.")
        return
    url = match.group(0)

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
        "socket_timeout": 30,
        **({"cookiefile": _COOKIES_PATH} if _COOKIES_PATH else {}),
        # YouTube blocks datacenter IPs with "Sign in to confirm you're not a bot".
        # Forcing android/ios alone makes it worse with current yt-dlp (needs PO token).
        # tv + web_embedded + android_vr is the most reliable combo in 2026.
        "extractor_args": {
            "youtube": {
                "player_client": ["tv", "web_embedded", "android_vr", "tv_downgraded", "web"],
                "player_skip": ["webpage"],
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
        send_file(message.chat.id, filename, info.get("title", ""))
        bot.reply_to(message, "تم ✅")
    except Exception as e:
        # YouTube bot-check: fall back to cobalt API silently (no message).
        if "youtube.com" in url or "youtu.be" in url:
            result = try_cobalt(url)
            if result:
                filename, title = result
                try:
                    if os.path.getsize(filename) > 50 * 1024 * 1024:
                        bot.reply_to(message, "الفيديو كبير أكتر من 50 ميجا، والتيليجرام مش هيقبله.")
                        return
                    send_file(message.chat.id, filename, title)
                    bot.reply_to(message, "تم ✅")
                    return
                except Exception as e2:
                    bot.reply_to(message, "معرفتش أحمل الفيديو ده، جرب لينك تاني.")
                    return
        bot.reply_to(message, "معرفتش أحمل الفيديو ده، جرب لينك تاني.")
    finally:
        for path in glob.glob(os.path.join(DOWNLOAD_DIR, "*")):
            try:
                os.remove(path)
            except OSError:
                pass


def run_bot():
    # If polling dies (e.g. Telegram 409 Conflict when an old instance is
    # still shutting down during a redeploy), retry instead of leaving
    # Flask alive with a dead bot thread and no replies at all.
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=60)
        except Exception as e:
            print(f"Polling stopped: {e}, retrying in 5s...", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
