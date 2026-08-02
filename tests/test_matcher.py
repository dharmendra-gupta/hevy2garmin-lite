from datetime import UTC, datetime

from src.matcher import find_best_match, parse_activity_summary_gmt, parse_garmin_gmt


def hevy_workout(start_iso: str, end_iso: str) -> dict:
    return {"start_time": start_iso, "end_time": end_iso}


def garmin_activity(activity_id: int, start_gmt: str, duration_s: float, type_key: str = "strength_training") -> dict:
    return {
        "activityId": activity_id,
        "startTimeGMT": start_gmt,
        "duration": duration_s,
        "activityType": {"typeKey": type_key},
    }


def test_parse_garmin_gmt_is_utc_aware():
    dt = parse_garmin_gmt("2026-08-01 10:00:00")
    assert dt.tzinfo == UTC
    assert dt.hour == 10


def test_exact_overlap_matches():
    workout = hevy_workout("2026-08-01T10:00:00Z", "2026-08-01T11:00:00Z")
    activity = garmin_activity(1, "2026-08-01 10:00:00", 3600)
    match = find_best_match(workout, [activity], set())
    assert match is not None
    assert match["activityId"] == 1


def test_startTimeLocal_would_fail_the_match_if_used_instead():
    # If the matcher accidentally used startTimeLocal (naive local time,
    # sitting at a different offset than UTC) instead of startTimeGMT, the
    # apparent drift would blow past tolerance and this match would be lost.
    # This guards against exactly that regression class (plan §6).
    workout = hevy_workout("2026-08-01T10:00:00Z", "2026-08-01T11:00:00Z")
    activity_gmt = garmin_activity(1, "2026-08-01 10:00:00", 3600)
    match_gmt = find_best_match(workout, [activity_gmt], set())
    assert match_gmt is not None

    # Simulate what would happen if startTimeLocal (e.g. UTC+5:30, India) had
    # been used as if it were UTC: apparent start is 15:30, drift ~5.5h > tolerance.
    activity_local_misused = garmin_activity(1, "2026-08-01 15:30:00", 3600)
    match_bad = find_best_match(workout, [activity_local_misused], set(), tolerance_minutes=15)
    assert match_bad is None


def test_activities_exactly_at_tolerance_edge_boundary():
    workout = hevy_workout("2026-08-01T10:00:00Z", "2026-08-01T11:00:00Z")
    # Drift exactly 15 minutes — within tolerance (<=).
    activity = garmin_activity(1, "2026-08-01 10:15:00", 3600)
    match = find_best_match(workout, [activity], set(), tolerance_minutes=15)
    assert match is not None

    # Drift 15 minutes + 1 second — outside tolerance.
    activity_over = garmin_activity(2, "2026-08-01 10:15:01", 3600)
    match_over = find_best_match(workout, [activity_over], set(), tolerance_minutes=15)
    assert match_over is None


def test_midnight_spanning_workout_matches():
    workout = hevy_workout("2026-08-01T23:30:00Z", "2026-08-02T00:30:00Z")
    activity = garmin_activity(1, "2026-08-01 23:30:00", 3600)
    match = find_best_match(workout, [activity], set())
    assert match is not None


def test_back_to_back_sessions_do_not_cross_match():
    # Two consecutive hour-long sessions; a Hevy workout matching the first
    # must not also match the second, and vice versa via overlap threshold.
    workout = hevy_workout("2026-08-01T10:00:00Z", "2026-08-01T11:00:00Z")
    first = garmin_activity(1, "2026-08-01 10:00:00", 3600)
    second = garmin_activity(2, "2026-08-01 11:00:00", 3600)  # starts exactly when first ends
    match = find_best_match(workout, [first, second], set())
    assert match is not None
    assert match["activityId"] == 1


def test_low_overlap_below_70_percent_does_not_match():
    workout = hevy_workout("2026-08-01T10:00:00Z", "2026-08-01T11:00:00Z")  # 1 hour
    # Activity overlaps only the first 20 minutes of the workout (33%).
    activity = garmin_activity(1, "2026-08-01 09:50:00", 1200)
    match = find_best_match(workout, [activity], set(), tolerance_minutes=15)
    assert match is None


def test_non_strength_training_type_excluded():
    workout = hevy_workout("2026-08-01T10:00:00Z", "2026-08-01T11:00:00Z")
    activity = garmin_activity(1, "2026-08-01 10:00:00", 3600, type_key="running")
    match = find_best_match(workout, [activity], set())
    assert match is None


def test_already_claimed_activity_is_skipped():
    workout = hevy_workout("2026-08-01T10:00:00Z", "2026-08-01T11:00:00Z")
    activity = garmin_activity(1, "2026-08-01 10:00:00", 3600)
    match = find_best_match(workout, [activity], already_claimed_ids={1})
    assert match is None


def test_multiple_candidates_picks_highest_overlap():
    workout = hevy_workout("2026-08-01T10:00:00Z", "2026-08-01T11:00:00Z")
    partial = garmin_activity(1, "2026-08-01 10:00:00", 2520)  # 70% overlap exactly
    full = garmin_activity(2, "2026-08-01 10:00:00", 3600)     # 100% overlap
    match = find_best_match(workout, [partial, full], set())
    assert match["activityId"] == 2


# --- client.get_activity()'s summaryDTO.startTimeGMT uses a DIFFERENT
# format ("2026-08-01T09:03:06.0") than get_activities()'s startTimeGMT
# ("2026-08-01 09:03:06", parsed by parse_garmin_gmt above) — confirmed live
# 2026-08-01 against activity 23810842954 while building the "learn from
# Garmin" feature. Needed there to reconstruct the timeline for startTime
# matching (src/learn.py) via src/main.py's learn-from-garmin endpoint.

def test_parse_activity_summary_gmt_handles_iso_t_format_with_fraction():
    parsed = parse_activity_summary_gmt("2026-08-01T09:03:06.0")
    assert parsed == datetime(2026, 8, 1, 9, 3, 6, tzinfo=UTC)
