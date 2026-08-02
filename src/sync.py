"""Sync orchestrator — wires Phases A through E together.

sync_one_workout() is the shared core used by three callers:
  - run_sync_cycle(): the polling reconciliation path (all events since cursor)
  - main.py's webhook handler: immediate attempt on workout.created
  - main.py's retry scheduler: re-attempts a no_watch_match workout at
    +5/+10/+15 min, handling the watch->Garmin Connect sync lag
"""

from __future__ import annotations

import hashlib
import json
import logging

from garminconnect import GarminConnectAuthenticationError

from src import hevy_client
from src.config import settings
from src.db import Database
from src.garmin_client import TokenLoadError, get_garmin_client, reset_garmin_client
from src.mapping import ExerciseMapper
from src.matcher import find_best_match, parse_garmin_gmt
from src.push import (
    SetPushCircuitBreaker,
    build_exercise_sets_payload,
    get_existing_exercise_sets,
    push_exercise_sets,
)
from src.timeline import TimelineConfig

logger = logging.getLogger("hevy2garmin_lite.sync")

GARMIN_ACTIVITY_FETCH_LIMIT = 20  # recent activities scanned per cycle


def _content_hash(workout: dict) -> str:
    return hashlib.sha256(json.dumps(workout, sort_keys=True, default=str).encode()).hexdigest()


def _timeline_config_from_settings() -> TimelineConfig:
    return TimelineConfig(
        working_set_seconds=settings.WORKING_SET_SECONDS,
        warmup_set_seconds=settings.WARMUP_SET_SECONDS,
        rest_between_sets_seconds=settings.REST_BETWEEN_SETS_SECONDS,
        rest_between_exercises_seconds=settings.REST_BETWEEN_EXERCISES_SECONDS,
    )


class SyncRunResult:
    def __init__(self):
        self.synced = 0
        self.no_match = 0
        self.skipped_idempotent = 0
        self.failed = 0
        self.source_deleted = 0
        self.errors: list[str] = []


def sync_one_workout(
    db: Database,
    mapper: ExerciseMapper,
    client,
    workout_id: str,
    workout: dict,
    garmin_activities: list[dict],
    already_claimed: set[int],
    breaker: SetPushCircuitBreaker,
    dry_run: bool = False,
) -> str:
    """Attempts to sync one already-fetched Hevy workout against
    already-fetched Garmin activities. Returns the resulting status:
    "synced" | "no_watch_match" | "skipped_idempotent" | "failed"."""
    content_hash = _content_hash(workout)

    existing = db.get_sync_record(workout_id)
    if existing and existing["content_hash"] == content_hash and existing["sync_status"] == "synced":
        return "skipped_idempotent"

    if breaker.tripped():
        return "failed"

    match = find_best_match(workout, garmin_activities, already_claimed)
    if match is None:
        db.record_sync(workout_id, None, "no_watch_match", content_hash)
        return "no_watch_match"

    activity_id = match["activityId"]
    activity_start = parse_garmin_gmt(match["startTimeGMT"])
    activity_duration_s = match["duration"]

    try:
        if not dry_run:
            get_existing_exercise_sets(client, activity_id)  # backup, best-effort

        payload = build_exercise_sets_payload(
            workout, activity_id, activity_start, activity_duration_s,
            mapper, _timeline_config_from_settings(),
        )

        if dry_run:
            logger.info("[dry-run] would push %d sets to activity %s", len(payload["exerciseSets"]), activity_id)
        else:
            push_exercise_sets(client, activity_id, payload)

        db.record_sync(workout_id, activity_id, "synced", content_hash)
        already_claimed.add(activity_id)
        breaker.record_success()
        return "synced"
    except GarminConnectAuthenticationError as e:
        # The cached client's session died mid-cycle (e.g. revoked after
        # login succeeded) — clear it so the *next* cycle re-logs in fresh
        # instead of reusing a permanently broken client.
        reset_garmin_client()
        logger.error("Garmin session died mid-push for workout %s: %s", workout_id, e)
        db.record_sync(workout_id, activity_id, "failed", content_hash)
        breaker.record_failure()
        return "failed"
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to push workout %s to activity %s: %s", workout_id, activity_id, e)
        db.record_sync(workout_id, activity_id, "failed", content_hash)
        breaker.record_failure()
        return "failed"


