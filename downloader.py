from __future__ import annotations

import importlib.util
import inspect
import ipaddress
import math
import os
import re
import shutil
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

import httpx
from yt_dlp import YoutubeDL

from cookies_vault import CookieError, CookieVault

# Cloudflare WARP egress (optional): when the host's datacenter IP is
# blocked by YouTube, downloads go through WARP's SOCKS5/HTTP proxy
# instead. Imported defensively - if warp.py is missing or broken,
# everything just uses direct egress as before.
try:
    from warp import ensure_warp as _ensure_warp
    from warp import warp_http_proxy as _warp_http_proxy
except Exception:
    _ensure_warp = None
    _warp_http_proxy = None

Progress = Callable[[int, int], None]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TelegramDownloadBot/1.0)"}
VIDEO_HOSTS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "tiktok.com",
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "x.com",
    "twitter.com",
    "vimeo.com",
    "dailymotion.com",
    "twitch.tv",
    "soundcloud.com",
    "reddit.com",
    "bilibili.com",
    "streamable.com",
    "odysee.com",
)
COOKIE_REQUIRED_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")
MIN_MULTICONNECTION = 2 * 1024 * 1024
DEFAULT_CONNECTIONS = 8
_FOLLOW_REDIRECTS = "follow_redirects" in inspect.signature(httpx.Client.__init__).parameters


def _new_client(timeout: float = 60.0) -> httpx.Client:
    kwargs = {"headers": HEADERS, "timeout": timeout}
    if _FOLLOW_REDIRECTS:
        kwargs["follow_redirects"] = True
    try:
        return httpx.Client(**kwargs)
    except Exception:
        # A malformed proxy env var (e.g. bracketed IPv6 in NO_PROXY)
        # makes httpx fail at construction; retry without env proxies.
        return httpx.Client(trust_env=False, **kwargs)


@dataclass
class DownloadResult:
    url: str
    backend: str
    files: list[str]
    profile: str | None = None


class _Meter:
    def __init__(self, total: int, callback: Progress | None) -> None:
        self.total = total
        self.callback = callback
        self.done = 0
        self._lock = threading.Lock()

    def add(self, amount: int) -> None:
        with self._lock:
            self.done += amount
            if self.callback and self.total:
                self.callback(min(self.done, self.total), self.total)


def normalize_url(url: str) -> str:
    url = url.strip()
    if url.startswith("magnet:"):
        return url
    if "://" not in url:
        host = url.split("/", 1)[0].split("?", 1)[0].split(":", 1)[0]
        try:
            ipaddress.ip_address(host)
            return "http://" + url
        except ValueError:
            return "https://" + url
    return url


def classify(url: str) -> str:
    value = normalize_url(url)
    lowered = value.lower()
    if lowered.startswith("magnet:") or lowered.endswith(".torrent") or ".torrent?" in lowered:
        return "torrent"
    host = (urlparse(value).hostname or "").lower().rstrip(".")
    if any(host == name or host.endswith("." + name) for name in VIDEO_HOSTS):
        return "media"
    return "direct"


def needs_cookies(url: str) -> bool:
    host = (urlparse(normalize_url(url)).hostname or "").lower().rstrip(".")
    return any(host == name or host.endswith("." + name) for name in COOKIE_REQUIRED_HOSTS)


def _safe_name(name: str) -> str:
    name = unquote(name).replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .")
    if name.startswith("."):
        name = "_" + name
    return name[:180] or f"download_{int(time.time())}"


def _unique_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = Path(filename).stem, Path(filename).suffix
    for index in range(1, 1000):
        candidate = directory / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem} ({time.time_ns()}){suffix}"


def _filename_from(headers: dict[str, str], url: str) -> str:
    disposition = headers.get("content-disposition", "")
    match = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", disposition, re.I)
    if match:
        return _safe_name(match.group(1).strip().strip('"'))
    match = re.search(r'filename\s*=\s*"([^"]+)"', disposition, re.I)
    if match:
        return _safe_name(match.group(1))
    match = re.search(r"filename\s*=\s*([^;]+)", disposition, re.I)
    if match:
        return _safe_name(match.group(1))
    return _safe_name(unquote(urlparse(url).path.rsplit("/", 1)[-1]))


def _probe_headers(client: httpx.Client, url: str) -> dict[str, str]:
    try:
        response = client.head(url)
        if response.status_code < 400:
            return dict(response.headers)
    except httpx.HTTPError:
        pass
    with client.stream("GET", url) as response:
        response.raise_for_status()
        return dict(response.headers)


