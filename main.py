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

# YouTube cookies (optional, from Railway Variables YOUTUBE_COOKIES and
# YOUTUBE_COOKIES_2): paste the contents of a youtube.com cookies.txt
# export here as a secret. yt-dlp uses them to pass YouTube's
# "Sign in to confirm you're not a bot" check, which blocks datacenter
# IPs like Railway's. The bot tries the first set, and if YouTube rejects
# it with a sign-in error it automatically tries the second set (exported
# from a different Google account). Never commit cookies.
def _load_cookies(env_name, filename):
    path = os.path.join(DOWNLOAD_DIR, filename)
    if os.environ.get(env_name):
        with open(path, "w") as _f:
            _f.write(os.environ[env_name])
        print(f"Cookies loaded from {env_name}", flush=True)
        return path
    if os.path.exists(path):
        print(f"Cookies loaded from file {filename}", flush=True)
        return path
    return None


_COOKIES_PATH = _load_cookies("YOUTUBE_COOKIES", "youtube_cookies.txt")
_COOKIES_PATH_2 = _load_cookies("YOUTUBE_COOKIES_2", "youtube_cookies2.txt")
if not _COOKIES_PATH:
    print("No YouTube cookies configured", flush=True)

# Instagram cookies (optional, Railway Variable INSTAGRAM_COOKIES):
# same idea as YouTube - Instagram's API refuses unauthenticated
# requests from datacenter IPs, so a logged-in session makes reels
# download reliably. Export instagram.com cookies.txt the same way
# (Kiwi + Get cookies.txt LOCALLY) and paste as a secret.
_IG_COOKIES_PATH = _load_cookies("INSTAGRAM_COOKIES", "instagram_cookies.txt")
if not _IG_COOKIES_PATH:
    print("No Instagram cookies configured", flush=True)

# Admin chat for failure alerts (optional, Railway Variable ADMIN_CHAT_ID):
# when YouTube downloads fail with a sign-in error (cookies dead), the bot
# sends a short alert here so the cookies can be renewed quickly.
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")

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


def _is_cookie_failure(exc):
    msg = str(exc).lower()
    return "sign in to confirm" in msg or "confirm you're not a bot" in msg


def _notify_admin(text):
    if not ADMIN_CHAT_ID:
        return
    try:
        bot.send_message(int(ADMIN_CHAT_ID), text)
    except Exception:
        pass


def _clean_download_dir():
    for path in glob.glob(os.path.join(DOWNLOAD_DIR, "*")):
        try:
            os.remove(path)
        except OSError:
            pass


def _is_transient_error(exc):
    msg = str(exc).lower()
    return any(s in msg for s in (
        "timed out", "timeout", "connection", "temporarily",
        "429", "too many requests", "502", "503", "500",
        "not granting access", "try again",
    ))


def _download_with_ytdlp(url, cookiefile):
    """Download with yt-dlp using one cookie file (or None).
    Returns (filename, info). Raises on failure.
    Retries transient errors (timeouts, rate limits, 5xx) up to 3 times,
    because Instagram/TikTok often fail once and succeed on retry."""
    outtmpl = os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s")
    ydl_opts = {
        "format": "best[ext=mp4][filesize<=45M]/best[filesize<=45M]/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "noplaylist": True,
        "merge_output_format": "mp4",
        "max_filesize": 50 * 1024 * 1024,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "socket_timeout": 30,
        **({"cookiefile": cookiefile} if cookiefile else {}),
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
    last_exc = None
    for attempt in range(3):
        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
            if not os.path.exists(filename):
                matches = glob.glob(os.path.join(DOWNLOAD_DIR, "*"))
                filename = max(matches, key=os.path.getctime) if matches else None
            return filename, info
        except Exception as e:
            last_exc = e
            if attempt < 2 and _is_transient_error(e):
                time.sleep(3 * (attempt + 1))
                continue
            raise
    raise last_exc


@bot.message_handler(func=lambda message: True)
def download_handler(message):
    text = message.text or ""
    match = URL_RE.search(text)
    if not match:
        bot.reply_to(message, "ابعت لينك صحيح.")
        return
    url = match.group(0)

    # YouTube Music links fail with 403 even with cookies; the same video
    # works on regular YouTube, so rewrite the host before downloading.
    if "music.youtube.com" in url:
        url = url.replace("music.youtube.com", "www.youtube.com", 1)

    # TikTok photo posts are image slideshows, not videos, and yt-dlp
    # cannot download them at all. Say so plainly instead of failing.
    if "tiktok.com" in url and "/photo/" in url:
        bot.reply_to(message, "اللينك ده صور مش فيديو، والبوت بينزل فيديوهات بس.")
        return

    bot.reply_to(message, "بحمل الفيديو... استنى شوية ⏳")
    is_youtube = "youtube.com" in url or "youtu.be" in url
    is_instagram = "instagram.com" in url
    # Cookie sets to try in order (YouTube only). Instagram uses its own
    # session cookies when configured, otherwise no cookies.
    if is_youtube:
        cookie_files = [_p for _p in (_COOKIES_PATH, _COOKIES_PATH_2) if _p]
    elif is_instagram and _IG_COOKIES_PATH:
        cookie_files = [_IG_COOKIES_PATH]
    else:
        cookie_files = []
    if not cookie_files:
        cookie_files = [None]
    filename = None
    info = None
    cookie_dead = False
    try:
        for _idx, _cookie in enumerate(cookie_files):
            try:
                filename, info = _download_with_ytdlp(url, _cookie)
                break
            except Exception as e:
                if is_youtube and _is_cookie_failure(e) and _idx < len(cookie_files) - 1:
                    # This cookie set is dead; clean up and try the next one.
                    _clean_download_dir()
                    continue
                raise
        if not filename or not os.path.exists(filename):
            bot.reply_to(message, "معرفتش أحمل الفيديو. جرب لينك تاني.")
            return
        size = os.path.getsize(filename)
        if size > 50 * 1024 * 1024:
            bot.reply_to(message, "الفيديو كبير أكتر من 50 ميجا، والتيليجرام مش هيقبله.")
            return
        send_file(message.chat.id, filename, (info or {}).get("title", ""))
        bot.reply_to(message, "تم ✅")
    except Exception as e:
        # Log the real error so Railway logs show what actually failed.
        print(f"[download] failed: {url} -> {e}", flush=True)
        # YouTube bot-check: fall back to cobalt API silently (no message).
        if is_youtube:
            cookie_dead = _is_cookie_failure(e)
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
        if is_youtube and cookie_dead:
            _notify_admin("⚠️ تنبيه: تحميل اليوتيوب فشل، الكوكيز غالباً وقفت. جدّدها من Kiwi وحدّث YOUTUBE_COOKIES في Railway.")
        bot.reply_to(message, "معرفتش أحمل الفيديو ده، جرب لينك تاني.")
    finally:
        # Remove only this download's file (and any partial file), not the
        # whole folder, so a simultaneous download is never wiped.
        for _p in (filename, (filename + ".part") if filename else None):
            if _p and os.path.exists(_p):
                try:
                    os.remove(_p)
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
