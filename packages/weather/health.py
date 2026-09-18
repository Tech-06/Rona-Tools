from toolbox.custom.weather.get_weather import get_weather


def check(config: dict) -> dict:
    if not config.get("api_key"):
        return {"ok": False, "detail": "OPENWEATHER_API_KEY is not set."}
    result = get_weather("Istanbul")
    return {"ok": bool(result.get("success")), "detail": result.get("error", "ok")}
