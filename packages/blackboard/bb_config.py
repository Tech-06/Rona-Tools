"""Configuration and secrets for the Blackboard Learn package.

manifest.json's "config" list is what the host resolves for health_check()
and the package's actions -- it hands them a ready-made dict, see
``toolbox.packages.load_config_values``. Tool functions (blackboard_tool.py)
are called by the model with no config of their own, so they read
config.json / .env themselves here, exactly like weather/web_search/
deepl_translate do for their API keys.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_PACKAGE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = _PACKAGE_DIR / "config.json"
# backend/toolbox/custom/blackboard/bb_config.py -> parents[3] == backend/
_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"

DEFAULT_AUTH_MODE = "direct"
VALID_AUTH_MODES = ("direct", "sso", "manual")
DEFAULT_CACHE_TTL = 300


def normalize_base_url(value: str | None) -> str:
    """Strip whitespace and a trailing slash so it's safe to f-string paths
    onto (``f"{base_url}/learn/api/..."``) without a doubled or missing '/'."""
    return (value or "").strip().rstrip("/")


def _read_raw() -> dict[str, Any]:
    if not CONFIG_FILE.is_file():
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config() -> dict[str, Any]:
    """Resolve every setting this package needs.

    Used directly by tool functions. health.py and actions.py instead reuse
    the config dict the host already handed them -- calling this again there
    would just re-read the same files a second time.
    """
    load_dotenv(_ENV_PATH)
    raw = _read_raw()

    auth_mode = str(raw.get("auth_mode") or DEFAULT_AUTH_MODE).strip().lower()
    if auth_mode not in VALID_AUTH_MODES:
        auth_mode = DEFAULT_AUTH_MODE

    try:
        cache_ttl = int(raw.get("cache_ttl") or DEFAULT_CACHE_TTL)
    except (TypeError, ValueError):
        cache_ttl = DEFAULT_CACHE_TTL

    timezone = str(raw.get("timezone") or "").strip()
    if not timezone:
        # Not app.config -- that would reach across the package boundary.
        # TRIGGER_TIMEZONE is already loaded into the process environment
        # the same way BB_PASSWORD is, so reading it here is no different
        # from reading any other env var; it just means "match whatever
        # Rona itself is set to" without importing anything host-side.
        timezone = os.environ.get("TRIGGER_TIMEZONE", "UTC").strip() or "UTC"

    return {
        "base_url": normalize_base_url(raw.get("base_url")),
        "auth_mode": auth_mode,
        "username": str(raw.get("username") or "").strip(),
        "password": os.environ.get("BB_PASSWORD", ""),
        "timezone": timezone,
        "cache_ttl": max(0, cache_ttl),
        "login_username_selector": str(raw.get("login_username_selector") or "").strip(),
        "login_password_selector": str(raw.get("login_password_selector") or "").strip(),
        "login_submit_selector": str(raw.get("login_submit_selector") or "").strip(),
    }


def is_configured(config: dict[str, Any] | None = None) -> bool:
    config = config if config is not None else load_config()
    return bool(config.get("base_url"))


def not_configured_error() -> dict[str, Any]:
    return {
        "success": False,
        "error": (
            "Blackboard is not configured yet. Set 'base_url' in the "
            "blackboard package's configuration first (Settings -> "
            "Connections -> Tool Packages, or `rona tools config blackboard "
            "--set base_url=https://your-school.blackboard.com`)."
        ),
    }
