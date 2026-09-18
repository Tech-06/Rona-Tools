from toolbox.custom.deepl_translate.deepl_translate import translate_text


def check(config: dict) -> dict:
    if not config.get("api_key"):
        return {"ok": False, "detail": "DEEPL_API_KEY is not set."}
    result = translate_text("merhaba", "EN-US")
    return {"ok": bool(result.get("success")), "detail": result.get("error", "ok")}
