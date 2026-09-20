import json
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
_CREDENTIALS_PATH = _PACKAGE_DIR / "credentials.json"


def check(config: dict) -> dict:
    """Healthy as soon as a usable OAuth client is in place.

    Having no authorized accounts yet is a perfectly normal state right after
    install -- accounts are added afterwards, from the dashboard or from
    `rona tools run google_auth add_account` -- so it is reported, not failed.
    """
    path = Path(config.get("credentials_file") or _CREDENTIALS_PATH)
    if not path.is_file():
        return {"ok": False, "detail": f"credentials.json not found at {path}"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "detail": f"credentials.json is not valid JSON: {exc}"}
    if "installed" not in data and "web" not in data:
        return {
            "ok": False,
            "detail": "credentials.json does not look like an OAuth client file "
            "(missing 'installed'/'web' key)",
        }

    accounts = sorted(
        name
        for name in (p.stem.removeprefix("token_") for p in _PACKAGE_DIR.glob("token_*.json"))
        if name
    )
    if not accounts:
        return {
            "ok": True,
            "detail": "OAuth client ready; no accounts authorized yet",
        }
    return {
        "ok": True,
        "detail": f"OAuth client ready; {len(accounts)} account(s): {', '.join(accounts)}",
    }
