"""Coverage for learn_mappings_from_garmin (src/learn.py) — the "learn from
Garmin" feature: if a user manually corrects an unmapped/custom exercise in
Garmin Connect's own "Choose an Exercise" UI, we read that correction back
and turn it into a validated (category, name) override, instead of the
user only ever being able to assign a category by hand.

Live spike (2026-08-01, activity 23810842954) confirmed: a manual UI
correction round-trips as a genuine fit_tool name string, but wktStepIndex/
messageIndex come back null on the corrected entry — so matching a Garmin
exerciseSets entry back to the Hevy exercise it belongs to has to go by
startTime (which does survive intact), not by index.
"""

from datetime import UTC, datetime, timedelta

from src.learn import LearnedMapping, learn_mappings_from_garmin
from src.timeline import build_set_timeline

ACTIVITY_START = datetime(2026, 8, 1, 9, 3, 6, tzinfo=UTC)
ACTIVITY_DURATION_S = 200.0


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.0")


def _active_set_start_time(hevy_exercises: list[dict], exercise_idx: int = 0) -> str:
    entries = build_set_timeline(hevy_exercises, ACTIVITY_DURATION_S)
    entry = next(e for e in entries if e.set_type == "ACTIVE" and e.exercise_idx == exercise_idx)
    return _fmt(ACTIVITY_START + timedelta(seconds=entry.start_offset_s))


def _hevy_exercise(template_id: str, title: str = "Custom Exercise") -> dict:
    return {
        "exercise_template_id": template_id,
        "title": title,
        "sets": [{"type": "normal", "reps": 8, "weight_kg": 40}],
    }


def test_learns_a_corrected_custom_exercise_matched_by_start_time():
    hevy_exercises = [_hevy_exercise("CUSTOM123", "My Custom Press")]
    start_time = _active_set_start_time(hevy_exercises)
    garmin_response = {
        "exerciseSets": [{
            "exercises": [{"category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS", "probability": 100.0}],
            "startTime": start_time,
            "setType": "ACTIVE",
        }],
    }

    learned = learn_mappings_from_garmin(
        hevy_exercises, garmin_response, ACTIVITY_START, ACTIVITY_DURATION_S,
        already_known_template_ids=set(),
    )

    assert learned == [LearnedMapping(template_id="CUSTOM123", category="BENCH_PRESS", name="BARBELL_BENCH_PRESS")]


def test_ignores_sets_whose_start_time_does_not_match_anything():
    hevy_exercises = [_hevy_exercise("CUSTOM123")]
    garmin_response = {
        "exerciseSets": [{
            "exercises": [{"category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS", "probability": 100.0}],
            "startTime": "1999-01-01T00:00:00.0",
            "setType": "ACTIVE",
        }],
    }

    learned = learn_mappings_from_garmin(
        hevy_exercises, garmin_response, ACTIVITY_START, ACTIVITY_DURATION_S,
        already_known_template_ids=set(),
    )

    assert learned == []


def test_ignores_rest_sets_with_no_exercises():
    hevy_exercises = [_hevy_exercise("CUSTOM123")]
    start_time = _active_set_start_time(hevy_exercises)
    garmin_response = {
        "exerciseSets": [
            {"exercises": [], "startTime": start_time, "setType": "REST"},
        ],
    }

    learned = learn_mappings_from_garmin(
        hevy_exercises, garmin_response, ACTIVITY_START, ACTIVITY_DURATION_S,
        already_known_template_ids=set(),
    )

    assert learned == []


def test_skips_template_ids_already_known_via_catalog_or_override():
    hevy_exercises = [_hevy_exercise("79D0BB3A")]  # a real bundled catalog id (Bench Press)
    start_time = _active_set_start_time(hevy_exercises)
    garmin_response = {
        "exerciseSets": [{
            "exercises": [{"category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS", "probability": 100.0}],
            "startTime": start_time,
            "setType": "ACTIVE",
        }],
    }

    learned = learn_mappings_from_garmin(
        hevy_exercises, garmin_response, ACTIVITY_START, ACTIVITY_DURATION_S,
        already_known_template_ids={"79D0BB3A"},
    )

    assert learned == []


def test_rejects_an_invalid_category_name_pair():
    hevy_exercises = [_hevy_exercise("CUSTOM123")]
    start_time = _active_set_start_time(hevy_exercises)
    garmin_response = {
        "exerciseSets": [{
            "exercises": [{"category": "BENCH_PRESS", "name": "NOT_A_REAL_SUBCATEGORY", "probability": 100.0}],
            "startTime": start_time,
            "setType": "ACTIVE",
        }],
    }

    learned = learn_mappings_from_garmin(
        hevy_exercises, garmin_response, ACTIVITY_START, ACTIVITY_DURATION_S,
        already_known_template_ids=set(),
    )

    assert learned == []


def test_dedupes_multiple_sets_of_the_same_custom_exercise():
    hevy_exercises = [{
        "exercise_template_id": "CUSTOM123",
        "title": "My Custom Press",
        "sets": [
            {"type": "normal", "reps": 8, "weight_kg": 40},
            {"type": "normal", "reps": 6, "weight_kg": 45},
        ],
    }]
    entries = build_set_timeline(hevy_exercises, ACTIVITY_DURATION_S)
    active_entries = [e for e in entries if e.set_type == "ACTIVE"]
    assert len(active_entries) == 2

    garmin_response = {
        "exerciseSets": [
            {
                "exercises": [{"category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS", "probability": 100.0}],
                "startTime": _fmt(ACTIVITY_START + timedelta(seconds=e.start_offset_s)),
                "setType": "ACTIVE",
            }
            for e in active_entries
        ],
    }

    learned = learn_mappings_from_garmin(
        hevy_exercises, garmin_response, ACTIVITY_START, ACTIVITY_DURATION_S,
        already_known_template_ids=set(),
    )

    assert learned == [LearnedMapping(template_id="CUSTOM123", category="BENCH_PRESS", name="BARBELL_BENCH_PRESS")]
