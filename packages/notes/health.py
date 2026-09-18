from toolbox.custom.notes.notes_tool import get_notes


def check(config: dict) -> dict:
    result = get_notes(limit=1)
    return {"ok": bool(result.get("success")), "detail": result.get("error", "ok")}