def _range_capable(client: httpx.Client, url: str, headers: dict[str, str]) -> bool:
    if headers.get("accept-ranges", "").lower() == "bytes":
        return True
    try:
        with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as response:
            return response.status_code == 206
    except httpx.HTTPError:
        return False


def _fetch_range(client: httpx.Client, url: str, start: int, end: int, fd: int, meter: _Meter) -> None:
    with client.stream("GET", url, headers={"Range": f"bytes={start}-{end}"}) as response:
        if response.status_code != 206:
            raise RuntimeError("server ignored range request")
        offset = start
        for block in response.iter_bytes():
            os.pwrite(fd, block, offset)
            offset += len(block)
            meter.add(len(block))


def download_direct(
    url: str,
    output_dir: str = "downloads",
    connections: int = DEFAULT_CONNECTIONS,
    progress: Progress | None = None,
) -> Path:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    value = normalize_url(url)

    with _new_client() as client:
        headers = _probe_headers(client, value)
        path = _unique_path(directory, _filename_from(headers, value))
        total = int(headers.get("content-length") or 0)
        parallel = total >= MIN_MULTICONNECTION and _range_capable(client, value, headers)

        if parallel:
            workers = max(2, min(connections, math.ceil(total / MIN_MULTICONNECTION)))
            step = math.ceil(total / workers)
            spans = [(start, min(start + step - 1, total - 1)) for start in range(0, total, step)]
            meter = _Meter(total, progress)
            try:
                with path.open("wb") as handle:
                    handle.truncate(total)
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        futures = [
                            pool.submit(_fetch_range, client, value, start, end, handle.fileno(), meter)
                            for start, end in spans
                        ]
                        for future in as_completed(futures):
                            future.result()
                if path.stat().st_size == total:
                    return path
                raise RuntimeError("incomplete parallel download")
            except Exception:
                path.unlink(missing_ok=True)

        meter = _Meter(total, progress)
        with client.stream("GET", value) as response:
            response.raise_for_status()
            with path.open("wb") as handle:
                for block in response.iter_bytes():
                    handle.write(block)
                    meter.add(len(block))
        if total and path.stat().st_size != total:
            raise RuntimeError("incomplete download")
        return path


def _media_options(output_dir: Path) -> dict:
    # Same yt-dlp settings as the main bot handler: short %(id)s filenames
    # (a %(title)s template breaks on long Facebook post titles),
    # 45M format cap + 50M max so Telegram can accept the file, and the
    # tv/web_embedded/android_vr player clients that get past YouTube's
    # bot-check. concurrent_fragment_downloads speeds downloads up.
    return {
        "format": "best[height<=720][ext=mp4][filesize<=45M]/best[height<=720][filesize<=45M]/best[height<=720]/best",
        "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "overwrites": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "merge_output_format": "mp4",
        "max_filesize": 50 * 1024 * 1024,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "concurrent_fragment_downloads": 8,
        "extractor_args": {
            "youtube": {
                "player_client": ["tv", "web_embedded", "android_vr", "tv_downgraded", "web"],
                "player_skip": ["webpage"],
            }
        },
    }


def _extract_media(url: str, options: dict) -> list[str]:
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        items = info.get("entries", [info]) if isinstance(info, dict) else [info]
        files: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            candidates = [
                entry.get("filepath")
                for entry in item.get("requested_downloads", [])
                if isinstance(entry, dict) and entry.get("filepath")
            ]
            try:
                candidates.append(ydl.prepare_filename(item))
            except Exception:
                pass
            for candidate in candidates:
                if candidate and Path(candidate).is_file() and candidate not in files:
                    files.append(candidate)
        return files


def _js_runtime_options() -> dict:
    # yt-dlp needs a JS runtime to decipher YouTube signatures / PO
    # tokens. Prefer quickjs (pip package, works on Railway), else a
    # system runtime (Deno is already in the Dockerfile).
    if importlib.util.find_spec("quickjs") is not None:
        return {"js_runtimes": {"quickjs": {}}}
    for name in ("node", "deno", "bun"):
        if shutil.which(name):
            return {"js_runtimes": {name: {}}}
    return {}


def _progress_hook(callback: Progress | None):
    if callback is None:
        return None

    def hook(data: dict) -> None:
        if data.get("status") != "downloading":
            return
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        done = data.get("downloaded_bytes") or 0
        if total:
            callback(done, total)

    return hook


