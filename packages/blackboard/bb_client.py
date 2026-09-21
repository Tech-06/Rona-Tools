"""Async HTTP layer for talking to Blackboard's own ``learn/api/v1`` REST
API with a saved session's cookies. This is the only module tool functions
use to make a request -- nothing in blackboard_tool.py calls httpx directly.

A live discovery session against a real Blackboard tenant (see the plan's
Phase 3) turned up two things worth recording here, because they shaped this
module's error handling:

  * A redirect or 401 reliably means the session is dead. A 403 does *not*
    -- a student's session can go on returning 200 from ``users/me`` and from
    every other course's endpoints while one specific course's gradebook
    starts (or stops) answering 403, with nothing this package did causing
    the change. Treating every 403 the same as an expired session -- which
    is what the draft this replaces effectively did, by not distinguishing
    them at all -- means either crying "please log in again" when nothing
    is wrong, or (worse) firing off a needless automatic re-login against a
    resource the account was simply never going to be allowed to see. So a
    403 gets a cheap follow-up probe against ``users/me`` first: still 200
    means "no permission for this one resource", anything else means the
    session really is gone.

  * ``paging.nextPage`` is a full relative URL with its query string already
    encoded (``/learn/api/v1/calendars/calendarItems?until=...%3A...``), not
    a bare offset/cursor value -- it's meant to be followed verbatim, never
    reassembled from its parts.

  * The session cookie (``BbRouter``) carries its own ``expires`` timestamp
    in its value (see ``bb_session.session_expires_at``), and that timestamp
    matched a real session's actual death to the second. So rather than only
    reacting once a request comes back 302/401/403, ``_request`` checks that
    local timestamp first and renews the session ahead of time when it's
    already gone or close to it -- no server round trip needed just to ask
    "is this about to expire". The reactive handling stays in place
    regardless, as the safety net for everything the local timestamp can't
    know about (a session revoked server-side, a clock that's off, and so
    on).
"""

from __future__ import annotations

import asyncio
import time
import weakref
from typing import Any

import httpx

from toolbox.custom.blackboard import bb_auth, bb_session
from toolbox.custom.blackboard.bb_config import DEFAULT_CACHE_TTL, normalize_base_url

_USERS_ME_PATH = "/learn/api/v1/users/me"
_REQUEST_TIMEOUT = 20.0

# How long before a session's known death time counts as "already close
# enough to renew now". Wide enough to absorb the time a request itself
# takes plus normal clock drift between this machine and Blackboard's.
_REFRESH_MARGIN_SECONDS = 120


class _LoopState:
    """The connection pool and locks belonging to one event loop.

    Both an ``httpx.AsyncClient`` and an ``asyncio.Lock`` bind themselves to
    the loop they are first used on, so a single module-level instance of
    either breaks the moment a second loop appears -- the client's pooled
    connections raise "Event loop is closed" and the lock raises "bound to a
    different event loop". Rona's backend only ever runs one loop, so that
    used to be invisible there, but anything calling these tools from a
    script or from more than one ``asyncio.run`` hit it immediately.
    Keeping this state per loop costs nothing and removes the whole class
    of failure.
    """

    def __init__(self) -> None:
        self.client: httpx.AsyncClient | None = None
        self.client_lock = asyncio.Lock()
        self.login_lock = asyncio.Lock()


# Weak keys so a finished loop's pool is not kept alive by this mapping.
_loop_states: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopState] = (
    weakref.WeakKeyDictionary()
)


def _state() -> _LoopState:
    """This loop's client/locks, created on first use. Must be called from
    inside a coroutine -- there is no meaningful answer without a running
    loop to key on."""
    loop = asyncio.get_running_loop()
    state = _loop_states.get(loop)
    if state is None:
        state = _LoopState()
        _loop_states[loop] = state
    return state


_CACHE: dict[tuple[str, str], tuple[float, Any]] = {}
_CACHE_MAX_ENTRIES = 200


class BlackboardError(Exception):
    """A request failed and there was nothing more this module could do
    about it on its own. Tool functions catch this and turn it into the
    package's usual ``{"success": False, "error": str}`` shape."""


class SessionExpiredError(BlackboardError):
    """No session is stored, or the stored one no longer authenticates at
    all (confirmed against ``users/me``, not guessed from one status code)."""


class PermissionDeniedError(BlackboardError):
    """The session is fine -- this specific resource just refused it."""


async def get_json(
    config: dict[str, Any],
    path: str,
    params: dict[str, Any] | None = None,
    *,
    use_cache: bool = True,
) -> dict[str, Any]:
    """GET one ``learn/api/v1`` endpoint and return its decoded JSON body."""
    cache_key = _cache_key(path, params)
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    response = await _request(config, "GET", path, params)
    try:
        data = response.json()
    except ValueError as exc:
        raise BlackboardError(f"Blackboard returned a non-JSON response: {exc}") from exc

    if use_cache:
        ttl = config.get("cache_ttl")
        _cache_set(cache_key, data, ttl if isinstance(ttl, int) else DEFAULT_CACHE_TTL)
    return data


