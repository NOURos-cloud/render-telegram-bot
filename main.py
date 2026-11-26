import os
import shutil
import subprocess
import sys
import threading
import time
import re
import glob

from flask import Flask
import requests
import telebot
from yt_dlp import YoutubeDL

# OpenCode's universal downloader (direct files + torrents + media).
# Imported defensively: if it ever fails to load, the main catch-all
# handler below keeps working and /dl just reports unavailable.
try:
    from cookies_vault import CookieVault
    from downloader import classify as classify_url
    from downloader import download as download_any
    from downloader import download_youtube as download_youtube_cookiefree
    _UNIVERSAL_DL = True
    _YT_COOKIEFREE = True
except Exception as _dl_err:
    print(f"Universal downloader unavailable: {_dl_err}", flush=True)
    CookieVault = None
    classify_url = None
    download_any = None
    download_youtube_cookiefree = None
    _UNIVERSAL_DL = False
    _YT_COOKIEFREE = False

# Cloudflare WARP egress (optional): when the host's datacenter IP is
# blocked by YouTube / Instagram geo-blocks, downloads go through
# WARP's SOCKS5 proxy instead. Defensive import - if warp.py is
# missing or broken, downloads just use direct egress as before.
try:
    from warp import ensure_warp as _ensure_warp
except Exception:
    _ensure_warp = None

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

# Cookie vault for the /dl command: seed it from the same YouTube cookie
# env vars, so /dl can use cookies for YouTube without a separate
# COOKIES_JSON variable. The vault file lives in /tmp and is re-seeded
# from env vars on every startup (Railway's filesystem is ephemeral).
_vault = None
if _UNIVERSAL_DL:
    try:
        _vault = CookieVault(path=os.path.join(DOWNLOAD_DIR, "vault.json"))
        for _name, _env in (("youtube1", "YOUTUBE_COOKIES"), ("youtube2", "YOUTUBE_COOKIES_2")):
            _content = os.environ.get(_env)
            if _content:
                try:
                    _vault.add(_name, _content, replace=True)
                    print(f"Vault seeded: {_name}", flush=True)
                except Exception as _e:
                    print(f"Vault seed {_name} failed: {_e}", flush=True)
    except Exception as _e:
        print(f"Cookie vault unavailable: {_e}", flush=True)
        _vault = None

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
        "لليوتيوب: ابعت اللينك عادي، بيشتغل من غير كوكيز دلوقتي، أو استخدم /yt <رابط يوتيوب>.\n"
        "للتورنت والملفات المباشرة: /dl <رابط> (ولليوتيوب بالكوكيز: /dlc <رابط>).\n"
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
    # "The page needs to be reloaded" is YouTube rejecting a stale
    # session without saying "sign in" outright - same remedy: try the
    # next cookie set, and alert the admin if all sets fail this way.
    return (
        "sign in to confirm" in msg
        or "confirm you're not a bot" in msg
        or "the page needs to be reloaded" in msg
    )


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


def _download_with_gallery_dl(url):
    """Second download tool (gallery-dl) for sites where yt-dlp fails.
    Works for Facebook, TikTok, Twitter/X and others; does NOT support
    YouTube or Instagram (Instagram needs a login). Returns
    (filename, title) or None. Merges separate video+audio with ffmpeg
    when gallery-dl splits them, and moves the result to DOWNLOAD_DIR."""
    outdir = os.path.join(DOWNLOAD_DIR, "gdl")
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir, exist_ok=True)
    _gdl = shutil.which("gallery-dl")
    _cmd = ([_gdl] if _gdl else [sys.executable, "-m", "gallery_dl"]) + [
        "--no-check-certificate", "-d", outdir, url,
    ]
    try:
        subprocess.run(_cmd, capture_output=True, text=True, timeout=180)
    except Exception:
        return None
    videos = []
    for _root, _dirs, _files in os.walk(outdir):
        for _fn in _files:
            if _fn.lower().endswith((".mp4", ".webm", ".mov", ".mkv")):
                videos.append(os.path.join(_root, _fn))
    if not videos:
        shutil.rmtree(outdir, ignore_errors=True)
        return None
    video = max(videos, key=os.path.getsize)
    base = os.path.splitext(video)[0]
    audio = None
    for _ext in (".m4a", ".mp3", ".opus", ".aac"):
        if os.path.exists(base + _ext):
            audio = base + _ext
            break
    if audio:
        merged = base + "_merged.mp4"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", video, "-i", audio, "-c", "copy", merged],
                capture_output=True, timeout=120, check=True,
            )
            if os.path.exists(merged) and os.path.getsize(merged) > 0:
                video = merged
        except Exception:
            pass
    title = os.path.splitext(os.path.basename(video))[0]
    dest = os.path.join(DOWNLOAD_DIR, os.path.basename(video))
    try:
        os.rename(video, dest)
    except OSError:
        dest = video
    shutil.rmtree(outdir, ignore_errors=True)
    return dest, title


