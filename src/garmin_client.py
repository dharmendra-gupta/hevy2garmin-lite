"""Garmin session access, backed by the shared token store.

Garmin rotates the account's refresh token on every refresh, so exactly one
refresh token is valid at a time no matter how many services use the account.
This module previously read the shared token store once and cached the client
for the life of the process, which meant a peer service (garmin-scale-sync)
refreshing would leave this process holding a superseded token. The 401 that
followed arrived typed as a connection error, so `push.py` carried
`_reraise_401_as_auth_error` — a regex on "API Error 401" — to convert it into
something that would trigger a client reset.

`GarminSession` removes the need for both halves of that: it re-reads the
shared store before every use (so a peer's rotation is adopted rather than
fought), republishes its own rotations, and classifies 401 by HTTP status
rather than by matching the text of an error message.

MFA handling is unchanged: the login thread blocks on a threading.Event
waiting for a code submitted via POST /v1/auth/mfa, rather than expecting
interactive terminal input (there's no terminal in a running container).
"""

from __future__ import annotations

import logging
import os
import threading

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

from src.config import settings
from src.garmin_session import (
    FileTokenStore,
    GarminSession,
    PostgresTokenStore,
    SqliteTokenStore,
)

logger = logging.getLogger("hevy2garmin_lite.garmin_client")

MFA_TIMEOUT_SECONDS = 60.0

mfa_state = {
    "waiting": False,
    "code": None,
    "event": threading.Event(),
}


class TokenLoadError(Exception):
    pass


def _prompt_mfa_callback() -> str:
    mfa_state["waiting"] = True
    mfa_state["code"] = None
    mfa_state["event"].clear()

    logger.warning("Garmin MFA requested — waiting for a code via the dashboard (POST /v1/auth/mfa)...")
    success = mfa_state["event"].wait(timeout=MFA_TIMEOUT_SECONDS)
    mfa_state["waiting"] = False

    if success and mfa_state["code"]:
        logger.info("MFA code received from dashboard, resuming login.")
        return mfa_state["code"]
    raise GarminConnectAuthenticationError("MFA input timed out or was not submitted in time.")


def submit_mfa_code(code: str) -> None:
    if not mfa_state["waiting"]:
        raise ValueError("No MFA prompt is currently waiting.")
    mfa_state["code"] = code.strip()
    mfa_state["event"].set()


def _build_token_store():
    """Select the shared-token backend. Must match garmin-scale-sync's
    TOKEN_STORE — pointing the two services at different stores splits them
    onto separate sessions, and each will rotate the other's token away."""
    kind = (settings.TOKEN_STORE or "file").strip().lower()
    if kind == "file":
        return FileTokenStore(settings.GARMIN_TOKEN_SOURCE_DIR)
    if kind == "sqlite":
        return SqliteTokenStore(os.path.join(settings.DATA_DIR, "garmin_tokens.db"))
    if kind == "postgres":
        if not settings.TOKEN_DB_URL:
            raise ValueError("TOKEN_STORE=postgres requires TOKEN_DB_URL to be set.")
        return PostgresTokenStore(settings.TOKEN_DB_URL)
    raise ValueError(
        f"Unknown TOKEN_STORE '{settings.TOKEN_STORE}'. Use file, sqlite, or postgres."
    )


# The shared token store, holding the account's one valid refresh token. Every
# access goes through the store's lock and an atomic write, rather than letting
# garminconnect dump straight onto the file behind other services' backs.
session = GarminSession(
    store=_build_token_store(),
    scratch_dir=os.path.join(settings.DATA_DIR, ".session_scratch"),
    email=settings.GARMIN_EMAIL or None,
    password=settings.GARMIN_PASSWORD or None,
    prompt_mfa=_prompt_mfa_callback,
)


def get_garmin_client() -> Garmin:
    """Returns a logged-in Garmin client for a sync cycle.

    Re-reads the shared token store first, so a token rotated by a peer
    service is adopted rather than fought. Raises TokenLoadError on any
    unrecoverable failure (bad credentials, rate limit, MFA timeout) —
    callers must skip the cycle, not retry forever within the same request.

    Callers should call publish_garmin_tokens() when the cycle finishes so a
    rotation performed during it reaches peers.
    """
    try:
        return session.acquire()
    except GarminConnectTooManyRequestsError as e:
        raise TokenLoadError(f"Garmin rate-limited this login attempt: {e}") from e
    except GarminConnectAuthenticationError as e:
        raise TokenLoadError(f"Garmin authentication failed: {e}") from e
    except Exception as e:
        raise TokenLoadError(f"Unexpected error loading/refreshing Garmin session: {e}") from e


def publish_garmin_tokens() -> None:
    """Write back a token rotation that happened during the cycle, so peer
    services see it instead of continuing on the token we just invalidated."""
    try:
        session.publish()
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not publish rotated Garmin token to the shared store: %s", e)


def reset_garmin_client() -> None:
    """Drops the cached session so the next get_garmin_client() rebuilds from
    the shared store. Call this after a request fails with an auth error
    mid-sync, so the next cycle starts from whatever token is current rather
    than reusing a known-bad client."""
    session.invalidate()


def auth_status() -> dict:
    if mfa_state["waiting"]:
        return {"status": "mfa_required", "message": "Multi-Factor Authentication code required."}
    if session.is_authenticated:
        return {"status": "authenticated", "message": "Garmin session active."}
    return {"status": "unauthenticated", "message": "Not yet authenticated this run."}