# Player clients that get past YouTube's datacenter-IP bot-check without
# cookies (verified from a datacenter IP, no cookies). visionos is the
# key one; the rest are fallbacks, then pytubefix.
_COOKIELESS_CLIENTS = (["visionos", "android_vr"], ["android_vr"], ["web_safari", "android"], None)


def _download_youtube_pytubefix(url: str, output_dir: Path, progress: Progress | None) -> list[str]:
    try:
        from pytubefix import YouTube
        from pytubefix.botGuard import bot_guard
    except ImportError as exc:
        raise RuntimeError("pytubefix is not installed") from exc

    def on_progress(stream, chunk, bytes_remaining: int) -> None:
        if progress is None:
            return
        total = getattr(stream, "filesize", 0) or 0
        if total:
            progress(total - bytes_remaining, total)

    # PO token (proof-of-origin): YouTube now requires it on the WEB
    # client or it answers LOGIN_REQUIRED / "detected as a bot" from a
    # flagged datacenter IP. pytubefix never sends one unless
    # use_po_token=True + a verifier is given, so mint it locally with
    # BotGuard under the pip Node wheel (nodejs-wheel-binaries, already
    # installed as a pytubefix dependency). No cookies involved.
    # Diagnostics only (token length + first 4 chars, never the token).
    def _make_verifier(holder: dict):
        def _po_token_verifier():
            yt = holder["yt"]
            visitor_data = getattr(yt, "visitor_data", None)
            try:
                token = bot_guard.generate_po_token(yt.video_id)
            except Exception as exc:
                print(f"[pytubefix] po_token mint failed: {exc}", flush=True)
                raise
            print(
                f"[pytubefix] po_token minted: len={len(token) if token else 0} "
                f"prefix={(token or '')[:4]} visitor_data={'yes' if visitor_data else 'no'}",
                flush=True,
            )
            return visitor_data, token

        return _po_token_verifier

    # WARP egress (if up): pytubefix uses stdlib urllib, whose
    # ProxyHandler cannot do SOCKS5, so it gets wireproxy's HTTP
    # listener instead. When WARP is down, reset the global urllib
    # opener first - a previous WARP-up call may have installed a
    # proxy opener that would otherwise keep pointing at a dead proxy.
    http_proxy = _warp_http_proxy() if _warp_http_proxy else None
    if http_proxy is None:
        urllib.request.install_opener(urllib.request.build_opener())
    proxies = {"http": http_proxy, "https": http_proxy} if http_proxy else None

    last_error: Exception | None = None
    for _client in ("WEB", "WEB_EMBED"):
        holder: dict = {}
        try:
            video = YouTube(
                url,
                client=_client,
                use_po_token=True,
                po_token_verifier=_make_verifier(holder),
                allow_oauth_cache=False,
                token_file=str(output_dir / "pytubefix-tokens.json"),
                on_progress_callback=on_progress,
                proxies=proxies,
            )
            holder["yt"] = video
        except Exception as exc:
            last_error = exc
            continue
        try:
            stream = video.streams.get_highest_resolution()
            if stream is None:
                stream = next(
                    (item for item in video.streams if item.is_progressive and item.type == "video"),
                    None,
                )
            if stream is None:
                raise RuntimeError("no progressive stream available")
            path = stream.download(output_path=str(output_dir), skip_existing=False)
            if not path:
                raise RuntimeError("pytubefix returned no file")
            return [path]
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(f"pytubefix download failed: {last_error}") from last_error


