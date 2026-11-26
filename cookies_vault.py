from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


class CookieError(Exception):
    pass


def validate_netscape(content: str) -> str:
    if not isinstance(content, str) or not content.strip():
        raise CookieError("cookie file is empty")

    rows: list[str] = []
    for line in content.splitlines():
        if not line.strip():
            continue
        if line.startswith("#HttpOnly_"):
            rows.append(line[1:])
            continue
        if line.startswith("#"):
            continue
        rows.append(line)

    if not rows:
        raise CookieError("no cookie rows found")

    for row in rows:
        parts = row.split("\t")
        if len(parts) < 7:
            raise CookieError("invalid cookie row: expected 7 tab-separated fields")
        if parts[1] not in ("TRUE", "FALSE"):
            raise CookieError("invalid cookie flag")
        if parts[3] not in ("TRUE", "FALSE"):
            raise CookieError("invalid secure flag")
        if not parts[5]:
            raise CookieError("cookie row has no name")

    return content.strip() + "\n"


@dataclass
class CookieEntry:
    name: str
    content: str
    failures: int = 0
    disabled: bool = False
    added_at: float = field(default_factory=time.time)
    last_used: float = 0.0

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "failures": self.failures,
            "disabled": self.disabled,
            "added_at": self.added_at,
            "last_used": self.last_used,
        }

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "CookieEntry":
        return cls(
            name=name,
            content=validate_netscape(data["content"]),
            failures=int(data.get("failures", 0)),
            disabled=bool(data.get("disabled", False)),
            added_at=float(data.get("added_at", time.time())),
            last_used=float(data.get("last_used", 0.0)),
        )


class CookieVault:
    def __init__(self, path: str | os.PathLike | None = None, env_key: str = "COOKIES_JSON"):
        self.path = Path(path or os.environ.get("COOKIE_STORE", "cookies/vault.json"))
        self.env_key = env_key
        self.gist_id = os.environ.get("COOKIES_GIST_ID", "")
        self.gist_token = os.environ.get("COOKIES_GIST_TOKEN", "")
        self._lock = threading.RLock()
        self._cursor = 0
        self._entries: dict[str, CookieEntry] = {}
        self._load()

    def _gist_request(self, payload: dict | None = None) -> dict:
        url = f"https://api.github.com/gists/{self.gist_id}"
        headers = {
            "Authorization": f"Bearer {self.gist_token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "cookie-vault",
        }
        data = None
        method = "GET"
        if payload is not None:
            method = "PATCH"
            data = json.dumps({"files": {"cookies.json": {"content": payload["content"]}}}).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    def _load_from_gist(self) -> dict | None:
        if not self.gist_id or not self.gist_token:
            return None
        try:
            response = self._gist_request()
            content = response["files"]["cookies.json"]["content"]
            return json.loads(content)
        except Exception:
            return None

    def _decode_env(self) -> dict | None:
        raw = os.environ.get(self.env_key)
        if not raw:
            return None
        raw = raw.strip()
        if not raw.startswith("{"):
            try:
                raw = base64.b64decode(raw, validate=True).decode("utf-8")
            except Exception as exc:
                raise CookieError("COOKIES_JSON is neither JSON nor base64 JSON") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CookieError("COOKIES_JSON is not valid JSON") from exc
        if not isinstance(data, dict):
            raise CookieError("COOKIES_JSON must be an object")
        return data

    def _load(self) -> None:
        data = self._load_from_gist()
        if data is None:
            data = self._decode_env()
        if data is None and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CookieError(f"cannot read cookie store {self.path}") from exc
        if not data:
            return

        entries = data.get("cookies", data) if isinstance(data.get("cookies", None), dict) else data
        for name, raw in entries.items():
            if not isinstance(raw, dict) or "content" not in raw:
                continue
            self._entries[str(name)] = CookieEntry.from_dict(str(name), raw)

    def save(self) -> None:
        with self._lock:
            payload = {
                "version": 1,
                "cookies": {name: entry.to_dict() for name, entry in self._entries.items()},
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(self.path.suffix + ".tmp")
            serialized = json.dumps(payload, ensure_ascii=False)
            temp.write_text(serialized, encoding="utf-8")
            os.chmod(temp, 0o600)
            temp.replace(self.path)
            if self.gist_id and self.gist_token:
                self._gist_request({"content": serialized})

    def to_env_json(self) -> str:
        with self._lock:
            return json.dumps(
                {"version": 1, "cookies": {n: e.to_dict() for n, e in self._entries.items()}},
                ensure_ascii=False,
                separators=(",", ":"),
            )

    def add(self, name: str, content: str, *, replace: bool = False) -> None:
        name = name.strip()
        if not name:
            raise CookieError("name cannot be empty")
        content = validate_netscape(content)
        with self._lock:
            if name in self._entries and not replace:
                raise CookieError(f"cookie '{name}' already exists")
            self._entries[name] = CookieEntry(name=name, content=content)
            self.save()

    def remove(self, name: str) -> bool:
        with self._lock:
            if name not in self._entries:
                return False
            del self._entries[name]
            self.save()
            return True

    def get(self, name: str) -> CookieEntry:
        with self._lock:
            if name not in self._entries:
                raise CookieError(f"cookie '{name}' not found")
            return self._entries[name]

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._entries)

    def summary(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "name": e.name,
                    "disabled": e.disabled,
                    "failures": e.failures,
                    "last_used": e.last_used,
                }
                for _, e in sorted(self._entries.items())
            ]

    def export(self, name: str, destination: str | os.PathLike) -> Path:
        entry = self.get(name)
        path = Path(destination)
        path.write_text(entry.content, encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def iter_available(self) -> Iterator[CookieEntry]:
        with self._lock:
            available = sorted((e for e in self._entries.values() if not e.disabled), key=lambda e: e.name)
            if not available:
                available = sorted(self._entries.values(), key=lambda e: e.name)
            if not available:
                raise CookieError("no cookies stored")
            start = self._cursor % len(available)
            ordered = available[start:] + available[:start]
            self._cursor = (self._cursor + 1) % len(available)
        return iter(ordered)

    def mark_success(self, name: str) -> None:
        with self._lock:
            entry = self.get(name)
            entry.failures = 0
            entry.disabled = False
            entry.last_used = time.time()
            self.save()

    def mark_failure(self, name: str, threshold: int = 3) -> None:
        with self._lock:
            entry = self.get(name)
            entry.failures += 1
            if entry.failures >= threshold:
                entry.disabled = True
            self.save()

    def enable(self, name: str) -> None:
        with self._lock:
            entry = self.get(name)
            entry.disabled = False
            entry.failures = 0
            self.save()

    @contextmanager
    def cookie_file(self, name: str) -> Iterator[Path]:
        content = self.get(name).content
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.txt"
            path.write_text(content, encoding="utf-8")
            os.chmod(path, 0o600)
            yield path