def _download_with_ytdlp(url, cookiefile):
    """Download with yt-dlp using one cookie file (or None).
    Returns (filename, info). Raises on failure.
    Retries transient errors (timeouts, rate limits, 5xx) up to 4 times,
    because Instagram/TikTok often fail once and succeed on retry."""
    outtmpl = os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s")
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
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 8,
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
    # WARP egress (if up) for YouTube and Instagram only: YouTube's
    # datacenter-IP block and Instagram's US-IP geo-block both come
    # from the host's egress IP. TikTok/Facebook/SoundCloud are fine
    # direct, so they are left alone. Resolved once per download call,
    # not per retry attempt.
    if "youtube.com" in url or "youtu.be" in url or "instagram.com" in url:
        _warp_proxy = _ensure_warp() if _ensure_warp else None
        if _warp_proxy:
            print(f"[download] using WARP proxy: {_warp_proxy}", flush=True)
            ydl_opts["proxy"] = _warp_proxy
    last_exc = None
    for attempt in range(4):
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
            if attempt < 3 and _is_transient_error(e):
                time.sleep(5 * (2 ** attempt))
                continue
            raise
    raise last_exc


def _is_admin(chat_id):
    # Cookie management (/cookies, /addcookie) is admin-only. The admin is
    # the same ADMIN_CHAT_ID used for failure alerts.
    return bool(ADMIN_CHAT_ID) and str(chat_id) == str(ADMIN_CHAT_ID)


MAX_DL_FILES = 5
MAX_SEND_BYTES = 50 * 1024 * 1024


def _dl_progress_editor(chat_id, message_id):
    # Throttled progress display: update at most every 10% or 5 seconds,
    # so Telegram isn't spammed with edits.
    state = {"percent": -10, "at": 0.0}

    def update(done, total):
        percent = int(done * 100 / total) if total else 0
        now = time.monotonic()
        if percent < state["percent"] + 10 and now - state["at"] < 5:
            return
        state["percent"] = percent
        state["at"] = now
        try:
            bot.edit_message_text(f"جارٍ التحميل... {percent}%", chat_id, message_id)
        except Exception:
            pass

    return update


def _deliver_dl_files(message, files):
    # Send downloaded files (videos as video, the rest as documents),
    # biggest first, at most MAX_DL_FILES, skipping anything over 50MB.
    # Returns how many were sent. Deletes every file afterwards so the
    # server disk never fills up.
    sent = 0
    existing = [p for p in files if p and os.path.exists(p)]
    existing.sort(key=lambda p: os.path.getsize(p), reverse=True)
    try:
        for path in existing[:MAX_DL_FILES]:
            size = os.path.getsize(path)
            if size > MAX_SEND_BYTES:
                bot.reply_to(message, f"{os.path.basename(path)} أكبر من 50 ميجا، مش هيتبعت.")
                continue
            try:
                send_file(message.chat.id, path, os.path.splitext(os.path.basename(path))[0])
                sent += 1
            except Exception:
                bot.reply_to(message, f"تعذر إرسال {os.path.basename(path)}")
        if len(existing) > MAX_DL_FILES:
            bot.reply_to(message, f"اتبعت أول {MAX_DL_FILES} ملفات من {len(existing)}.")
    finally:
        for path in existing:
            try:
                os.remove(path)
            except OSError:
                pass
    return sent