def download_youtube(
    url: str,
    output_dir: str = "downloads",
    *,
    progress: Progress | None = None,
    format_id: str | None = None,
) -> list[str]:
    """Download a YouTube URL without cookies. Tries yt-dlp with
    cookieless player clients first (visionos/android_vr etc., with a JS
    runtime for signature/PO-token deciphering), then pytubefix.
    Uses the same safe yt-dlp settings as _media_options (short %(id)s
    filenames, 45M format cap, 50M max_filesize, nocheckcertificate).
    Raises RuntimeError if every attempt fails."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    hook = _progress_hook(progress)
    last_error: Exception | None = None

    # WARP egress (if up): route yt-dlp through the SOCKS5 proxy so
    # YouTube sees Cloudflare's IP instead of the host's flagged one.
    # Resolved once per download, not per client attempt.
    proxy = _ensure_warp() if _ensure_warp else None
    if proxy:
        print(f"[download] using WARP proxy for YouTube: {proxy}", flush=True)

    for clients in _COOKIELESS_CLIENTS:
        options = _media_options(directory)
        options.update(_js_runtime_options())
        options["format"] = format_id or "best[height<=720][ext=mp4][filesize<=45M]/best[height<=720][filesize<=45M]/best[height<=720]/best"
        if proxy:
            options["proxy"] = proxy
        if clients is not None:
            options["extractor_args"] = {"youtube": {"player_client": clients}}
        if hook:
            options["progress_hooks"] = [hook]
        try:
            files = _extract_media(url, options)
        except Exception as exc:
            last_error = exc
            continue
        if files:
            return files
        last_error = RuntimeError("no files were produced")

    try:
        return _download_youtube_pytubefix(url, directory, progress)
    except Exception as exc:
        last_error = exc

    raise RuntimeError(f"cookie-free YouTube download failed: {last_error}") from last_error


def download_media(
    url: str,
    output_dir: str = "downloads",
    *,
    vault: CookieVault | None = None,
    allow_cookie_fallback: bool = False,
    prefer_cookies: bool = False,
) -> tuple[list[str], str | None]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    base = _media_options(directory)
    profiles: list[object] = []

    if vault is not None and (prefer_cookies or allow_cookie_fallback):
        try:
            profiles = list(vault.iter_available())
        except CookieError:
            pass

    attempts: list[object] = profiles + [None] if prefer_cookies else [None] + profiles

    last_error: Exception | None = None
    for entry in attempts:
        options = dict(base)
        try:
            if entry is None:
                files = _extract_media(url, options)
            else:
                with vault.cookie_file(entry.name) as cookie_path:
                    options["cookiefile"] = str(cookie_path)
                    files = _extract_media(url, options)
                vault.mark_success(entry.name)
            if files:
                return files, None if entry is None else entry.name
            last_error = RuntimeError("no media files were produced")
        except Exception as exc:
            last_error = exc
            if entry is not None:
                vault.mark_failure(entry.name)

    raise RuntimeError(f"media download failed: {last_error}") from last_error


def download_torrent(url: str, output_dir: str = "downloads", timeout: int | None = None) -> list[str]:
    try:
        import libtorrent as lt
    except ImportError as exc:
        raise RuntimeError("torrent support requires libtorrent (pip install libtorrent==2.0.11)") from exc

    budget = float(timeout or os.environ.get("TORRENT_TIMEOUT", "300"))
    directory = Path(output_dir) / f"torrent-{int(time.time())}"
    directory.mkdir(parents=True, exist_ok=True)

    session = lt.session()
    session.listen_on(6881, 6891)
    session.start_dht()
    session.start_lsd()
    session.start_upnp()
    session.start_natpmp()
    for router, port in (("router.bittorrent.com", 6881), ("dht.transmissionbt.com", 6881)):
        session.add_dht_router(router, port)

    params = {
        "save_path": str(directory),
        "storage_mode": lt.storage_mode_t.storage_mode_sparse,
    }
    if normalize_url(url).startswith("magnet:"):
        handle = lt.add_magnet_uri(session, url, params)
    else:
        with _new_client(timeout=30.0) as client:
            response = client.get(normalize_url(url))
            response.raise_for_status()
        info = lt.torrent_info(lt.bdecode(response.content))
        handle = session.add_torrent({"ti": info, **params})

    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        status = handle.status()
        if status.progress >= 1.0 or status.is_seeding:
            break
        session.pop_alerts()
        time.sleep(1)
    session.pause()

    return [
        str(path)
        for path in directory.rglob("*")
        if path.is_file() and path.stat().st_size > 0 and not path.name.endswith(".part")
    ]


def download(
    url: str,
    vault: CookieVault | None = None,
    output_dir: str = "downloads",
    *,
    connections: int = DEFAULT_CONNECTIONS,
    allow_cookie_fallback: bool = False,
    progress: Progress | None = None,
) -> DownloadResult:
    value = normalize_url(url)
    backend = classify(value)

    if backend == "direct":
        files = [str(download_direct(value, output_dir, connections, progress))]
        profile = None
    elif backend == "torrent":
        files = download_torrent(value, output_dir)
        profile = None
    else:
        files, profile = download_media(
            value,
            output_dir,
            vault=vault,
            allow_cookie_fallback=allow_cookie_fallback,
            prefer_cookies=vault is not None and needs_cookies(value),
        )

    if not files:
        raise RuntimeError("no files were downloaded")
    return DownloadResult(url=value, backend=backend, files=files, profile=profile)
