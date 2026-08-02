"""Activity matching (Phase B of the implementation plan).

Rule: >=70% temporal overlap AND start drift within MATCH_TOLERANCE_MINUTES
AND activity type "strength_training". Matching always uses Garmin's
startTimeGMT (UTC) — never startTimeLocal — compared against Hevy's Z-suffixed
UTC timestamps. This mirrors hevy2garmin's proven heuristic; naive
start-time-only matching mismatches back-to-back sessions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.config import settings
from src.hevy_client import parse_hevy_timestamp

MIN_OVERLAP_PCT = 0.70
STRENGTH_TRAINING_TYPE_KEY = "strength_training"


def parse_garmin_gmt(raw: str) -> datetime:
    """Garmin's startTimeGMT is a naive 'YYYY-MM-DD HH:MM:SS' string
    representing UTC. See plan §3 Phase B — never use startTimeLocal here."""
    naive = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    return naive.replace(tzinfo=UTC)


def parse_activity_summary_gmt(raw: str) -> datetime:
    """client.get_activity()'s summaryDTO.startTimeGMT uses a DIFFERENT
    format ("2026-08-01T09:03:06.0") than get_activities()'s startTimeGMT
    ("2026-08-01 09:03:06", parsed by parse_garmin_gmt above) — confirmed
    live 2026-08-01. Used by the "learn from Garmin" endpoint, which needs
    get_activity() (not get_activities()) to fetch a single known activity."""
    naive = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%f")
    return naive.replace(tzinfo=UTC)


def _overlap_seconds(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> float:
    latest_start = max(a_start, b_start)
    earliest_end = min(a_end, b_end)
    return max(0.0, (earliest_end - latest_start).total_seconds())


def _is_strength_training(activity: dict) -> bool:
    activity_type = activity.get("activityType") or {}
    return activity_type.get("typeKey") == STRENGTH_TRAINING_TYPE_KEY


def find_best_match(
    hevy_workout: dict,
    garmin_activities: list[dict],
    already_claimed_ids: set[int],
    tolerance_minutes: int | None = None,
) -> dict | None:
    """Returns the best-matching Garmin activity dict, or None."""
    tolerance = timedelta(minutes=tolerance_minutes if tolerance_minutes is not None else settings.MATCH_TOLERANCE_MINUTES)

    hevy_start = parse_hevy_timestamp(hevy_workout["start_time"])
    hevy_end = parse_hevy_timestamp(hevy_workout["end_time"])
    hevy_duration_s = (hevy_end - hevy_start).total_seconds()
    if hevy_duration_s <= 0:
        return None

    candidates: list[tuple[float, float, dict]] = []  # (overlap_pct, drift_s, activity)

    for activity in garmin_activities:
        activity_id = activity.get("activityId")
        if activity_id in already_claimed_ids:
            continue
        if not _is_strength_training(activity):
            continue

        duration_s = activity.get("duration") or 0
        if duration_s <= 0:
            continue

        start_gmt = activity.get("startTimeGMT")
        if not start_gmt:
            continue

        act_start = parse_garmin_gmt(start_gmt)
        act_end = act_start + timedelta(seconds=duration_s)

        drift = abs((act_start - hevy_start).total_seconds())
        if drift > tolerance.total_seconds():
            continue

        overlap_s = _overlap_seconds(hevy_start, hevy_end, act_start, act_end)
        overlap_pct = overlap_s / hevy_duration_s
        if overlap_pct < MIN_OVERLAP_PCT:
            continue

        candidates.append((overlap_pct, drift, activity))

    if not candidates:
        return None

    # Highest overlap wins; break ties by smallest drift.
    candidates.sort(key=lambda c: (-c[0], c[1]))
    return candidates[0][2]
