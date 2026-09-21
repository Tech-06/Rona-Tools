"""Signing in to Blackboard Learn.

Three independent ways in, because no single one works everywhere:

  * ``login_direct`` fills Blackboard's own username/password form. Runs
    headless, so it's the only one safe to run unattended on a server --  but
    it only exists on institutions that haven't put their Blackboard behind
    single sign-on.

  * ``login_sso`` opens a *visible* browser on this machine and simply waits
    for a valid session to appear, however that happens: SSO, MFA, a
    passkey, whatever your institution's identity provider asks for. There is
    no URL pattern to match against here on purpose -- Entra ID, Shibboleth,
    CAS and every campus's own branded login page all land somewhere
    different, and the earlier draft this replaces broke on anything that
    wasn't its one author's own university (it waited for a Learn *Ultra*
    URL specifically, matched only English button text on the consent
    popup, and hardcoded that one school's login field names). Only useful
    when you're sitting at the machine the backend runs on.

  * ``parse_cookie_text`` / ``validate_cookies`` back the "import a session"
    action: paste cookies copied out of your own browser. Works anywhere,
    including over SSH, and needs no browser automation at all.

None of this runs at import time, and none of it is imported by
blackboard_tool.py -- only by actions.py, which the host loads lazily, only
when a human actually runs an action. That's deliberate: the draft this
replaces built its Playwright-backed auth object at module import, and Rona
imports every tool module at process startup, so a missing password made the
whole backend hang on a stdin prompt before it ever started serving requests.
"""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from toolbox.custom.blackboard import bb_session
from toolbox.custom.blackboard.bb_config import normalize_base_url

_USERS_ME_PATH = "/learn/api/v1/users/me"
_LOGIN_PATH = "/webapps/login/"
_NONCE_FIELD = "blackboard.platform.security.NonceUtil.nonce.ajax"

_MFA_ERROR = (
    "this account requires multi-factor authentication, which this login "
    "path does not support yet -- use the 'login_sso' or 'import_session' "
    "action instead"
)

# Blackboard's "Privacy, cookies and terms of use" lightbox is not a real
# consent flow -- its OK button just runs cookieConsent.agree(), which sets
# this one cookie (with a 10-year expiry) and closes itself. Setting it
# ourselves before the first request means the modal never renders in the
# first place, on any tenant, in any language -- no button to find or click.
CONSENT_COOKIE_NAME = "COOKIE_CONSENT_ACCEPTED"

_CONSENT_BUTTON_TEXTS = (
    "Accept All",
    "Accept all cookies",
    "Accept",
    "I Agree",
    "Agree",
    "Got it",
    "OK",
    "Ok",
    "Kabul Et",
    "Tümünü Kabul Et",
    "Kabul ediyorum",
    "Onayla",
    "Tamam",
)

# Blackboard's own default login template (bbLogin.jsp) uses user_id /
# password / #entry-login; that isn't one institution's quirk; it's the
# out-of-the-box Learn Original template most direct-login sites still use.
_DEFAULT_LOGIN_FORM_CANDIDATES: list[dict[str, str]] = [
    {
        "username": 'input[name="user_id"]',
        "password": 'input[name="password"]',
        "submit": "#entry-login",
    },
    {
        "username": "#user_id",
        "password": "#password",
        "submit": 'button[type="submit"], input[type="submit"]',
    },
]


def validate_cookies(base_url: str, jar: dict[str, str]) -> bool:
    """True iff ``jar`` is a currently-working Blackboard session."""
    if not jar:
        return False
    try:
        with httpx.Client(cookies=jar, follow_redirects=False, timeout=8.0) as client:
            response = client.get(f"{base_url}{_USERS_ME_PATH}")
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def parse_cookie_text(text: str) -> list[dict[str, Any]]:
    """Turn pasted cookie text into session storage's cookie-object shape.

    Accepts a JSON array (as a cookie-export browser extension produces) or a
    plain ``name=value; name2=value2`` Cookie-header string (as copied
    straight out of DevTools' Network or Application tab).
    """
    stripped = (text or "").strip()
    if not stripped:
        raise ValueError("nothing was pasted")

    if stripped.startswith("["):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"that looks like JSON but doesn't parse: {exc}") from exc
        if not isinstance(data, list):
            raise ValueError("expected a JSON array of cookie objects")
        cookies = []
        for item in data:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            cookies.append(
                {
                    "name": str(item["name"]),
                    "value": str(item.get("value", "")),
                    "domain": item.get("domain"),
                    "path": item.get("path", "/"),
                    "expires": item.get("expirationDate") or item.get("expires"),
                }
            )
        if not cookies:
            raise ValueError("that JSON array had no usable cookie objects in it")
        return cookies

    cookies = []
    for part in stripped.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if name:
            cookies.append(
                {"name": name, "value": value, "domain": None, "path": "/", "expires": None}
            )
    if not cookies:
        raise ValueError(
            "couldn't find any name=value cookies in that text -- paste either "
            "the 'Cookie' request header's value, or a JSON export from a "
            "cookie-export browser extension"
        )
    return cookies


