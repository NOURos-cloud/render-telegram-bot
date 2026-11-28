"""Cloudflare WARP egress for the Telegram download bot.

Why this exists
---------------
FadeHost's North-America datacenter IP is hard-flagged by YouTube: even
with a valid, freshly minted PO token (pytubefix, len=124) YouTube still
answers "Sign in to confirm you're not a bot", and Instagram geo-blocks
some reels on the US IP ("not available in your country"). The block is
on the *egress IP*, not on the code, so the fix is to leave FadeHost via
a different IP: Cloudflare WARP (free, anonymous, no account).

How it works
------------
* ``wgcf`` (ViRb3) registers a free anonymous WARP identity and generates
  a WireGuard profile (``wgcf-profile.conf``).
* ``wireproxy`` (windtf) runs that WireGuard tunnel in userspace (no
  root, no NET_ADMIN) and exposes it as a SOCKS5 proxy on
  127.0.0.1:1080 plus an HTTP proxy on 127.0.0.1:18080.
* yt-dlp talks SOCKS5 natively (PySocks); pytubefix uses stdlib urllib,
  whose ProxyHandler cannot do SOCKS5, so it gets the HTTP proxy
  instead (plain HTTP CONNECT, which urllib handles natively).
* IPv4 is forced: the WireGuard peer only routes 0.0.0.0/0 and
  wireproxy's ``ResolveStrategy = ipv4`` prioritises A records, so
  YouTube/googlevideo are always reached over IPv4.

Non-fatal by design
-------------------
Every step can fail (no outbound UDP on the host, GitHub unreachable,
registration rejected, ...). If anything fails, ``ensure_warp()``
returns ``None`` and the bot keeps working with DIRECT egress exactly
as before. Nothing here ever raises to the caller, and nothing here
ever blocks bot startup: the binaries/config are prepared lazily on
the first download (or by a warm-up thread), then cached in
``warp-bin/`` next to this file so restarts are fast.

Security note: ``warp-bin/`` holds the WARP private key
(``wgcf-profile.conf`` / ``wireproxy.conf``). Never print those files
and never log their contents.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import tarfile
import threading
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOCKS_PORT = 1080
HTTP_PORT = 18080
INFO_PORT = 9080
SOCKS_URL = f"socks5://127.0.0.1:{SOCKS_PORT}"
HTTP_URL = f"http://127.0.0.1:{HTTP_PORT}"
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"

# wgcf releases (linux amd64). First entry is tried first; the second is
# the fallback if the first URL 404s or its hash mismatches. Hashes are
# the publishers' own release checksums (verified against the GitHub
# release API 2026-11-27 for the fallback entries).
_WGCF_VERSIONS = [
    ("2.2.31", "69147e1a517c66129edd8ac8cb60484d6c9515178d7b4a2f95e3c925f225572a"),
    ("2.2.30", "fc443008fe29a6f0b05b45d27436b7ce89e87a6836718597ccb39e41da418304"),
]
_WIREPROXY_VERSIONS = [
    ("1.1.3", "e88c1d090740373fc606c1bafd81d9a5eadc642cce5667616e20e9d7a444f51c"),
    ("1.1.2", "b7dcff8f6e9d3410364e432aff24154eaa8db8206e0c6faac35d6c6ab06dac51"),
]

_WGCF_URL = "https://github.com/ViRb3/wgcf/releases/download/v{ver}/wgcf_{ver}_linux_amd64"
_WIREPROXY_URL = "https://github.com/windtf/wireproxy/releases/download/v{ver}/wireproxy_linux_amd64.tar.gz"

_DOWNLOAD_TIMEOUT = 120
_WGCF_TIMEOUT = 90
_START_TIMEOUT = 25          # wait for the SOCKS port to accept
_CHECK_TIMEOUT = 25          # wait for readyz / trace to confirm
_DOWN_COOLDOWN = 600         # seconds before retrying after a failure

# ---------------------------------------------------------------------------
# Module state (guarded by _LOCK)
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()  # RLock: ensure_warp() calls stop_warp() while holding it
_PROC: subprocess.Popen | None = None   # wireproxy we started (None if adopted)
_ADOPTED = False                        # True if we adopted a pre-existing proxy
_STATUS: str = "unknown"                # "unknown" | "up" | "down"
_LAST_ATTEMPT = 0.0


def _log(msg: str) -> None:
    print(f"[warp] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _warp_dir() -> Path:
    """Persistent dir for binaries + WARP identity. Prefer the repo dir
    (it survives restarts on FadeHost); fall back to /tmp if it is not
    writable (binaries are then re-downloaded on every restart, which
    still works, just slower)."""
    here = Path(__file__).resolve().parent
    for base in (here / "warp-bin", Path("/tmp") / "warp-bin"):
        try:
            base.mkdir(parents=True, exist_ok=True)
            probe = base / ".write-test"
            probe.write_text("ok")
            probe.unlink()
            return base
        except OSError:
            continue
    return Path("/tmp") / "warp-bin"


# ---------------------------------------------------------------------------
# Binary download (stdlib only, with sha256 verification)
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: Path, sha256: str) -> bool:
    """Download *url* to *dest*, verifying its sha256. Returns True on
    success. Never raises."""
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "warp-bot/1.0"})
        with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp, open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f)
        if _sha256(tmp) != sha256:
            _log(f"hash mismatch for {dest.name}, discarding")
            tmp.unlink(missing_ok=True)
            return False
        tmp.replace(dest)
        return True
    except Exception as exc:
        _log(f"download failed: {url} -> {exc}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _ensure_binaries(warp_dir: Path) -> tuple[Path, Path] | None:
    """Download wgcf + wireproxy (cached; skipped when already present
    and executable). Returns (wgcf, wireproxy) or None."""
    wgcf = warp_dir / "wgcf"
    wireproxy = warp_dir / "wireproxy"

    if not (wgcf.is_file() and os.access(wgcf, os.X_OK)):
        got = False
        for ver, sha in _WGCF_VERSIONS:
            _log(f"downloading wgcf v{ver} ...")
            if _download(_WGCF_URL.format(ver=ver), wgcf, sha):
                got = True
                break
        if not got:
            _log("could not download wgcf")
            return None
        wgcf.chmod(0o755)

    if not (wireproxy.is_file() and os.access(wireproxy, os.X_OK)):
        got = False
        tarball = warp_dir / "wireproxy.tar.gz"
        for ver, sha in _WIREPROXY_VERSIONS:
            _log(f"downloading wireproxy v{ver} ...")
            if _download(_WIREPROXY_URL.format(ver=ver), tarball, sha):
                got = True
                break
        if not got:
            _log("could not download wireproxy")
            return None
        try:
            with tarfile.open(tarball, "r:gz") as tf:
                # Only extract the single top-level binary, never
                # arbitrary members (path-traversal safety).
                member = next(
                    (m for m in tf.getmembers() if m.isfile() and m.name.rstrip("/").endswith("wireproxy")),
                    None,
                )
                if member is None:
                    _log("wireproxy tarball has no wireproxy binary")
                    return None
                member.name = "wireproxy"
                tf.extract(member, path=warp_dir)
        except Exception as exc:
            _log(f"wireproxy extract failed: {exc}")
            return None
        finally:
            try:
                tarball.unlink(missing_ok=True)
            except OSError:
                pass
        wireproxy.chmod(0o755)

    return wgcf, wireproxy


# ---------------------------------------------------------------------------
# WARP identity (wgcf register + generate, once, cached)
# ---------------------------------------------------------------------------

def _run(cmd: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
    except Exception as exc:
        _log(f"command failed: {' '.join(cmd[:2])} -> {exc}")
        return None


def _run_env(cmd: list[str], cwd: Path, timeout: int, extra_env: dict) -> subprocess.CompletedProcess | None:
    """Like _run but with extra environment variables merged in."""
    try:
        env = dict(os.environ)
        env.update(extra_env)
        return subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env
        )
    except Exception as exc:
        _log(f"command failed: {' '.join(cmd[:2])} -> {exc}")
        return None


def _ensure_account(wgcf: Path, warp_dir: Path) -> Path | None:
    """Register a free anonymous WARP identity (once) and generate the
    WireGuard profile (once). Returns the profile path or None."""
    account = warp_dir / "wgcf-account.toml"
    profile = warp_dir / "wgcf-profile.conf"

    if not account.is_file():
        # Pre-check: can we reach Cloudflare's registration API at all?
        # If the host blocks it, wgcf register cannot succeed and we
        # should say so plainly instead of retrying blindly.
        try:
            req = urllib.request.Request(
                "https://api.cloudflareclient.com/v0a4005/reg",
                headers={"User-Agent": "warp-bot/1.0"},
                method="GET",
            )
            urllib.request.urlopen(req, timeout=15)
            _log("Cloudflare API reachable (register endpoint answered)")
        except Exception as exc:
            _log(f"Cloudflare API NOT reachable from this host: {exc}")
        # Retry registration: the first attempt can fail transiently
        # (DNS / TLS / API hiccup). Set HOME explicitly — wgcf writes
        # its account file relative to it on some setups.
        # NOTE: wgcf register is INTERACTIVE by default — it shows a
        # Cloudflare Terms-of-Service prompt ("Do you agree? Yes/No")
        # and waits for arrow-key input. In a non-interactive host
        # (FadeHost) stdin is EOF immediately, so it fails in checkTOS
        # before ever reaching the network. --accept-tos accepts the
        # TOS non-interactively and is REQUIRED here.
        env_home = str(warp_dir)
        ok = False
        for attempt in range(1, 4):
            _log(f"registering anonymous WARP identity (wgcf register --accept-tos, attempt {attempt}/3) ...")
            res = _run_env([str(wgcf), "register", "--accept-tos"], warp_dir, _WGCF_TIMEOUT, {"HOME": env_home})
            if res is not None and res.returncode == 0 and account.is_file():
                ok = True
                break
            # Log the FULL output (not truncated) so the real error is visible.
            out = ""
            if res is not None:
                out = (res.stdout or "") + "\n" + (res.stderr or "")
            _log(f"wgcf register failed (rc={res.returncode if res else '?'}): {out.strip()[:2000]}")
            if attempt < 3:
                time.sleep(15)
        if not ok:
            return None
        _log("WARP identity registered")
    if not profile.is_file():
        _log("generating WireGuard profile (wgcf generate) ...")
        res = _run([str(wgcf), "generate"], warp_dir, _WGCF_TIMEOUT)
        if res is None or res.returncode != 0 or not profile.is_file():
            err = (res.stderr or res.stdout or "")[-300:] if res else ""
            _log(f"wgcf generate failed (rc={res.returncode if res else '?'}): {err}")
            return None
        _log("WireGuard profile generated")
    return profile


def _parse_wgcf_profile(profile: Path) -> dict | None:
    """Minimal INI parse of wgcf-profile.conf. Returns the keys we need.
    Never log the returned values (they include the private key)."""
    data: dict = {}
    try:
        section = ""
        for raw in profile.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                data[f"{section}.{key.strip()}"] = value.strip()
    except Exception as exc:
        _log(f"could not read WireGuard profile: {exc}")
        return None
    need = ("Interface.PrivateKey", "Interface.Address", "Peer.PublicKey", "Peer.Endpoint")
    if not all(data.get(k) for k in need):
        _log("WireGuard profile is missing keys")
        return None
    return data


def _build_wireproxy_conf(profile: Path, dest: Path, endpoint: str | None = None) -> Path | None:
    """Build wireproxy.conf from the wgcf profile. Forces IPv4-only
    egress: AllowedIPs is 0.0.0.0/0 (no ::/0) and ResolveStrategy=ipv4,
    so YouTube/googlevideo are always reached over IPv4.
    *endpoint* overrides the profile's Peer endpoint (used to retry
    the WARP UDP ports 500/1701/4500 when 2408 is blocked)."""
    keys = _parse_wgcf_profile(profile)
    if keys is None:
        return None
    endpoint = endpoint or keys["Peer.Endpoint"]
    conf = (
        "[Interface]\n"
        f"PrivateKey = {keys['Interface.PrivateKey']}\n"
        f"Address = {keys['Interface.Address']}\n"
        "DNS = 1.1.1.1\n"
        "MTU = 1280\n"
        "CheckAlive = 1.1.1.1\n"
        "CheckAliveInterval = 5\n"
        "\n"
        "[Peer]\n"
        f"PublicKey = {keys['Peer.PublicKey']}\n"
        "AllowedIPs = 0.0.0.0/0\n"
        f"Endpoint = {endpoint}\n"
        "PersistentKeepalive = 25\n"
        "\n"
        "[Resolve]\n"
        "ResolveStrategy = ipv4\n"
        "\n"
        "[Socks5]\n"
        f"BindAddress = 127.0.0.1:{SOCKS_PORT}\n"
        "\n"
        "[http]\n"
        f"BindAddress = 127.0.0.1:{HTTP_PORT}\n"
    )
    try:
        dest.write_text(conf)
        return dest
    except OSError as exc:
        _log(f"could not write wireproxy config: {exc}")
        return None


def _resolve_host_ipv4(host: str) -> str | None:
    """Resolve *host* to an IPv4 address, or None. Used to try the WARP
    endpoint by raw IP when DNS is the problem."""
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_DGRAM)
        if infos:
            return infos[0][4][0]
    except OSError:
        pass
    return None


def _candidate_endpoints(profile_endpoint: str) -> list[str]:
    """Endpoints to try, in order: the profile's own endpoint first,
    then the same host on Cloudflare WARP's other UDP ports
    (500/1701/4500 — hosts sometimes block only 2408), then the
    resolved IPv4 form (in case DNS is the problem). Deduplicated."""
    host, sep, port = profile_endpoint.rpartition(":")
    if not sep or not port.isdigit():
        return [profile_endpoint]
    out: list[str] = []
    for p in (int(port), 500, 1701, 4500):
        ep = f"{host}:{p}"
        if ep not in out:
            out.append(ep)
    ip = _resolve_host_ipv4(host)
    if ip and ip != host:
        ep = f"{ip}:{port}"
        if ep not in out:
            out.append(ep)
    return out


def _dump_wireproxy_log_tail(warp_dir: Path, lines: int = 30) -> None:
    """Print the tail of wireproxy's own log so the REAL tunnel error
    (handshake timeout, no route, DNS failure, ...) is visible in the
    bot logs instead of a bare 'did not verify'. The log never holds
    the private key, but any line mentioning it is skipped anyway."""
    try:
        text = (warp_dir / "wireproxy.log").read_text(errors="ignore")
    except OSError:
        _log("wireproxy log not readable")
        return
    tail = [ln for ln in text.splitlines() if "PrivateKey" not in ln][-lines:]
    _log("wireproxy.log tail:")
    for ln in tail:
        print(f"[warp-log] {ln}", flush=True)


# ---------------------------------------------------------------------------
# Liveness checks
# ---------------------------------------------------------------------------

def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def _readyz_ok(timeout: float) -> bool:
    """wireproxy's /readyz returns 200 once the tunnel answered a ping
    (CheckAlive). Pure stdlib, no curl needed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{INFO_PORT}/readyz", timeout=3
            ) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _trace_warp_on(timeout: int) -> bool | None:
    """Fetch https://www.cloudflare.com/cdn-cgi/trace through the SOCKS5
    proxy and check for ``warp=on``. Returns True/False, or None when
    the check itself could not run (no curl and no PySocks)."""
    # 1) curl, if present (socks5h = remote DNS through the proxy).
    curl = shutil.which("curl")
    if curl:
        try:
            res = subprocess.run(
                [curl, "-sS", "--max-time", str(timeout),
                 "-x", f"socks5h://127.0.0.1:{SOCKS_PORT}", TRACE_URL],
                capture_output=True, text=True, timeout=timeout + 5,
            )
            if res.returncode == 0:
                return "warp=on" in res.stdout
            _log(f"curl trace check failed (rc={res.returncode}): {(res.stderr or '')[-200:]}")
            return False
        except Exception as exc:
            _log(f"curl trace check error: {exc}")
            return False
    # 2) PySocks fallback: plain socket through the proxy + TLS.
    try:
        import socks  # PySocks (a bot dependency, but optional here)
        import http.client
        import ssl

        sock = socks.socksocket()
        sock.set_proxy(socks.SOCKS5, "127.0.0.1", SOCKS_PORT)
        sock.settimeout(timeout)
        sock.connect(("www.cloudflare.com", 443))
        ctx = ssl.create_default_context()
        tls = ctx.wrap_socket(sock, server_hostname="www.cloudflare.com")
        conn = http.client.HTTPConnection("www.cloudflare.com", 443, timeout=timeout)
        conn.sock = tls
        conn.request("GET", "/cdn-cgi/trace", headers={"Host": "www.cloudflare.com"})
        body = conn.getresponse().read().decode("utf-8", "ignore")
        conn.close()
        return "warp=on" in body
    except ImportError:
        return None
    except Exception as exc:
        _log(f"PySocks trace check failed: {exc}")
        return False


