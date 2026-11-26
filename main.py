import os
import threading
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
        # YouTube bot-check: fall back to cobalt API, which needs no cookies.
        if "youtube.com" in url or "youtu.be" in url:
            bot.reply_to(message, "يوتيوب قفل التحميل المباشر، بجرب طريقة تانية... ⏳")
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
                    bot.reply_to(message, f"حصل خطأ في التحميل: {e2}")
                    return
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