def sync_workout_by_id(db: Database, mapper: ExerciseMapper, workout_id: str, dry_run: bool = False) -> str:
    """Single-workout entry point for the webhook handler and its retries.
    Always re-fetches the workout fresh (see hevy_client.fetch_workout) and
    the current Garmin activity list, since this may run well after any
    recent poll. Raises TokenLoadError/HevyAPIError to the caller on
    infrastructure failure (auth, network) — those aren't "no match", they're
    "couldn't check", and callers should treat them differently."""
    client = get_garmin_client()

    workout = hevy_client.fetch_workout(workout_id)
    if workout is None:
        logger.warning("Hevy workout %s not found (deleted before we could sync it?)", workout_id)
        return "failed"

    garmin_activities = client.get_activities(0, GARMIN_ACTIVITY_FETCH_LIMIT)
    already_claimed = {
        row["garmin_activity_id"]
        for row in db.recent_sync_history(limit=500)
        if row["garmin_activity_id"] is not None
    }
    breaker = SetPushCircuitBreaker(max_consecutive_failures=1)  # single workout — no need for a multi-failure budget

    return sync_one_workout(db, mapper, client, workout_id, workout, garmin_activities, already_claimed, breaker, dry_run)


def run_sync_cycle(db: Database, mapper: ExerciseMapper, dry_run: bool = False) -> SyncRunResult:
    """Polling reconciliation path: processes every Hevy event since the
    cursor. Handles updated/deleted events, which the webhook does not."""
    result = SyncRunResult()

    try:
        client = get_garmin_client()
    except TokenLoadError as e:
        logger.error("Sync cycle aborted — Garmin token load failed: %s", e)
        result.errors.append(str(e))
        return result

    since = db.get_last_poll_timestamp()
    try:
        events = hevy_client.poll_events(since)
    except Exception as e:  # noqa: BLE001
        logger.error("Sync cycle aborted — Hevy poll failed: %s", e)
        result.errors.append(str(e))
        return result

    if not events:
        logger.info("No new Hevy events since %s", since)
        return result

    try:
        garmin_activities = client.get_activities(0, GARMIN_ACTIVITY_FETCH_LIMIT)
    except GarminConnectAuthenticationError as e:
        # get_activities() goes through the high-level, decorated connectapi(),
        # so a dead session here IS correctly typed already — but we still
        # need to reset the cache so the *next* cycle re-logs in fresh.
        reset_garmin_client()
        logger.error("Sync cycle aborted — Garmin session died fetching activities: %s", e)
        result.errors.append(str(e))
        return result
    except Exception as e:  # noqa: BLE001
        logger.error("Sync cycle aborted — could not fetch Garmin activities: %s", e)
        result.errors.append(str(e))
        return result

    already_claimed = {
        row["garmin_activity_id"]
        for row in db.recent_sync_history(limit=500)
        if row["garmin_activity_id"] is not None
    }
    breaker = SetPushCircuitBreaker(max_consecutive_failures=3)

    for event in events:
        workout_id = event["workout_id"]
        if not workout_id:
            continue

        if event["type"] == "deleted":
            db.mark_source_deleted(workout_id)
            result.source_deleted += 1
            logger.info("Hevy workout %s deleted upstream — not touching Garmin", workout_id)
            continue

        if breaker.tripped():
            logger.error("Circuit breaker tripped — skipping remaining pushes this cycle")
            result.failed += 1
            continue

        status = sync_one_workout(
            db, mapper, client, workout_id, event["workout"],
            garmin_activities, already_claimed, breaker, dry_run,
        )
        if status == "synced":
            result.synced += 1
        elif status == "no_watch_match":
            result.no_match += 1
        elif status == "skipped_idempotent":
            result.skipped_idempotent += 1
        else:
            result.failed += 1

    import datetime
    db.set_last_poll_timestamp(datetime.datetime.now(datetime.UTC).isoformat())

    return result
