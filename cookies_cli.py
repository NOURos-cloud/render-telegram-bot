from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

from cookies_vault import CookieError, CookieVault, validate_netscape


def _write_netscape(jar, destination: Path) -> tuple[str, int]:
    from http.cookiejar import MozillaCookieJar

    output = MozillaCookieJar(str(destination))
    count = 0
    for cookie in jar:
        if not any(domain in cookie.domain for domain in ("youtube.com", "google.com")):
            continue
        output.set_cookie(cookie)
        count += 1
    output.save(ignore_discard=True, ignore_expires=True)
    return destination.read_text(encoding="utf-8"), count


def _extract(args: argparse.Namespace, vault: CookieVault) -> None:
    try:
        module = importlib.import_module("browser_cookie3")
    except Exception as exc:
        raise CookieError(f"cannot load browser-cookie3 ({exc}); run extraction on a desktop with the browser installed") from exc

    loader = getattr(module, args.browser, None)
    if loader is None:
        raise CookieError(f"unsupported browser '{args.browser}'")

    kwargs = {}
    if args.profile:
        kwargs["cookie_file"] = args.profile
    jar = loader(**kwargs)

    temp = Path(".cookies_extract.tmp")
    try:
        content, count = _write_netscape(jar, temp)
    finally:
        temp.unlink(missing_ok=True)

    if count == 0:
        raise CookieError("no youtube/google cookies found; log in first")

    vault.add(args.name, content, replace=args.replace)
    print(f"saved {count} cookies as '{args.name}'")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Store and manage YouTube cookies for the bot")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list stored cookie profiles")

    add = sub.add_parser("add", help="import a Netscape cookies.txt file")
    add.add_argument("name")
    add.add_argument("file", type=Path)
    add.add_argument("--replace", action="store_true")

    remove = sub.add_parser("remove", help="delete a cookie profile")
    remove.add_argument("name")

    export = sub.add_parser("export", help="write a profile to a cookies.txt file")
    export.add_argument("name")
    export.add_argument("file", type=Path)

    extract = sub.add_parser("extract", help="pull cookies from a local browser session")
    extract.add_argument("name")
    extract.add_argument("--browser", default="chrome")
    extract.add_argument("--profile", default="")
    extract.add_argument("--replace", action="store_true")

    enable = sub.add_parser("enable", help="re-enable a disabled profile")
    enable.add_argument("name")

    sub.add_parser("env-json", help="print JSON for the COOKIES_JSON cloud variable")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    vault = CookieVault()
    try:
        if args.command == "list":
            rows = vault.summary()
            if not rows:
                print("no cookies stored")
            for row in rows:
                state = "disabled" if row["disabled"] else "active"
                print(f"{row['name']}\t{state}\tfailures={row['failures']}")
        elif args.command == "add":
            vault.add(args.name, validate_netscape(args.file.read_text(encoding="utf-8")), replace=args.replace)
            print(f"saved '{args.name}'")
        elif args.command == "remove":
            print("removed" if vault.remove(args.name) else "not found")
        elif args.command == "export":
            path = vault.export(args.name, args.file)
            print(f"wrote {path}")
        elif args.command == "extract":
            _extract(args, vault)
        elif args.command == "enable":
            vault.enable(args.name)
            print(f"enabled '{args.name}'")
        elif args.command == "env-json":
            print(vault.to_env_json())
        return 0
    except (CookieError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
