"""Self-healing Garmin token consumption (revised per user decision — see
plan §2). Hevy2Garmin Lite now holds its own copy of the account credentials and
can perform a full credential re-login into the SHARED token store when the
cached token is rejected, exactly mirroring garmin-scale-sync's own pattern.
This is not a routine path: python-garminconnect only falls back to a full
login when the cached token load/validate actually fails, so this only fires
when the shared store is genuinely dead — not on every sync.

MFA handling mirrors garmin-scale-sync's dashboard flow: the login thread
blocks on a threading.Event waiting for a code submitted via POST
/v1/auth/mfa, rather than expecting interactive terminal input (there's no
terminal in a running container).
"""

from __future__ import annotations

import logging
import threading

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

from src.config import settings

logger = logging.getLogger("hevy2garmin_lite.garmin_client")

MFA_TIMEOUT_SECONDS = 60.0

mfa_state = {
    "waiting": False,
    "code": None,
    "event": threading.Event(),
}

_client_instance: Garmin | None = None
_client_lock = threading.Lock()


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


def get_garmin_client() -> Garmin:
    """Returns a cached, logged-in Garmin client. Loads the shared token
    store on first use; only performs a full credential login (mirroring
    garmin-scale-sync) if those cached tokens are rejected by the API.
    Raises TokenLoadError on any unrecoverable failure (bad credentials,
    rate limit, MFA timeout) — callers must skip the cycle, not retry
    forever within the same request."""
    global _client_instance
    if _client_instance is not None:
        return _client_instance

    with _client_lock:
        if _client_instance is not None:
            return _client_instance

        client = Garmin(
            email=settings.GARMIN_EMAIL or None,
            password=settings.GARMIN_PASSWORD or None,
            prompt_mfa=_prompt_mfa_callback,
        )
        try:
            client.login(settings.GARMIN_TOKEN_SOURCE_DIR)
        except GarminConnectTooManyRequestsError as e:
            raise TokenLoadError(f"Garmin rate-limited this login attempt: {e}") from e
        except GarminConnectAuthenticationError as e:
            raise TokenLoadError(f"Garmin authentication failed: {e}") from e
        except Exception as e:  # noqa: BLE001
            raise TokenLoadError(f"Unexpected error loading/refreshing Garmin session: {e}") from e

        _client_instance = client
        return _client_instance


def reset_garmin_client() -> None:
    """Forces the next get_garmin_client() call to re-login. Call this after
    a request fails with an auth error mid-sync, so the next cycle retries
    cleanly instead of reusing a known-bad cached client."""
    global _client_instance
    with _client_lock:
        _client_instance = None


def auth_status() -> dict:
    if mfa_state["waiting"]:
        return {"status": "mfa_required", "message": "Multi-Factor Authentication code required."}
    if _client_instance is not None:
        return {"status": "authenticated", "message": "Garmin session active."}
    return {"status": "unauthenticated", "message": "Not yet authenticated this run."}