@bot.message_handler(commands=["dl", "dlc"])
def handle_dl(message):
    if not _UNIVERSAL_DL:
        bot.reply_to(message, "التحميل المتقدم مش متاح دلوقتي.")
        return
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "الاستخدام: /dl <رابط>")
        return
    url = parts[1].strip()
    with_cookies = parts[0].lower().endswith("dlc")
    # Same normalisations as the main handler.
    if "music.youtube.com" in url:
        url = url.replace("music.youtube.com", "www.youtube.com", 1)
    if "tiktok.com" in url and "/photo/" in url:
        bot.reply_to(message, "اللينك ده صور مش فيديو، والبوت بينزل فيديوهات بس.")
        return
    backend = classify_url(url) if classify_url else "media"
    is_instagram = "instagram.com" in url
    is_youtube = "youtube.com" in url or "youtu.be" in url
    notice = bot.reply_to(message, "جارٍ التحميل... ⏳")
    # A private output dir per call, so a failed /dl never leaves files
    # behind and never touches another download in progress.
    dl_dir = os.path.join(DOWNLOAD_DIR, f"dl-{message.chat.id}-{int(time.time())}")
    os.makedirs(dl_dir, exist_ok=True)
    try:
        files = None
        # Retry transient errors once (not for torrents: restarting a
        # torrent download from scratch is not a retry).
        attempts = 1 if backend == "torrent" else 2
        for _a in range(attempts):
            try:
                result = download_any(
                    url,
                    vault=_vault,
                    output_dir=dl_dir,
                    allow_cookie_fallback=with_cookies,
                    progress=_dl_progress_editor(message.chat.id, notice.message_id),
                )
                files = result.files
                break
            except Exception as e:
                if _a < attempts - 1 and _is_transient_error(e):
                    time.sleep(5)
                    continue
                raise
        if not files:
            raise RuntimeError("no files were downloaded")
        sent = _deliver_dl_files(message, files)
        if sent:
            bot.edit_message_text(f"تم ✅ ({backend})", message.chat.id, notice.message_id)
        else:
            bot.edit_message_text("معرفتش أحمل الفيديو ده، جرب لينك تاني.", message.chat.id, notice.message_id)
    except Exception as e:
        print(f"[dl] failed: {url} -> {e}", flush=True)
        # Second tool for media, same as the main handler.
        if backend == "media" and not is_instagram:
            gdl = _download_with_gallery_dl(url)
            if gdl:
                filename, title = gdl
                try:
                    if os.path.getsize(filename) > MAX_SEND_BYTES:
                        bot.reply_to(message, "الفيديو كبير أكتر من 50 ميجا، والتيليجرام مش هيقبله.")
                        return
                    send_file(message.chat.id, filename, title)
                    bot.edit_message_text("تم ✅", message.chat.id, notice.message_id)
                    return
                except Exception:
                    pass
                finally:
                    try:
                        os.remove(filename)
                    except OSError:
                        pass
        if is_youtube and _is_cookie_failure(e):
            _notify_admin("⚠️ تنبيه: تحميل اليوتيوب فشل على FadeHost (يوتيوب حاجب آي بي السيرفر).")
        bot.edit_message_text("معرفتش أحمل الفيديو ده، جرب لينك تاني.", message.chat.id, notice.message_id)
    finally:
        shutil.rmtree(dl_dir, ignore_errors=True)


@bot.message_handler(commands=["cookies"])
def list_cookies(message):
    if not _is_admin(message.chat.id) or not _UNIVERSAL_DL or _vault is None:
        return
    rows = _vault.summary()
    if not rows:
        bot.reply_to(message, "لا توجد كوكيز مخزنة")
        return
    lines = []
    for row in rows:
        state = "معطل" if row["disabled"] else "فعال"
        lines.append(f"{row['name']} - {state} - فشل={row['failures']}")
    bot.reply_to(message, "\n".join(lines))


@bot.message_handler(commands=["addcookie"])
def ask_cookie_file(message):
    if not _is_admin(message.chat.id) or not _UNIVERSAL_DL or _vault is None:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "الاستخدام: /addcookie <name> ثم أرسل ملف cookies.txt")
        return
    name = parts[1].strip()
    prompt = bot.reply_to(message, f"أرسل ملف cookies.txt للحساب: {name}")
    bot.register_next_step_handler(prompt, lambda msg: _save_cookie_file(msg, name))


def _save_cookie_file(message, name):
    if message.document is None:
        bot.reply_to(message, "لم أستلم ملفاً")
        return
    try:
        raw = bot.download_file(message.document.file_path).decode("utf-8")
        _vault.add(name, raw, replace=True)
        bot.reply_to(message, f"تم حفظ الكوكيز باسم {name} ✅")
    except Exception as exc:
        print(f"[addcookie] failed: {exc}", flush=True)
        bot.reply_to(message, "فشل الحفظ: تأكد إن الملف cookies.txt صحيح.")


