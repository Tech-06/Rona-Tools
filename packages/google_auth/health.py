import json
from pathlib import Path

_CREDENTIALS_PATH = Path(__file__).resolve().parent / "credentials.json"


def check(config: dict) -> dict:
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
    return {"ok": True, "detail": "credentials.json present and valid"}
