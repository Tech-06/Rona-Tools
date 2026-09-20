"""Operator-facing actions for google_auth.

These are the things a human does to this package: authorize an account, see
which ones are authorized, revoke one. They are not tools -- the model never
calls them. Rona resolves them through the ``actions`` list in manifest.json
and drives them from `rona tools run google_auth <action>` or from the
dashboard's Tool Packages panel.

Every handler takes ``(config, params, state)`` and returns a status dict.
``add_account`` is the interesting one: it answers "input_required" on the
first call with the consent URL, then completes on the second call with
whatever the user pasted back. Because the state it returns is plain JSON, the
two calls can be two separate HTTP requests -- which is exactly what makes
authorizing an account on a headless server possible at all.
"""

from toolbox.custom.google_auth import google_auth as auth

_PASTE_FIELD = {
    "key": "redirect_url",
    "label": "Pasted address",
    "description": "The whole address the browser ended up on, or just the code.",
    "type": "string",
    "required": True,
}


def _accounts_payload() -> dict:
    return {"accounts": auth.list_accounts()}


def add_account(config, params, state):
    """Authorize an account without needing a browser on the backend's host."""
    if state is None:
        try:
            name = auth.normalize_account_name(params.get("account_name", ""))
            auth_url, next_state = auth.begin_authorization(name)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": str(exc)}

        again = " (this replaces its existing authorization)" if name in auth.list_accounts() else ""
        return {
            "status": "input_required",
            "message": (
                f"Open this address and approve access for '{name}'{again}:\n\n"
                f"{auth_url}\n\n"
                "You can open it on any device -- it does not have to be the machine "
                "Rona runs on. After you approve, the browser will try to load "
                f"{next_state['redirect_uri']} and show a 'site can't be reached' "
                "error. That is expected: nothing is listening there, and the address "
                "bar is the point. Copy that whole address and paste it below."
            ),
            "fields": [_PASTE_FIELD],
            "state": next_state,
        }

    try:
        name = auth.complete_authorization(state, params.get("redirect_url", ""))
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": f"authorization failed: {exc}"}

    return {
        "status": "ok",
        "message": (
            f"'{name}' is authorized. Add it to a Google tool's allowed accounts "
            "for that tool to be able to use it."
        ),
        "data": {"account": name, **_accounts_payload()},
    }


def add_account_here(config, params, state):
    """Authorize using the browser on the machine the backend runs on.

    Useless (and slow to fail) anywhere else, which is why the manifest marks
    it cli_only -- it would block a dashboard request until the consent page
    was answered on a machine the person at the dashboard may not even have.
    """
    try:
        name = auth.normalize_account_name(params.get("account_name", ""))
        auth.authorize_with_local_browser(name)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": f"authorization failed: {exc}"}
    return {
        "status": "ok",
        "message": f"'{name}' is authorized.",
        "data": {"account": name, **_accounts_payload()},
    }


def list_accounts(config, params, state):
    """Show every authorized account and whether its token still works."""
    statuses = auth.account_statuses()
    if not statuses:
        return {
            "status": "ok",
            "message": "No accounts authorized yet.",
            "data": {"accounts": [], "statuses": []},
        }
    lines = [f"{entry['name']} -- {entry['detail']}" for entry in statuses]
    return {
        "status": "ok",
        "message": "\n".join(lines),
        "data": {"accounts": [e["name"] for e in statuses], "statuses": statuses},
    }


def remove_account(config, params, state):
    """Delete one account's token. The tools' allowed-account lists are the
    user's own config, so they are left alone on purpose."""
    try:
        name = auth.normalize_account_name(params.get("account_name", ""))
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    if not auth.remove_account(name):
        return {"status": "error", "message": f"no authorized account named '{name}'"}
    return {
        "status": "ok",
        "message": (
            f"'{name}' removed. Take it out of any tool's allowed accounts too, "
            "or that tool will keep offering an account it can no longer reach."
        ),
        "data": _accounts_payload(),
    }
