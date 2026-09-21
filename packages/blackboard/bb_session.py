"""Session (cookie jar) storage for the Blackboard Learn package.

Blackboard has no refresh token the way OAuth does -- a login just hands back
a cookie jar that is valid until the server says otherwise. This is the only
module that reads or writes session.json; everything else goes through it.

The draft this package replaces flattened cookies to a bare {name: value}
dict, discarding domain/path/expiry, and saved it under whatever base_url
happened to be configured at the time with no record of which institution it
belonged to. If the base_url was ever repointed at a different school, the
old cookies would silently be sent to the new one. Here the full cookie
objects are kept and stamped with the base_url they were issued for, so a
session for the wrong institution is treated as no session at all rather
than sent somewhere it was never meant for.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Any

_PACKAGE_DIR = Path(__file__).resolve().parent
SESSION_FILE = _PACKAGE_DIR / "session.json"


def load_session(base_url: str) -> list[dict[str, Any]] | None:
    """Cookies saved for ``base_url``, or None if there is nothing usable.

    A session saved for a different base_url -- the configured institution
    changed -- is treated as absent rather than handed to a host it was
    never issued for.
    """
    data = _read()
    if data is None or data.get("base_url") != base_url:
        return None
    cookies = data.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        return None
    return cookies


def save_session(base_url: str, cookies: list[dict[str, Any]]) -> None:
    """Persist a cookie jar, replacing whatever was stored before."""
    payload = {
        "base_url": base_url,
        "saved_at": time.time(),
        "cookies": cookies,
    }
    SESSION_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    try:
        os.chmod(SESSION_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass  # best-effort; not every filesystem honours POSIX permission bits


def clear_session() -> bool:
    """Delete the stored session. False if there was nothing to delete."""
    if not SESSION_FILE.is_file():
        return False
    SESSION_FILE.unlink()
    return True


def session_info() -> dict[str, Any] | None:
    """Metadata about the stored session -- never the cookie values themselves,
    so this is safe to put in an action's message or a health check detail."""
    data = _read()
    if data is None:
        return None
    cookies = data.get("cookies")
    return {
        "base_url": data.get("base_url"),
        "saved_at": data.get("saved_at"),
        "cookie_count": len(cookies) if isinstance(cookies, list) else 0,
    }


def session_expires_at(base_url: str) -> float | None:
    """Unix timestamp the stored session's own ``BbRouter`` cookie says it
    dies at, or None if that can't be worked out.

    ``BbRouter`` packs its own metadata into the cookie *value* as
    comma-separated ``key:value`` pairs, one of which is ``expires`` -- a
    unix timestamp that, on a real session observed live, landed exactly 3
    hours after the session was saved. That makes the session's death time
    knowable purely from what's already on disk, with zero network calls,
    which is what lets bb_client refresh proactively before a request
    instead of only reacting after the server has already rejected one.

    None means "unknown" -- no session stored, no BbRouter cookie in it, no
    ``expires`` field in that cookie's value, or a value that doesn't parse
    as a number -- and callers must treat that the same as "don't know",
    never as "already expired". This never raises: any storage or parsing
    failure just falls into the None case.
    """
    try:
        cookies = load_session(base_url)
        if not cookies:
            return None
        value = cookies_to_jar(cookies).get("BbRouter")
        if not value:
            return None
        for part in value.split(","):
            key, _, val = part.partition(":")
            if key.strip() == "expires":
                return float(val.strip())
        return None
    except Exception:  # noqa: BLE001
        return None


def cookies_to_jar(cookies: list[dict[str, Any]]) -> dict[str, str]:
    """Flatten to the plain {name: value} mapping httpx's simple cookie jar
    takes. Storage keeps domain/path/expiry regardless -- this only discards
    them at the point of use.

    A login can leave more than one cookie with the same name at different
    paths -- observed live against Blackboard's own consent page, which
    scopes its own JSESSIONID to /webapps/privacy-disclosure separately from
    the root-scoped one every /learn/api/... call actually needs. When a
    name repeats, the root-scoped ("/") cookie always wins regardless of
    which one the browser happened to report first or last.
    """
    jar: dict[str, str] = {}
    jar_is_root: dict[str, bool] = {}
    for cookie in cookies:
        name = cookie.get("name")
        if not name:
            continue
        is_root = (cookie.get("path") or "/") == "/"
        if name not in jar or (is_root and not jar_is_root.get(name)):
            jar[name] = cookie["value"]
            jar_is_root[name] = is_root
    return jar


def _read() -> dict[str, Any] | None:
    if not SESSION_FILE.is_file():
        return None
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
