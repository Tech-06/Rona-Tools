from toolbox import packages


def check(config: dict) -> dict:
    """Healthy once every allowed account has a token.

    An empty allowed-accounts list is the normal state right after install --
    accounts are authorized separately and added here afterwards -- so it is
    reported rather than failed.
    """
    accounts = config.get("accounts") or []
    if not accounts:
        return {"ok": True, "detail": "no accounts allowed yet"}

    google_auth_dir = packages.package_dir("google_auth")
    missing = [a for a in accounts if not (google_auth_dir / f"token_{a}.json").is_file()]
    if missing:
        return {
            "ok": False,
            "detail": (
                f"no authorization for account(s): {', '.join(missing)}. Add each "
                "one with `rona tools run google_auth add_account`, or from the "
                "dashboard's Settings > Connections > Tool Packages panel."
            ),
        }

    from toolbox.custom.google_contacts.contacts_tool import get_contacts

    result = get_contacts(accounts[0], page_size=1)
    return {"ok": bool(result.get("success")), "detail": result.get("error", "ok")}
