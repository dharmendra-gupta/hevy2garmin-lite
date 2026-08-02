"""Push Hevy sets into an existing watch activity (Phase C of the plan).

Payload shape confirmed directly from hevy2garmin's build_exercise_sets_payload()
(src/hevy2garmin/merge.py) against the live, undocumented
PUT /activity-service/activity/{id}/exerciseSets endpoint.

REVISED after Phase G live testing (2026-08-01): we originally never sent a
`name`, believing Garmin ignores pushed identities on watch-recorded
activities regardless of validity. That belief was wrong — it only looked
true because the first test sent `probability: null`. With a real
`probability` value (see mapping.CONFIDENT_PROBABILITY) AND a correct
fit_tool-resolved subcategory name, the specific exercise renders correctly
on both web and mobile, on a genuinely watch-recorded activity. So we now
send real names, which means we can hit the "Invalid Sub-Category"
rejection class (#199/#222 in hevy2garmin) — the atomic PUT gives no
per-exercise error, so one bad name 400s the whole batch. We handle this
with a single strip-all-names-and-retry fallback (simpler than
hevy2garmin's bisect — acceptable at this project's scale) rather than the
"we'll never hit this" assumption the original version made.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from garminconnect import Garmin, GarminConnectAuthenticationError, GarminConnectConnectionError
from tenacity import retry, stop_after_attempt, wait_exponential

from src.mapping import ExerciseMapper
from src.timeline import SetEntry, TimelineConfig, build_set_timeline

logger = logging.getLogger("hevy2garmin_lite.push")

EXERCISE_SETS_PATH = "/activity-service/activity/{activity_id}/exerciseSets"


class PushCircuitBreakerTripped(Exception):
    pass


class SetPushCircuitBreaker:
    """Disables pushing for the rest of a sync run after N consecutive
    failures of any kind (network, auth, unexpected 4xx). See plan §3 Phase C."""

    def __init__(self, max_consecutive_failures: int = 3):
        self._max = max_consecutive_failures
        self._consecutive_failures = 0

    def record_success(self) -> None:
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        self._consecutive_failures += 1

    def tripped(self) -> bool:
        return self._consecutive_failures >= self._max


def _format_garmin_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.0")


def build_exercise_sets_payload(
    hevy_workout: dict,
    activity_id: int,
    activity_start: datetime,
    activity_duration_s: float,
    mapper: ExerciseMapper,
    timeline_config: TimelineConfig | None = None,
) -> dict:
    """Anchored strictly to the watch activity's own start/duration, per
    plan §3 Phase E — Hevy's start_time is ordering information only."""
    exercises = hevy_workout.get("exercises", [])
    entries: list[SetEntry] = build_set_timeline(exercises, activity_duration_s, timeline_config)

    exercise_sets: list[dict] = []
    for msg_idx, entry in enumerate(entries):
        set_start = activity_start + timedelta(seconds=entry.start_offset_s)

        if entry.set_type == "REST":
            exercise_sets.append({
                "exercises": [],
                "duration": round(entry.duration_s, 3),
                "setType": "REST",
                "startTime": _format_garmin_time(set_start),
                "wktStepIndex": entry.exercise_idx,
                "messageIndex": msg_idx,
            })
            continue

        ex = exercises[entry.exercise_idx]
        identity = mapper.resolve(ex.get("exercise_template_id"), ex.get("title") or ex.get("name", "Unknown"))

        reps = entry.set_data.get("reps")
        weight_kg = entry.set_data.get("weight_kg")

        exercise_sets.append({
            "exercises": [{
                "category": identity.category,
                "name": identity.name,
                "probability": identity.probability,
            }],
            "duration": round(entry.duration_s, 3),
            "repetitionCount": int(reps) if reps is not None else 0,
            "weight": float(round(weight_kg * 1000)) if weight_kg else 0.0,  # grams
            "setType": "ACTIVE",
            "startTime": _format_garmin_time(set_start),
            "wktStepIndex": entry.exercise_idx,
            "messageIndex": msg_idx,
        })

    return {"activityId": activity_id, "exerciseSets": exercise_sets}


_UNAUTHORIZED_RE = re.compile(r"API Error 401\b")
_INVALID_SUBCATEGORY_RE = re.compile(r"[Ii]nvalid [Ss]ub-?[Cc]ategory")


