"""Hevy API client (Phase A of the implementation plan).

NOTE: the exact shape of /v1/workouts/events (event_type field name, whether
deleted events carry the full workout body or just an id) is inferred from
public documentation referenced during planning, not verified against a live
response. Treat _parse_event as the one place to fix up field names once
this runs against the real API with a real HEVY_API_KEY.
"""

from __future__ import annotations

import logging
from datetime import datetime

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.config import settings

logger = logging.getLogger("hevy2garmin_lite.hevy_client")

BASE_URL = "https://api.hevyapp.com"
EVENTS_PAGE_SIZE = 10  # Hevy's documented max for /v1/workouts/events


class HevyAPIError(Exception):
    pass


def _headers() -> dict:
    return {"api-key": settings.HEVY_API_KEY, "Accept": "application/json"}


@retry(
    retry=retry_if_exception_type(requests.RequestException),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _get(path: str, params: dict | None = None) -> dict | None:
    resp = requests.get(f"{BASE_URL}{path}", headers=_headers(), params=params, timeout=15)
    if resp.status_code == 401:
        raise HevyAPIError("Hevy API key rejected (401) — check HEVY_API_KEY")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


@retry(
    retry=retry_if_exception_type(requests.RequestException),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _post(path: str, json_body: dict) -> dict:
    resp = requests.post(f"{BASE_URL}{path}", headers=_headers(), json=json_body, timeout=15)
    if resp.status_code == 401:
        raise HevyAPIError("Hevy API key rejected (401) — check HEVY_API_KEY")
    resp.raise_for_status()
    return resp.json()


def _delete(path: str) -> None:
    resp = requests.delete(f"{BASE_URL}{path}", headers=_headers(), timeout=15)
    if resp.status_code not in (200, 204, 404):
        resp.raise_for_status()


def parse_hevy_timestamp(raw: str) -> datetime:
    """Hevy timestamps are ISO 8601 with a Z suffix (UTC). See plan §3 Phase B."""
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def fetch_workout_events(since: str | None) -> list[dict]:
    """Fetch all events since the given ISO timestamp, paginating through
    Hevy's 10-per-page change feed. Returns raw event dicts with at least
    `type` ("updated" | "deleted") and `workout` (for updated events)."""
    events: list[dict] = []
    page = 1
    while True:
        params = {"page": page, "pageSize": EVENTS_PAGE_SIZE}
        if since:
            params["since"] = since
        data = _get("/v1/workouts/events", params)
        page_events = data.get("events", [])
        events.extend(page_events)
        if len(page_events) < EVENTS_PAGE_SIZE:
            break
        page += 1
    return events


def _parse_event(raw: dict) -> dict:
    event_type = raw.get("type") or raw.get("event_type", "updated")
    workout = raw.get("workout") or raw
    return {
        "type": "deleted" if event_type == "deleted" else "updated",
        "workout_id": workout.get("id") or raw.get("id"),
        "workout": workout if event_type != "deleted" else None,
    }


def poll_events(since: str | None) -> list[dict]:
    raw_events = fetch_workout_events(since)
    return [_parse_event(e) for e in raw_events]


def fetch_workout(workout_id: str) -> dict | None:
    """GET a single workout by id. Used as a fallback for webhook payloads
    and for the retry-with-backoff path — the webhook fires on
    workout.created, but the payload shape isn't verified against a live
    response, so we always re-fetch by id rather than trusting an embedded
    body we haven't confirmed exists."""
    data = _get(f"/v1/workouts/{workout_id}")
    if data is None:
        return None
    return data.get("workout", data)


# --- Webhook subscription management -----------------------------------
# NOTE: endpoint shapes (POST/GET/DELETE /v1/webhook-subscription, the
# workout.created-only event, and the Authorization-header auth pattern) are
# corroborated across several independent third-party integrations, not
# Hevy's own official docs — verify against the real API on first use.

def register_webhook(url: str, auth_token: str) -> dict:
    return _post("/v1/webhook-subscription", {"url": url, "auth_token": auth_token})


def get_webhook_subscription() -> dict | None:
    return _get("/v1/webhook-subscription")


def delete_webhook_subscription() -> None:
    _delete("/v1/webhook-subscription")


def parse_webhook_payload(raw: dict) -> dict | None:
    """Parses the POST body Hevy sends to our webhook endpoint. Only
    workout.created is currently supported by Hevy — anything else is
    ignored (returns None) rather than guessed at."""
    if raw.get("event") != "workout.created":
        return None
    workout = raw.get("workout")
    workout_id = (workout or {}).get("id") or raw.get("workout_id") or raw.get("id")
    return {"workout_id": workout_id, "workout": workout}


def ensure_webhook_registered(url: str, auth_token: str) -> None:
    """Idempotent: only registers if not already pointing at our URL. Called
    at app startup so redeploys don't spam Hevy with redundant registrations."""
    existing = get_webhook_subscription()
    if existing and existing.get("url") == url:
        logger.info("Hevy webhook already registered at %s", url)
        return
    register_webhook(url, auth_token)
    logger.info("Registered Hevy webhook at %s", url)
