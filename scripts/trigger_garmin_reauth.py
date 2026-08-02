"""One-off bootstrap: perform a REAL credential login to refresh the shared
Garmin token store when it's dead (see spike findings — the previous token's
refresh token was itself rejected, not just the short-lived access token).

This is DELIBERATELY separate from src/garmin_client.py, which must never
hold credentials (plan §2). This script exists to do exactly what
garmin-scale-sync's own dashboard "re-login" flow does — a full credential
login — as a one-time manual bootstrap, using garmin-scale-sync's own
.env.gss credentials.

Per python-garminconnect's login() self-healing behavior (confirmed by
reading __init__.py): calling client.login(tokenstore_path) with credentials
set on the constructor will detect that the cached tokens are rejected by
the API, discard them, perform a full fresh credential login (prompting MFA
via the callback below if Garmin requires it), and persist the new tokens
back to that same tokenstore_path — updating the SHARED file that both
garmin-scale-sync and Hevy2Garmin Lite read from.

Credentials are read from the environment (docker-compose's `reauth` service
loads them from .env via env_file) and never printed or logged.

Run: docker compose run --rm reauth
"""

from __future__ import annotations

import os
import sys

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

TOKEN_DIR = "/app/garmin_tokens_source"  # the REAL shared dir, mounted read-write for this script only


def mfa_prompt() -> str:
    print("\nGarmin is requesting a Multi-Factor Authentication code.")
    print("Check your email/SMS/authenticator app for the code Garmin sent.")
    print("(If Garmin instead emailed you a VERIFICATION LINK rather than a code,")
    print(" there's nothing this script can do with that automatically —")
    print(" open the link yourself, approve the sign-in, then re-run this script.)")
    return input("Enter MFA code: ").strip()


def main() -> int:
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")

    if not email or not password:
        print("GARMIN_EMAIL / GARMIN_PASSWORD not found in the environment (.env)")
        return 1

    print(f"Loaded credentials for {email[:2]}***@*** (not printing the rest).")
    print(f"Attempting login, will persist fresh tokens to {TOKEN_DIR} ...\n")

    client = Garmin(email=email, password=password, prompt_mfa=mfa_prompt)

    try:
        client.login(TOKEN_DIR)
    except GarminConnectTooManyRequestsError as e:
        print(f"\nRate limited by Garmin: {e}")
        print("This is Garmin-side and unrelated to your password — wait and retry later.")
        return 1
    except GarminConnectAuthenticationError as e:
        print(f"\nAuthentication failed: {e}")
        print("Check GARMIN_EMAIL / GARMIN_PASSWORD in .env.gss, or check https://sso.garmin.com")
        print("for an account lock / new-device verification email from Garmin.")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\nUnexpected error during login: {e}")
        return 1

    print("\nLogin succeeded. Verifying by fetching profile...")
    name = client.get_full_name()
    print(f"Authenticated as: {name}")
    print(f"Fresh tokens written to {TOKEN_DIR} — garmin-scale-sync and Hevy2Garmin Lite can both read them now.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
