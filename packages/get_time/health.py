from toolbox.custom.get_time.get_time import get_time


def check(config: dict) -> dict:
    result = get_time("UTC")
    return {"ok": bool(result.get("success")), "detail": result.get("error", "ok")}