async def get_all_results(
    config: dict[str, Any],
    path: str,
    params: dict[str, Any] | None = None,
    *,
    max_items: int = 1000,
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    """GET a ``{"results": [...], "paging": {...}}`` endpoint and follow
    ``paging.nextPage`` until it runs out, the server repeats a page, or
    ``max_items`` is reached -- whichever comes first."""
    data = await get_json(config, path, params, use_cache=use_cache)
    results = list(data.get("results", []))
    next_page = (data.get("paging") or {}).get("nextPage") or None
    seen = {path}

    while next_page and len(results) < max_items:
        if next_page in seen:
            break  # a server echoing the same page back would loop forever otherwise
        seen.add(next_page)
        page = await get_json(config, next_page, None, use_cache=use_cache)
        results.extend(page.get("results", []))
        next_page = (page.get("paging") or {}).get("nextPage") or None

    return results[:max_items]


def clear_cache() -> None:
    _CACHE.clear()


# ---------------------------------------------------------------------------
# Request plumbing
# ---------------------------------------------------------------------------


async def _request(
    config: dict[str, Any], method: str, path: str, params: dict[str, Any] | None
) -> httpx.Response:
    base_url = normalize_base_url(config.get("base_url"))
    if not base_url:
        raise BlackboardError("Blackboard is not configured yet -- set 'base_url' first")

    # Proactive renewal, read entirely off disk (no round trip) in two cases:
    #
    #   * there is no session at all. On a fresh server install with a
    #     username and password configured, the very first tool call should
    #     just log itself in rather than telling nobody-in-particular to go
    #     run an action by hand -- unattended operation is the whole point.
    #   * the stored session's own BbRouter cookie says it is already dead or
    #     about to be, so a request now would only come back 401/302.
    #
    # A session that exists but carries no expiry (an imported one, say) is
    # deliberately left alone: "expiry unknown" must not mean "renew on every
    # single request". That case stays with the reactive path below.
    cookies = bb_session.load_session(base_url)
    expires_at = bb_session.session_expires_at(base_url)
    expiring = expires_at is not None and expires_at <= time.time() + _REFRESH_MARGIN_SECONDS
    if cookies is None or expiring:
        await _ensure_fresh_session(config, base_url)
        cookies = bb_session.load_session(base_url)

    if not cookies:
        raise SessionExpiredError(
            "not logged in yet -- set a username and password (BB_PASSWORD) "
            "to have this package log itself in, or run the 'login', "
            "'login_sso' or 'import_session' action"
        )
    jar = bb_session.cookies_to_jar(cookies)

    client = await _get_client()
    url = f"{base_url}{path}"
    try:
        response = await client.get(url, params=params, headers=_cookie_header(jar), timeout=_REQUEST_TIMEOUT)
    except httpx.HTTPError as exc:
        raise BlackboardError(f"could not reach {base_url}: {exc}") from exc

    if response.status_code in (301, 302, 303, 307, 308, 401):
        return await _reauth_and_retry(config, base_url, method, url, params)

    if response.status_code == 403:
        if await _session_still_valid(base_url, jar):
            raise PermissionDeniedError(
                "your Blackboard account doesn't have permission to see this "
                "(HTTP 403) -- this isn't a login problem, the session is fine"
            )
        return await _reauth_and_retry(config, base_url, method, url, params)

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # Anything else (404 on a bad course/content id, 5xx, ...) still has
        # to come back as a BlackboardError -- a bare httpx exception isn't
        # caught by any tool function's `except bb_client.BlackboardError`,
        # so it would otherwise escape as a raw traceback instead of the
        # {"success": False, "error": ...} shape every tool here returns.
        raise BlackboardError(
            f"Blackboard returned HTTP {response.status_code} for {path}"
        ) from exc
    return response


def _has_direct_credentials(config: dict[str, Any]) -> bool:
    """Whether there's a username *and* password to auto-login with.

    Auto-refresh used to be gated on ``config["auth_mode"] == "direct"``,
    which meant someone who filled in a username/password but left
    auth_mode on its "sso" default silently got no automatic renewal at
    all. What actually matters is whether login_direct has something to
    fill the login form with, not which label the config happens to carry.
    """
    return bool(config.get("username")) and bool(config.get("password"))


async def _login_and_save(config: dict[str, Any]) -> None:
    """Run the headless login (sync, and may drive Playwright, hence the
    thread) and turn a failure into the error this module's callers raise
    on. Assumes ``_login_lock`` is already held by the caller."""
    result = await asyncio.to_thread(bb_auth.login_direct, config)
    if not result.get("success"):
        raise SessionExpiredError(
            f"the Blackboard session expired and automatic re-login "
            f"failed: {result.get('error')}"
        )


async def _ensure_fresh_session(config: dict[str, Any], base_url: str) -> None:
    """Proactive counterpart to ``_reauth_and_retry``, called from
    ``_request`` before a request is even sent -- either because no session
    exists yet, or because the stored one is about to expire.

    Shares ``_login_lock`` with the reactive path so concurrent tool calls
    hitting the same soon-to-expire session only ever cause one login, and
    re-checks the local expiry after acquiring the lock in case another
    coroutine already renewed it while this one was waiting (a freshly
    logged-in session reads back with an expiry comfortably in the future,
    which is exactly what that check looks for).

    Missing credentials are not an error here -- there is simply nothing to
    proactively refresh with, so this quietly does nothing and leaves the
    stale session in place. ``_request`` then goes ahead and makes the
    request anyway; if the session really is dead, the existing reactive
    302/401/403 handling in ``_reauth_and_retry`` still runs and raises the
    right ``SessionExpiredError`` for the caller.
    """
    async with _state().login_lock:
        expires_at = bb_session.session_expires_at(base_url)
        if expires_at is not None and expires_at > time.time() + _REFRESH_MARGIN_SECONDS:
            return  # someone else already renewed it while we waited for the lock

        if not _has_direct_credentials(config):
            return

        await _login_and_save(config)


async def _reauth_and_retry(
    config: dict[str, Any],
    base_url: str,
    method: str,
    url: str,
    params: dict[str, Any] | None,
) -> httpx.Response:
    """One re-login attempt, serialized so concurrent tool calls hitting an
    expired session at the same time don't each launch their own login.

    This is the safety net for everything the proactive check in ``_request``
    can't see -- a session revoked server-side before its own stated expiry,
    a clock skew, or simply no BbRouter ``expires`` field to read in the
    first place -- so it stays in place unchanged by the proactive path
    existing alongside it."""
    async with _state().login_lock:
        client = await _get_client()

        # Another call may have already fixed this while we were waiting.
        cookies = bb_session.load_session(base_url)
        jar = bb_session.cookies_to_jar(cookies) if cookies else {}
        if jar and await _session_still_valid(base_url, jar):
            try:
                response = await client.request(
                    method, url, params=params, headers=_cookie_header(jar), timeout=_REQUEST_TIMEOUT
                )
            except httpx.HTTPError as exc:
                raise BlackboardError(f"could not reach {base_url}: {exc}") from exc
            if response.status_code < 400:
                return response

        if not _has_direct_credentials(config):
            raise SessionExpiredError(
                "the Blackboard session has expired or was rejected. Set a "
                "username and password (BB_PASSWORD) to let this package "
                "renew it automatically, or run the 'login_sso' or "
                "'import_session' action again"
            )

        await _login_and_save(config)

        cookies = bb_session.load_session(base_url)
        jar = bb_session.cookies_to_jar(cookies) if cookies else {}
        try:
            response = await client.request(
                method, url, params=params, headers=_cookie_header(jar), timeout=_REQUEST_TIMEOUT
            )
        except httpx.HTTPError as exc:
            raise BlackboardError(f"could not reach {base_url}: {exc}") from exc
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BlackboardError(
                f"Blackboard returned HTTP {response.status_code} for {url} "
                "(after re-login)"
            ) from exc
        return response


async def _session_still_valid(base_url: str, jar: dict[str, str]) -> bool:
    client = await _get_client()
    try:
        probe = await client.get(
            f"{base_url}{_USERS_ME_PATH}", headers=_cookie_header(jar), timeout=10.0
        )
    except httpx.HTTPError:
        return False
    return probe.status_code == 200


async def _get_client() -> httpx.AsyncClient:
    state = _state()
    if state.client is None:
        async with state.client_lock:
            if state.client is None:
                state.client = httpx.AsyncClient(follow_redirects=False)
    return state.client


def _cookie_header(jar: dict[str, str]) -> dict[str, str]:
    """A ``Cookie`` header built straight from the flattened jar, instead of
    httpx's own ``cookies=`` kwarg -- httpx deprecated setting cookies per
    request (cookie persistence semantics on a shared client are ambiguous),
    and we already flatten cookies ourselves in bb_session.cookies_to_jar,
    with no per-domain matching to preserve anyway."""
    if not jar:
        return {}
    return {"Cookie": "; ".join(f"{name}={value}" for name, value in jar.items())}


# ---------------------------------------------------------------------------
# Cache -- small, TTL-bound, keyed on the exact request
# ---------------------------------------------------------------------------


def _cache_key(path: str, params: dict[str, Any] | None) -> tuple[str, str]:
    items = tuple(sorted((params or {}).items()))
    return (path, str(items))


def _cache_get(key: tuple[str, str]) -> Any | None:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if time.monotonic() > expires_at:
        _CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: tuple[str, str], value: Any, ttl: int) -> None:
    if ttl <= 0:
        return
    if len(_CACHE) >= _CACHE_MAX_ENTRIES:
        _CACHE.pop(next(iter(_CACHE)), None)
    _CACHE[key] = (time.monotonic() + ttl, value)