def login_direct(config: dict) -> dict:
    """Headless username/password login. Safe to run on a server.

    Two independent attempts, cheapest first: ``_login_http`` posts straight
    to Blackboard's own login form with no browser at all, which is all the
    stock Learn Original template (bbLogin.jsp) needs. ``_login_browser``
    falls back to a headless Playwright fill for institutions whose login
    page has been customised enough that the HTTP path can't find or POST
    the form. Neither one understands SSO or MFA -- that's what 'login_sso'
    and 'import_session' are for -- so a failure from both is reported as
    one error naming both reasons, rather than only ever showing whichever
    path happened to run last.
    """
    base_url = normalize_base_url(config.get("base_url"))
    if not base_url:
        return {"success": False, "error": "set 'base_url' first"}

    username = (config.get("username") or "").strip()
    password = config.get("password") or ""
    if not username or not password:
        return {
            "success": False,
            "error": (
                "direct login needs both 'username' (in config) and a password "
                "(BB_PASSWORD, in Rona's .env) set first"
            ),
        }

    http_result = _login_http(config)
    if http_result.get("success"):
        return http_result
    if http_result.get("mfa_required"):
        return http_result

    browser_result = _login_browser(config)
    if browser_result.get("success"):
        return browser_result

    return {
        "success": False,
        "error": (
            f"HTTP login failed: {http_result.get('error', 'unknown error')}; "
            f"browser login failed: {browser_result.get('error', 'unknown error')}"
        ),
    }


