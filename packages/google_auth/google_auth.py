"""Shared Google OAuth plumbing for the google_* tool packages.

One OAuth client (credentials.json) authorizes any number of accounts; each
one's token lands next to this file as ``token_<account_name>.json``. Nothing
here knows or cares what an account is called -- the names are whatever the
user picked, and the set of them is simply whatever tokens exist on disk.

There are two ways to authorize, because one of them cannot work everywhere:

  * ``authorize_with_local_browser`` opens a consent page and spins up a
    throwaway HTTP listener for Google to redirect back to. Both of those
    happen on the machine the *backend* runs on, so this only works when that
    is also the machine you are sitting at. On a server it fails in a
    thoroughly confusing way: Google sends the browser to ``localhost``, which
    is the browser's own machine, where nothing is listening.

  * ``begin_authorization`` / ``complete_authorization`` hand you a URL to open
    anywhere and take back the address Google redirected to. Nothing needs to
    be reachable from anywhere, so this is the flow that works over SSH, and
    the only one the web dashboard offers.

The manual flow is deliberately split into two calls that share nothing but a
plain-JSON state dict: the flow object is rebuilt from credentials.json on the
second call, so no session has to stay alive in between and the dashboard can
run the two halves as two separate HTTP requests.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Google often echoes the granted scopes back in a different order, or adds one
# it grants implicitly; oauthlib treats any difference as an error and the
# whole consent fails at the very last step. setdefault so an operator who
# genuinely wants the strict behaviour can still ask for it.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/contacts",
    "https://mail.google.com/",
]

_PACKAGE_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = _PACKAGE_DIR / "credentials.json"
_CONFIG_FILE = _PACKAGE_DIR / "config.json"

# A desktop OAuth client may redirect to http://localhost on any port, with no
# pre-registration. In the manual flow nothing ever listens here -- the browser
# failing to load it is the expected outcome, and the address bar is where the
# authorization code comes from.
MANUAL_REDIRECT_URI = "http://localhost:47111/"

_ACCOUNT_NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]+")


def normalize_account_name(account_name: str) -> str:
    """Validate an account name. It becomes part of a file name, so anything
    that could walk out of this directory is refused rather than sanitised."""
    name = (account_name or "").strip()
    if not name:
        raise ValueError("an account name is required")
    if not _ACCOUNT_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "account names may only contain letters, digits, dots, dashes and "
            "underscores"
        )
    return name


def scopes() -> list[str]:
    """The scopes to request.

    Configurable so an install that only wants Calendar need not hand Rona
    full mailbox access. Defaults to all three, which is what the packages
    collectively need. Narrowing this invalidates existing tokens -- Google
    has to be asked again for the smaller set.
    """
    try:
        data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return list(DEFAULT_SCOPES)
    configured = data.get("scopes")
    if isinstance(configured, list) and configured:
        return [str(scope) for scope in configured]
    return list(DEFAULT_SCOPES)


def _token_file(account_name: str) -> Path:
    return _PACKAGE_DIR / f"token_{account_name}.json"


def _build_flow(redirect_uri: str | None = None) -> InstalledAppFlow:
    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            "credentials.json is missing. Install the google_auth package with a "
            "valid OAuth client credentials.json first."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), scopes())
    if redirect_uri:
        flow.redirect_uri = redirect_uri
    return flow


def _save(account_name: str, creds) -> Path:
    path = _token_file(account_name)
    path.write_text(creds.to_json(), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Using an account
# ---------------------------------------------------------------------------


def get_google_credentials(account_name: str, allow_browser: bool = True):
    """Credentials for one account, refreshing an expired token if it can."""
    account_name = normalize_account_name(account_name)
    token_file = _token_file(account_name)
    creds = None

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), scopes())

    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:  # noqa: BLE001
            creds = None
        else:
            # Write the refreshed token back. Not doing so meant refreshing
            # again on every single call, for the lifetime of the install.
            _save(account_name, creds)

    if creds and creds.valid:
        return creds

    if not allow_browser:
        raise RuntimeError(
            f"Account '{account_name}' has no valid credentials and needs "
            "re-authorization. Add it again from the dashboard (Settings -> "
            "Connections -> Tool Packages) or run: "
            "rona tools run google_auth add_account"
        )

    return authorize_with_local_browser(account_name)


# ---------------------------------------------------------------------------
# Authorizing an account
# ---------------------------------------------------------------------------


def authorize_with_local_browser(account_name: str):
    """Consent using *this machine's* browser.

    Only useful when the backend runs on the machine you are sitting at: both
    the browser and the redirect target are this host's, not yours.
    """
    account_name = normalize_account_name(account_name)
    flow = _build_flow()
    print(f"[{account_name}] waiting for browser approval...")
    creds = flow.run_local_server(port=0)
    _save(account_name, creds)
    return creds


def begin_authorization(account_name: str) -> tuple[str, dict]:
    """Start the manual flow.

    Returns the URL to open (anywhere at all) and a plain-JSON state dict to
    hand back to ``complete_authorization`` unchanged.
    """
    account_name = normalize_account_name(account_name)
    flow = _build_flow(MANUAL_REDIRECT_URI)
    auth_url, oauth_state = flow.authorization_url(
        access_type="offline", prompt="consent"
    )
    return auth_url, {
        "account_name": account_name,
        "redirect_uri": MANUAL_REDIRECT_URI,
        "oauth_state": oauth_state,
        # PKCE: authorization_url() generated a code verifier and sent only
        # its hash to Google. complete_authorization() rebuilds the flow from
        # scratch, so the verifier has to travel in the state -- without it
        # Google rejects the exchange with invalid_grant, and the failure
        # looks exactly like a mistyped code.
        "code_verifier": flow.code_verifier,
    }


def complete_authorization(state: dict, pasted: str) -> str:
    """Finish the manual flow and write the token. Returns the account name.

    ``pasted`` is the whole address the browser ended up on; a bare code is
    accepted too, since that is what people tend to try first.
    """
    state = state or {}
    account_name = normalize_account_name(state.get("account_name", ""))
    code = extract_code(pasted)
    flow = _build_flow(state.get("redirect_uri") or MANUAL_REDIRECT_URI)
    code_verifier = state.get("code_verifier")
    if code_verifier:
        # Must be the very verifier begin_authorization() hashed into the
        # consent URL; a freshly generated one would fail the PKCE check.
        flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    _save(account_name, flow.credentials)
    return account_name


def extract_code(pasted: str) -> str:
    """Pull the authorization code out of whatever the user pasted."""
    value = (pasted or "").strip()
    if not value:
        raise ValueError("nothing was pasted")
    if "://" not in value and "code=" not in value:
        return value  # already just the code
    query = urlparse(value).query if "://" in value else value.lstrip("?")
    params = parse_qs(query)
    error = params.get("error", [""])[0]
    if error:
        raise ValueError(f"Google returned '{error}' instead of an authorization code")
    code = params.get("code", [""])[0]
    if not code:
        raise ValueError(
            "no 'code' in what you pasted -- copy the whole address out of the "
            "browser's address bar, error page and all"
        )
    return code


# ---------------------------------------------------------------------------
# Managing authorized accounts
# ---------------------------------------------------------------------------


def list_accounts() -> list[str]:
    """Every account with a token on disk, in name order."""
    names = []
    for path in _PACKAGE_DIR.glob("token_*.json"):
        name = path.stem.removeprefix("token_")
        if name:
            names.append(name)
    return sorted(names)


def account_statuses() -> list[dict]:
    """``list_accounts`` plus whether each token is still usable."""
    statuses = []
    for name in list_accounts():
        try:
            creds = Credentials.from_authorized_user_file(
                str(_token_file(name)), scopes()
            )
        except Exception as exc:  # noqa: BLE001
            statuses.append(
                {"name": name, "usable": False, "detail": f"unreadable token: {exc}"}
            )
            continue
        if creds.valid:
            detail = "authorized"
        elif creds.expired and creds.refresh_token:
            detail = "expired, refreshes automatically on next use"
        else:
            detail = "needs re-authorization"
        statuses.append(
            {
                "name": name,
                "usable": bool(creds.valid or creds.refresh_token),
                "detail": detail,
            }
        )
    return statuses


def remove_account(account_name: str) -> bool:
    """Delete one account's token. False if there was nothing to delete."""
    account_name = normalize_account_name(account_name)
    path = _token_file(account_name)
    if not path.is_file():
        return False
    path.unlink()
    return True


def get_registered_accounts() -> dict:
    """Kept for anything still calling the older name."""
    return {"success": True, "registered_accounts": list_accounts()}