def _proxy_works() -> bool:
    """Decide whether the SOCKS5 proxy on 127.0.0.1:1080 is a live WARP
    egress. readyz (tunnel alive) is the stdlib check; the trace
    (warp=on) is the definitive one when curl/PySocks is available."""
    if _readyz_ok(timeout=10):
        return True
    _log("readyz did not confirm the tunnel, trying the trace check ...")
    traced = _trace_warp_on(timeout=15)
    if traced is True:
        return True
    if traced is None:
        _log("no way to verify the proxy (no curl, no PySocks) and readyz failed")
    return False


def _start_and_verify(wireproxy: Path, conf: Path, warp_dir: Path) -> bool:
    """Start wireproxy with *conf*, wait for its SOCKS port, verify it
    is really WARP. Returns True on success. On failure the process
    is left in _PROC for the caller to stop with stop_warp() (except
    when it already exited, where _PROC is cleared)."""
    global _PROC
    log_path = warp_dir / "wireproxy.log"
    try:
        log_f = open(log_path, "ab")
    except OSError as exc:
        _log(f"could not open wireproxy log: {exc}")
        return False
    try:
        _PROC = subprocess.Popen(
            [str(wireproxy), "-c", str(conf), "-i", f"127.0.0.1:{INFO_PORT}"],
            stdout=log_f, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as exc:
        _log(f"could not start wireproxy: {exc}")
        _PROC = None
        return False
    finally:
        # The child keeps its own fd; closing ours is safe.
        try:
            log_f.close()
        except Exception:
            pass

    # Wait for the SOCKS listener, then verify it is really WARP.
    deadline = time.monotonic() + _START_TIMEOUT
    while time.monotonic() < deadline:
        if _PROC.poll() is not None:
            _log(f"wireproxy exited early (rc={_PROC.returncode}), see {log_path}")
            _PROC = None
            return False
        if _port_open(SOCKS_PORT):
            break
        time.sleep(0.5)
    else:
        _log("wireproxy did not open the SOCKS port in time")
        return False

    return _proxy_works()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ensure_warp() -> str | None:
    """Start WARP (first call only) and return the SOCKS5 proxy URL
    (``socks5://127.0.0.1:1080``) when it is up, else ``None`` (the
    caller then uses direct egress). Never raises. Safe to call from
    any thread; setup runs once and the result is cached (a failed
    setup is retried at most once per ``_DOWN_COOLDOWN`` seconds)."""
    global _PROC, _ADOPTED, _STATUS, _LAST_ATTEMPT
    with _LOCK:
        now = time.monotonic()
        if _STATUS == "up":
            # Our process still alive, or an adopted proxy still serving.
            if _PROC is not None and _PROC.poll() is not None:
                _STATUS, _PROC = "down", None
                _log("wireproxy died, will retry on next download")
            elif _port_open(SOCKS_PORT):
                return SOCKS_URL
            else:
                _STATUS = "down"
                _log("SOCKS port closed, WARP is down")
        if _STATUS == "down" and now - _LAST_ATTEMPT < _DOWN_COOLDOWN:
            return None
        _LAST_ATTEMPT = now

        # A previous bot process may have left a working wireproxy
        # behind (unclean restart). Adopt it instead of failing on a
        # busy port.
        if _port_open(SOCKS_PORT) and _trace_warp_on(timeout=15) is True:
            _ADOPTED, _STATUS = True, "up"
            _log("adopted an already-running WARP proxy")
            return SOCKS_URL

        warp_dir = _warp_dir()
        bins = _ensure_binaries(warp_dir)
        if bins is None:
            _STATUS = "down"
            return None
        _wgcf, wireproxy = bins

        profile = _ensure_account(_wgcf, warp_dir)
        if profile is None:
            _STATUS = "down"
            return None
        keys = _parse_wgcf_profile(profile)
        if keys is None:
            _STATUS = "down"
            return None
        # Try the profile endpoint, then the same host on WARP's other
        # UDP ports, then the raw-IP form. On FadeHost the first
        # deploy proved the tunnel never establishes on 2408 (readyz
        # fails, trace gets SOCKS 0x04 Host unreachable) — most
        # likely outbound UDP 2408 is blocked, so another port may
        # work. One setup run only; the result is cached.
        for ep in _candidate_endpoints(keys["Peer.Endpoint"]):
            _log(f"trying WARP endpoint {ep} ...")
            conf = _build_wireproxy_conf(profile, warp_dir / "wireproxy.conf", endpoint=ep)
            if conf is None:
                continue
            if _start_and_verify(wireproxy, conf, warp_dir):
                _STATUS = "up"
                _log(f"WARP is up (endpoint {ep}; SOCKS5 {SOCKS_URL}, HTTP {HTTP_URL})")
                return SOCKS_URL
            _log(f"WARP endpoint {ep} did not verify")
            stop_warp()
        _dump_wireproxy_log_tail(warp_dir)
        _log("WARP proxy did not verify on any endpoint (no warp=on), falling back to direct")
        _STATUS = "down"
        return None


def warp_http_proxy() -> str | None:
    """HTTP proxy URL for tools that cannot do SOCKS5 (pytubefix uses
    stdlib urllib, whose ProxyHandler only speaks plain HTTP CONNECT).
    Returns the URL only when WARP is up, else None."""
    if ensure_warp() is None:
        return None
    return HTTP_URL


def warp_ok() -> bool:
    """True when the WARP proxy is up right now."""
    return ensure_warp() is not None


def stop_warp() -> None:
    """Stop the wireproxy subprocess we started (never kills an adopted
    one). Safe to call any time."""
    global _PROC, _ADOPTED, _STATUS
    with _LOCK:
        proc, _PROC = _PROC, None
        _ADOPTED = False
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if _STATUS == "up":
            _STATUS = "down"