def _login_http(config: dict) -> dict:
    """Plain-httpx username/password login: no browser, no Playwright.

    Blackboard's default login page (bbLogin.jsp) turned out, on inspection
    of a live tenant, to be an ordinary POST form -- not an SSO redirect --
    so there's no need to pay for a browser just to fill two fields and
    click submit. This only understands that one stock form shape though;
    anything it can't parse or that Blackboard rejects falls through to
    ``_login_browser`` (or is reported as needing SSO/MFA), never raises.

    Success is judged the same way ``_wait_for_session`` judges the browser
    path: not by scraping the response HTML for a "welcome" string, but by
    asking Blackboard itself whether the cookies we ended up with actually
    work (``validate_cookies``). The HTML is only parsed afterwards, and
    only to explain a *failure*.
    """
    base_url = normalize_base_url(config.get("base_url"))
    username = (config.get("username") or "").strip()
    password = config.get("password") or ""
    login_url = f"{base_url}{_LOGIN_PATH}"

    try:
        with httpx.Client(follow_redirects=True, timeout=30.0) as client:
            client.cookies.set(CONSENT_COOKIE_NAME, "true")

            get_response = client.get(login_url)
            nonce = _extract_nonce(get_response.text)

            form_data = {
                "user_id": username,
                "password": password,
                "login": "Login",
                "action": "login",
                "new_loc": "",
            }
            if nonce:
                form_data[_NONCE_FIELD] = nonce

            post_response = client.post(login_url, data=form_data)

            cookies = [
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "expires": cookie.expires,
                }
                for cookie in client.cookies.jar
            ]
            # cookies_to_jar already knows to prefer the root-scoped
            # duplicate of a same-named cookie over a path-scoped one, so
            # there's no need to dedupe here before handing it to
            # validate_cookies -- and the full list, duplicates included,
            # is still what gets saved on success.
            jar = bb_session.cookies_to_jar(cookies)
            if validate_cookies(base_url, jar):
                bb_session.save_session(base_url, cookies)
                return {"success": True, "message": "logged in and session saved"}

            error = _explain_http_login_failure(post_response.text)
            # Flagged separately so login_direct can skip the browser
            # fallback: it fills the very same form and would land on the
            # very same challenge, just half a minute later.
            if error == _MFA_ERROR:
                return {"success": False, "error": error, "mfa_required": True}
            return {"success": False, "error": error}
    except httpx.HTTPError as exc:
        return {"success": False, "error": f"network error reaching Blackboard: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"unexpected error: {exc}"}


def _extract_nonce(html: str) -> str | None:
    """Pull Blackboard's per-page-load login nonce out of the login form.

    Blackboard regenerates this hidden input
    (``blackboard.platform.security.NonceUtil.nonce.ajax``) on every GET of
    the login page and some tenants reject the POST without it. Returns
    None -- rather than raising -- on any page that doesn't have one, since
    plenty of tenants don't require it and the POST is fine without it.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    field = soup.find("input", attrs={"name": _NONCE_FIELD})
    if field is None:
        return None
    value = field.get("value")
    return value or None


def _explain_http_login_failure(html: str) -> str:
    """Turn a failed login POST's response HTML into a specific error.

    Blackboard's login POST comes back 200 whether it succeeded or not --
    there's no status code to branch on -- so the only way to say anything
    more useful than "it didn't work" is to look at what the page rendered.
    """
    soup = BeautifulSoup(html or "", "html.parser")

    if _is_rendered_mfa_prompt(soup):
        return _MFA_ERROR

    error_selectors = (
        "#loginError",
        ".loginError",
        "[id*=error]",
        "[class*=error]",
        ".errorMessage",
    )
    for selector in error_selectors:
        element = soup.select_one(selector)
        if element is not None:
            text = element.get_text(strip=True)
            if text:
                return f"Blackboard rejected the login: {text}"

    return "credentials were rejected or the login form changed"


def _is_rendered_mfa_prompt(soup: BeautifulSoup) -> bool:
    """True if the response is an actual MFA challenge, not just Blackboard's
    always-present-but-inert MFA template markup.

    The login page ships ``secondaryAuthToken`` / ``totp-verification-input``
    and the hidden ``showMFA*`` flags on every tenant, MFA or not. On a page
    where MFA isn't active, those flags hold the literal, unsubstituted
    template text (``$showMFARegistration`` and friends) rather than a real
    value -- so their mere presence in the markup can't be the signal, or
    every non-MFA account would be misreported as needing MFA.
    """
    has_token_field = (
        soup.find(id="totp-verification-input") is not None
        or soup.find(attrs={"name": "secondaryAuthToken"}) is not None
    )
    if not has_token_field:
        return False

    flag_names = ("showMFARegistration", "showMFAVerification", "showMFASuccessFul")
    flags = [
        soup.find(attrs={"name": name}) or soup.find(id=name) for name in flag_names
    ]
    flags = [flag for flag in flags if flag is not None]
    if not flags:
        return False

    return not any((flag.get("value") or "").startswith("$") for flag in flags)


def _consent_cookie_for_playwright(base_url: str) -> dict[str, str]:
    """The same cookie-consent shortcut as ``_login_http``, shaped for
    ``BrowserContext.add_cookies``."""
    domain = urlparse(base_url).hostname or ""
    return {"name": CONSENT_COOKIE_NAME, "value": "true", "domain": domain, "path": "/"}


def _login_browser(config: dict) -> dict:
    """Headless username/password login via a real (headless) browser.

    The fallback for ``_login_http``: institutions whose login page has been
    customised past the stock bbLogin.jsp shape -- extra JavaScript, a
    reworked form -- may still work here, since this drives an actual
    rendered page with Playwright's selectors instead of parsing raw HTML.
    """
    base_url = normalize_base_url(config.get("base_url"))
    username = (config.get("username") or "").strip()
    password = config.get("password") or ""

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "success": False,
            "error": (
                "Playwright isn't installed. Run the 'install_browser' action, "
                "or use 'import_session' instead, which doesn't need it."
            ),
        }

    candidates = _selector_candidates(config)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                context.add_cookies([_consent_cookie_for_playwright(base_url)])
                page = context.new_page()
                page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
                _dismiss_consent_popups(page)

                filled = False
                for candidate in candidates:
                    try:
                        user_field = page.locator(candidate["username"]).first
                        user_field.wait_for(state="visible", timeout=4000)
                        user_field.fill(username)
                        page.locator(candidate["password"]).first.fill(password)
                        page.locator(candidate["submit"]).first.click()
                        filled = True
                        break
                    except PlaywrightError:
                        continue

                if not filled:
                    return {
                        "success": False,
                        "error": (
                            "could not find a direct login form on this page. Your "
                            "institution may use single sign-on instead -- try the "
                            "'login_sso' or 'import_session' action, or set the "
                            "login_*_selector config fields to match your "
                            "institution's login page."
                        ),
                    }

                cookies = _wait_for_session(context, base_url, timeout_s=20.0)
                if cookies is None:
                    return {
                        "success": False,
                        "error": (
                            "submitted the login form but never reached a valid "
                            "session within 20s -- double-check the username/"
                            "password, or this may actually need SSO/MFA (try "
                            "'login_sso' or 'import_session' instead)"
                        ),
                    }
                bb_session.save_session(base_url, cookies)
                return {"success": True, "message": "logged in and session saved"}
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"login failed: {exc}"}


def login_sso(config: dict, timeout_s: float = 300.0) -> dict:
    """Opens a visible browser and waits for the human at it to finish
    single sign-on (and MFA) however their institution asks for it."""
    base_url = normalize_base_url(config.get("base_url"))
    if not base_url:
        return {"success": False, "error": "set 'base_url' first"}

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "success": False,
            "error": (
                "Playwright isn't installed. Run the 'install_browser' action, "
                "or use 'import_session' instead, which doesn't need it."
            ),
        }

    print(
        f"Opening a browser window at {base_url} -- complete your institution's "
        f"sign-in there yourself (including MFA if it asks). Waiting up to "
        f"{int(timeout_s)}s for a valid session to appear..."
    )
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            try:
                context = browser.new_context()
                context.add_cookies([_consent_cookie_for_playwright(base_url)])
                page = context.new_page()
                page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
                _dismiss_consent_popups(page)

                cookies = _wait_for_session(context, base_url, timeout_s=timeout_s)
                if cookies is None:
                    return {
                        "success": False,
                        "error": (
                            f"no valid Blackboard session appeared within "
                            f"{int(timeout_s)}s, so the window was closed without "
                            "saving anything. Run this action again if you need "
                            "more time, or use 'import_session' instead."
                        ),
                    }
                bb_session.save_session(base_url, cookies)
                print("Signed in -- session saved.")
                return {"success": True, "message": "logged in and session saved"}
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"login failed: {exc}"}


def _selector_candidates(config: dict) -> list[dict[str, str]]:
    override_user = (config.get("login_username_selector") or "").strip()
    override_pass = (config.get("login_password_selector") or "").strip()
    override_submit = (config.get("login_submit_selector") or "").strip()
    if override_user and override_pass and override_submit:
        return [
            {"username": override_user, "password": override_pass, "submit": override_submit}
        ]
    return list(_DEFAULT_LOGIN_FORM_CANDIDATES)


def _dismiss_consent_popups(page, per_candidate_timeout_ms: int = 300) -> None:
    """Fallback only: click-based dismissal of a cookie/privacy banner.

    Setting ``CONSENT_COOKIE_NAME`` before ``page.goto`` (see
    ``_consent_cookie_for_playwright``) is what actually keeps Blackboard's
    "Privacy, cookies and terms of use" lightbox from rendering in the first
    place -- inspecting that modal's own OK button showed it does nothing
    but set that one cookie and close itself, so there was never anything to
    click if the cookie is already there. This function only remains for a
    Blackboard version that names its consent cookie differently: it tries a
    handful of common button labels in a few languages and clicks the first
    one that shows up, and never raises if none match. The draft this
    replaces matched only English text, so a Turkish-locale Blackboard's
    "Tamam"/"Kabul Et" banner silently blocked the login form.
    """
    for text in _CONSENT_BUTTON_TEXTS:
        try:
            button = page.locator(f'button:has-text("{text}"), a:has-text("{text}")').first
            button.wait_for(state="visible", timeout=per_candidate_timeout_ms)
            button.click(timeout=1000)
            return
        except Exception:  # noqa: BLE001,S112 -- most of these texts simply won't be present
            continue


def _wait_for_session(
    context, base_url: str, timeout_s: float, poll_interval_s: float = 1.5
) -> list[dict[str, Any]] | None:
    """Poll the browser context's cookies against Blackboard's own session
    check until they work, instead of guessing a post-login URL pattern.

    A URL pattern is exactly what made the earlier draft this replaces
    institution-specific: it waited for '**/ultra/**', which only exists on
    Blackboard Ultra tenants and only once *that* institution's SSO redirect
    chain happens to land there. Asking Blackboard itself whether the
    current cookies work is host-agnostic by construction.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        cookies = context.cookies()
        jar = {c["name"]: c["value"] for c in cookies}
        if validate_cookies(base_url, jar):
            return cookies
        time.sleep(poll_interval_s)
    return None
