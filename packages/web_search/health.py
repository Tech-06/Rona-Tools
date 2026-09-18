from toolbox.custom.web_search.web_search import web_search


def check(config: dict) -> dict:
    if not config.get("api_key"):
        return {"ok": False, "detail": "TAVILY_API_KEY is not set."}
    result = web_search("test", 1)
    return {"ok": bool(result.get("success")), "detail": result.get("error", "ok")}
