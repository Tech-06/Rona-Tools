from toolbox import packages


def check(config: dict) -> dict:
    accounts = config.get("accounts") or []
    if not accounts:
        return {"ok": False, "detail": "no accounts configured"}

    google_auth_dir = packages.package_dir("google_auth")
    missing = [a for a in accounts if not (google_auth_dir / f"token_{a}.json").is_file()]
    if missing:
        return {
            "ok": False,
            "detail": (
                f"no token for account(s): {', '.join(missing)}. Run "
                "'python -m toolbox.custom.google_auth.add_account <account>' "
                "for each and retry."
            ),
        }

    from toolbox.custom.google_mail.mail_tool import get_recent_emails

    result = get_recent_emails(accounts[0], max_results=1)
    return {"ok": bool(result.get("success")), "detail": result.get("error", "ok")}