def _reraise_401_as_auth_error(e: Exception) -> None:
    """client.client.put() bypasses the high-level Garmin.connectapi()
    wrapper (it's GET-only — see push_exercise_sets), so it never gets that
    wrapper's 401 -> GarminConnectAuthenticationError translation. Without
    this, a dead session mid-push surfaces as a generic
    GarminConnectConnectionError, callers' `except GarminConnectAuthenticationError`
    never fires, reset_garmin_client() never gets called, and the poisoned
    cached client keeps failing every subsequent sync until the process
    restarts. Normalize here so every caller sees the same exception type
    regardless of which layer raised it."""
    if isinstance(e, GarminConnectConnectionError) and _UNAUTHORIZED_RE.search(str(e)):
        raise GarminConnectAuthenticationError(f"Session rejected mid-request: {e}") from e


def _is_invalid_subcategory_error(e: Exception) -> bool:
    return isinstance(e, GarminConnectConnectionError) and bool(_INVALID_SUBCATEGORY_RE.search(str(e)))


def _strip_all_names(payload: dict) -> dict:
    """Fallback for a 400 'Invalid Sub-Category': the PUT is atomic with no
    per-exercise error, so we can't tell which exercise's name was rejected.
    Strip every name (keep category + probability, which Garmin always
    accepts) and retry once, rather than failing the whole push over one
    bad subcategory. Simpler than hevy2garmin's bisect-and-strip — accepted
    tradeoff at this project's scale; upgrade to bisect if this proves to
    lose more names than expected in practice."""
    return {
        **payload,
        "exerciseSets": [
            {**s, "exercises": [{**ex, "name": None} for ex in s.get("exercises", [])]}
            for s in payload.get("exerciseSets", [])
        ],
    }


@retry(wait=wait_exponential(multiplier=1, min=2, max=20), stop=stop_after_attempt(3), reraise=True)
def get_existing_exercise_sets(client: Garmin, activity_id: int) -> dict | None:
    """Backup before push (plan §3 Phase C), so a bad push can be diagnosed.
    Best-effort for ordinary failures (network hiccup, 404) — but an auth
    failure must propagate, not be swallowed, so the caller aborts and
    resets the client instead of blindly proceeding to push with a session
    we already know is dead."""
    try:
        return client.connectapi(EXERCISE_SETS_PATH.format(activity_id=activity_id))
    except GarminConnectAuthenticationError:
        raise
    except Exception as e:  # noqa: BLE001
        _reraise_401_as_auth_error(e)
        logger.warning("Could not back up existing exerciseSets for activity %s: %s", activity_id, e)
        return None


def _put_exercise_sets(client: Garmin, activity_id: int, payload: dict) -> None:
    """Undocumented endpoint — confirmed working by hevy2garmin (#111/#112),
    live-tested against the real Garmin Connect API.

    NOTE: Garmin.connectapi() is GET-only (no `method` kwarg — confirmed by
    reading python-garminconnect's client.py: connectapi() hardcodes "GET").
    Every PUT call in the library itself goes through the lower-level
    `client.client.put("connectapi", path, json=..., api=True)`, so we do
    the same here rather than the higher-level wrapper.
    """
    try:
        client.client.put(
            "connectapi",
            EXERCISE_SETS_PATH.format(activity_id=activity_id),
            json=payload,
            api=True,
        )
    except GarminConnectConnectionError as e:
        _reraise_401_as_auth_error(e)
        raise


@retry(wait=wait_exponential(multiplier=1, min=2, max=20), stop=stop_after_attempt(3), reraise=True)
def push_exercise_sets(client: Garmin, activity_id: int, payload: dict) -> None:
    try:
        _put_exercise_sets(client, activity_id, payload)
    except GarminConnectConnectionError as e:
        if not _is_invalid_subcategory_error(e):
            raise
        logger.warning(
            "Activity %s rejected a subcategory name (400 Invalid Sub-Category) — "
            "retrying once with all names stripped (category preserved): %s",
            activity_id, e,
        )
        _put_exercise_sets(client, activity_id, _strip_all_names(payload))

    logger.info("Pushed %d exercise sets to activity %s", len(payload.get("exerciseSets", [])), activity_id)