@bot.message_handler(commands=["yt"])
def handle_yt(message):
    # Cookie-free YouTube download (yt-dlp with visionos/android_vr
    # clients, then pytubefix). Open to everyone, like /dl.
    if not _YT_COOKIEFREE:
        bot.reply_to(message, "التحميل بدون كوكيز مش متاح دلوقتي.")
        return
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "الاستخدام: /yt <رابط يوتيوب>")
        return
    url = parts[1].strip()
    if "music.youtube.com" in url:
        url = url.replace("music.youtube.com", "www.youtube.com", 1)
    if "youtube.com" not in url and "youtu.be" not in url:
        bot.reply_to(message, "الأمر ده لليوتيوب بس. ابعت لينك يوتيوب.")
        return
    notice = bot.reply_to(message, "جارٍ تحميل يوتيوب بدون كوكيز... ⏳")
    yt_dir = os.path.join(DOWNLOAD_DIR, f"dl-yt-{message.chat.id}-{int(time.time())}")
    os.makedirs(yt_dir, exist_ok=True)
    try:
        files = download_youtube_cookiefree(
            url,
            output_dir=yt_dir,
            progress=_dl_progress_editor(message.chat.id, notice.message_id),
        )
        sent = _deliver_dl_files(message, files)
        if sent:
            bot.edit_message_text("تم ✅ (بدون كوكيز)", message.chat.id, notice.message_id)
        else:
            bot.edit_message_text("معرفتش أحمل الفيديو ده، جرب لينك تاني.", message.chat.id, notice.message_id)
    except Exception as e:
        print(f"[yt] failed: {url} -> {e}", flush=True)
        bot.edit_message_text("معرفتش أحمل الفيديو ده، جرب لينك تاني.", message.chat.id, notice.message_id)
    finally:
        shutil.rmtree(yt_dir, ignore_errors=True)


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
    # Cookie sets to try in order. YouTube tries both of its sets.
    # Instagram: use its own session cookies if configured, otherwise
    # fall back to the same cookie file the old working version passed
    # for every download (harmless - those cookies are for youtube.com
    # and are never sent to instagram.com).
    if is_youtube:
        cookie_files = [_p for _p in (_COOKIES_PATH, _COOKIES_PATH_2) if _p]
    elif is_instagram:
        cookie_files = [_p for _p in (_IG_COOKIES_PATH, _COOKIES_PATH) if _p]
    else:
        cookie_files = []
    if not cookie_files:
        cookie_files = [None]
    filename = None
    info = None
    cookie_dead = False
    try:
        # Cookie-free YouTube first (works without cookies now via
        # visionos/android_vr clients + pytubefix); fall back to the
        # cookie sets below if it fails.
        if is_youtube and _YT_COOKIEFREE:
            try:
                _cf_files = download_youtube_cookiefree(url, output_dir=DOWNLOAD_DIR)
                if _cf_files:
                    filename = _cf_files[0]
                    info = {"title": os.path.splitext(os.path.basename(filename))[0]}
            except Exception as _cf_e:
                print(f"[download] cookiefree YouTube failed: {url} -> {_cf_e}", flush=True)
        if not filename:
            for _idx, _cookie in enumerate(cookie_files):
                try:
                    filename, info = _download_with_ytdlp(url, _cookie)
                    break
                except Exception as e:
                    if is_youtube and _idx < len(cookie_files) - 1:
                        # Try the next cookie set. A stale set can fail in
                        # ways that don't say "sign in" (e.g. YouTube's
                        # "The page needs to be reloaded"), so fail over on
                        # any YouTube error, not just sign-in errors.
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
        else:
            # Second tool: gallery-dl often succeeds where yt-dlp fails
            # (different extraction). Skip for Instagram (needs login).
            if not is_instagram:
                gdl = _download_with_gallery_dl(url)
                if gdl:
                    filename, title = gdl
                    try:
                        if os.path.getsize(filename) > 50 * 1024 * 1024:
                            bot.reply_to(message, "الفيديو كبير أكتر من 50 ميجا، والتيليجرام مش هيقبله.")
                            return
                        send_file(message.chat.id, filename, title)
                        bot.reply_to(message, "تم ✅")
                        return
                    except Exception:
                        bot.reply_to(message, "معرفتش أحمل الفيديو ده، جرب لينك تاني.")
                        return
        if is_youtube and cookie_dead:
            _notify_admin("⚠️ تنبيه: تحميل اليوتيوب فشل على FadeHost (يوتيوب حاجب آي بي السيرفر).")
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
    # Warm up WARP in the background so the first YouTube/Instagram
    # download does not wait for the binary download + registration.
    # Best-effort only: any failure just means direct egress.
    if _ensure_warp is not None:
        threading.Thread(target=_ensure_warp, daemon=True).start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
