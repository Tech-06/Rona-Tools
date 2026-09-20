"""Authorize a Google account from the backend's own terminal.

Kept for anyone with the old command in their notes, but it is now the least
capable way to do this: it can only use a browser on the machine the backend
runs on, which is no use on a server. Prefer

    rona tools run google_auth add_account

which gives you a link to open on any device, or the dashboard's
Settings -> Connections -> Tool Packages panel, which does the same thing.
"""

import sys

from toolbox.custom.google_auth import google_auth


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m toolbox.custom.google_auth.add_account <account_name>")
        print("Example: python -m toolbox.custom.google_auth.add_account personal")
        print()
        print("Prefer: rona tools run google_auth add_account")
        sys.exit(1)

    account_name = sys.argv[1]
    print(f"Starting authorization for '{account_name}'...")
    print(
        "This opens a browser on the machine the backend runs on. If that is not "
        "where you are, cancel and use `rona tools run google_auth add_account`."
    )

    try:
        google_auth.authorize_with_local_browser(account_name)
    except Exception as exc:  # noqa: BLE001
        print(f"An error occurred: {exc}")
        sys.exit(1)

    print(f"Success! Account '{account_name}' registered and token file created.")
    print("Add it to a Google tool's allowed accounts to start using it.")


if __name__ == "__main__":
    main()
