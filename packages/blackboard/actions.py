"""Operator-facing actions for the Blackboard Learn package.

These are the things a human does to this package -- log in, check whether
the session still works, forget it -- never something the model calls.
Resolved through the ``actions`` list in manifest.json and driven from
`rona tools run blackboard <action>` or the dashboard's Tool Packages panel.

Every handler takes ``(config, params, state)`` and returns a status dict;
``import_session`` is the only two-step one, following the same
``input_required`` -> second call pattern google_auth's ``add_account`` uses.
"""

from __future__ import annotations

from datetime import datetime, timezone

from toolbox.custom.blackboard import bb_auth, bb_session
from toolbox.custom.blackboard.bb_config import normalize_base_url

_COOKIE_FIELD = {
    "key": "cookie_text",
    "label": "Pasted cookies",
    "description": (
        "The value of the 'Cookie' request header from a logged-in Blackboard "
        "tab (DevTools -> Network -> any request -> Headers), or a JSON export "
        "from a cookie-export browser extension."
    ),
    "type": "string",
    "required": True,
}


def login(config, params, state):
    result = bb_auth.login_direct(config)
    if result.get("success"):
        return {"status": "ok", "message": result.get("message", "logged in")}
    return {"status": "error", "message": result.get("error", "login failed")}


def login_sso(config, params, state):
    result = bb_auth.login_sso(config)
    if result.get("success"):
        return {"status": "ok", "message": result.get("message", "logged in")}
    return {"status": "error", "message": result.get("error", "login failed")}


def import_session(config, params, state):
    base_url = normalize_base_url(config.get("base_url"))
    if not base_url:
        return {"status": "error", "message": "set 'base_url' first"}

    if state is None:
        return {
            "status": "input_required",
            "message": (
                "Open Blackboard in your own browser and make sure you're "
                "logged in. Then open DevTools (F12) -> Network tab, click any "
                f"request to {base_url}, and copy the value of its 'Cookie' "
                "request header. Paste that whole value below. (A JSON export "
                "from a cookie-export extension works too.)"
            ),
            "fields": [_COOKIE_FIELD],
            "state": {"step": "paste"},
        }

    try:
        cookies = bb_auth.parse_cookie_text(params.get("cookie_text", ""))
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}

    jar = bb_session.cookies_to_jar(cookies)
    if not bb_auth.validate_cookies(base_url, jar):
        return {
            "status": "error",
            "message": (
                "those cookies don't look like a valid Blackboard session for "
                f"{base_url} -- make sure you copied them while logged in "
                "there, and that base_url matches exactly (no trailing slash)"
            ),
        }

    bb_session.save_session(base_url, cookies)
    return {
        "status": "ok",
        "message": "session imported and saved",
        "data": {"cookie_count": len(cookies)},
    }


def session_status(config, params, state):
    base_url = normalize_base_url(config.get("base_url"))
    if not base_url:
        return {"status": "error", "message": "set 'base_url' first"}

    info = bb_session.session_info()
    if info is None or info.get("base_url") != base_url:
        return {
            "status": "ok",
            "message": (
                "No session saved for this Blackboard URL yet. Run 'login', "
                "'login_sso' or 'import_session'."
            ),
            "data": {"authenticated": False},
        }

    cookies = bb_session.load_session(base_url) or []
    jar = bb_session.cookies_to_jar(cookies)
    valid = bb_auth.validate_cookies(base_url, jar)
    saved_at = info.get("saved_at")
    saved_desc = (
        datetime.fromtimestamp(saved_at, tz=timezone.utc).isoformat()
        if saved_at
        else "unknown"
    )
    status_word = "Valid" if valid else "Expired or invalid"
    return {
        "status": "ok",
        "message": (
            f"{status_word} session for {base_url}, saved {saved_desc}, "
            f"{info.get('cookie_count', 0)} cookie(s)."
        ),
        "data": {"authenticated": valid, "base_url": base_url, "saved_at": saved_at},
    }


def logout(config, params, state):
    if bb_session.clear_session():
        return {"status": "ok", "message": "session deleted"}
    return {"status": "ok", "message": "no session was stored"}


def install_browser(config, params, state):
    import subprocess
    import sys

    print("Downloading Chromium for Playwright (this can take a minute)...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=900,
            check=False,  # the returncode is inspected below
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": f"could not run playwright install: {exc}"}

    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "playwright install failed").strip()
        return {"status": "error", "message": tail[-4000:]}
    return {"status": "ok", "message": "Chromium installed."}
