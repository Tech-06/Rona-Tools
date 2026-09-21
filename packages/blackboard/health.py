"""Health check for the Blackboard Learn package.

Not being logged in yet is a perfectly normal state right after install or
after a config change (base_url, say) -- login happens separately, from the
dashboard's Tool Packages panel or `rona tools run blackboard <action>` -- so
that is reported, not failed. Only an actually-broken session (present but
rejected by the server) counts as unhealthy.
"""

from __future__ import annotations

import httpx

from toolbox.custom.blackboard import bb_session
from toolbox.custom.blackboard.bb_config import normalize_base_url


def check(config: dict) -> dict:
    base_url = normalize_base_url(config.get("base_url"))
    if not base_url:
        return {"ok": True, "detail": "not configured yet -- set 'base_url' first"}

    cookies = bb_session.load_session(base_url)
    if not cookies:
        return {
            "ok": True,
            "detail": (
                "configured, not logged in yet -- run the 'login', 'login_sso' "
                "or 'import_session' action"
            ),
        }

    jar = bb_session.cookies_to_jar(cookies)
    try:
        with httpx.Client(cookies=jar, follow_redirects=False, timeout=10.0) as client:
            response = client.get(f"{base_url}/learn/api/v1/users/me")
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"could not reach {base_url}: {exc}"}

    if response.status_code == 200:
        return {"ok": True, "detail": "logged in and session is valid"}
    if response.status_code in (301, 302, 303, 307, 308, 401, 403):
        return {
            "ok": False,
            "detail": (
                "session has expired or was rejected -- run the 'login', "
                "'login_sso' or 'import_session' action again"
            ),
        }
    return {"ok": False, "detail": f"unexpected response: HTTP {response.status_code}"}
