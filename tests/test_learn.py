"""Coverage for learn_mappings_from_garmin (src/learn.py) — the "learn from
Garmin" feature: if a user manually corrects an unmapped/custom exercise in
Garmin Connect's own "Choose an Exercise" UI, we read that correction back
and turn it into a validated (category, name) override, instead of the
user only ever being able to assign a category by hand.

Matches by ARRAY POSITION / per-exercise set-count grouping, not by
reconstructing a timeline. The original design reconstructed a synthesized
timeline and joined on startTime string equality — fragile, because any
drift between the timeline-tuning config in effect at push-time vs at
learn-time (e.g. the user adjusted the dashboard's Timeline Tuning sliders
in between) broke the exact match, even though nothing was actually wrong.
Confirmed live 2026-08-17 (activity 24010259873) that Garmin's exerciseSets
GET response instead preserves the *exact* array order/grouping it was
pushed in, even around a manually corrected entry — wktStepIndex/
messageIndex null out on the corrected ACTIVE entry, but its array
POSITION never moves. So we split Garmin's ACTIVE-only entries into
consecutive groups sized by each Hevy exercise's own set count, in the same
order — purely structural, no time reconstruction, no config dependency.
"""

from src.learn import LearnedMapping, learn_mappings_from_garmin


def _hevy_exercise(template_id: str, num_sets: int, title: str = "Custom Exercise") -> dict:
    return {
        "exercise_template_id": template_id,
        "title": title,
        "sets": [{"type": "normal", "reps": 8, "weight_kg": 40} for _ in range(num_sets)],
    }


def _active(category: str, name: str, wkt_step_index=0, message_index=0) -> dict:
    return {
        "exercises": [{"category": category, "name": name, "probability": 100.0}],
        "setType": "ACTIVE",
        "wktStepIndex": wkt_step_index,
        "messageIndex": message_index,
    }


def _rest(wkt_step_index=0, message_index=0) -> dict:
    return {"exercises": [], "setType": "REST", "wktStepIndex": wkt_step_index, "messageIndex": message_index}


def test_learns_a_corrected_custom_exercise_matched_by_position():
    hevy_exercises = [_hevy_exercise("CUSTOM123", 1, "My Custom Press")]
    garmin_response = {"exerciseSets": [_active("BENCH_PRESS", "BARBELL_BENCH_PRESS")]}

    learned = learn_mappings_from_garmin(hevy_exercises, garmin_response, already_known_template_ids=set())

    assert learned == [LearnedMapping(template_id="CUSTOM123", category="BENCH_PRESS", name="BARBELL_BENCH_PRESS")]


def test_groups_multiple_sets_of_one_exercise_by_count():
    # 2 hevy exercises: first has 2 sets, second has 1 — Garmin's ACTIVE
    # entries (with rests interspersed) must be split 2/1, in order.
    hevy_exercises = [_hevy_exercise("EX_A", 2, "Exercise A"), _hevy_exercise("EX_B", 1, "Exercise B")]
    garmin_response = {"exerciseSets": [
        _active("BENCH_PRESS", "BARBELL_BENCH_PRESS"), _rest(),
        _active("BENCH_PRESS", "BARBELL_BENCH_PRESS"), _rest(),
        _active("DEADLIFT", "BARBELL_DEADLIFT"),
    ]}

    learned = learn_mappings_from_garmin(hevy_exercises, garmin_response, already_known_template_ids=set())

    assert learned == [
        LearnedMapping(template_id="EX_A", category="BENCH_PRESS", name="BARBELL_BENCH_PRESS"),
        LearnedMapping(template_id="EX_B", category="DEADLIFT", name="BARBELL_DEADLIFT"),
    ]


def test_matches_even_when_wktstepindex_is_null_on_corrected_entry():
    # Regression for the exact live case that motivated this design: a
    # manually corrected ACTIVE entry has wktStepIndex/messageIndex == None,
    # but its array position still identifies which exercise it belongs to.
    hevy_exercises = [_hevy_exercise("EX_A", 1), _hevy_exercise("CORRECTED", 1)]
    garmin_response = {"exerciseSets": [
        _active("SQUAT", "BARBELL_BACK_SQUAT", wkt_step_index=0, message_index=0),
        _rest(wkt_step_index=0, message_index=1),
        _active("LEG_RAISE", "HANGING_LEG_RAISE", wkt_step_index=None, message_index=None),
    ]}

    learned = learn_mappings_from_garmin(hevy_exercises, garmin_response, already_known_template_ids=set())

    assert LearnedMapping(template_id="CORRECTED", category="LEG_RAISE", name="HANGING_LEG_RAISE") in learned


def test_skips_template_ids_already_known():
    hevy_exercises = [_hevy_exercise("79D0BB3A", 1)]
    garmin_response = {"exerciseSets": [_active("BENCH_PRESS", "BARBELL_BENCH_PRESS")]}

    learned = learn_mappings_from_garmin(
        hevy_exercises, garmin_response, already_known_template_ids={"79D0BB3A"},
    )

    assert learned == []


def test_rejects_an_invalid_category_name_pair():
    hevy_exercises = [_hevy_exercise("CUSTOM123", 1)]
    garmin_response = {"exerciseSets": [_active("BENCH_PRESS", "NOT_A_REAL_SUBCATEGORY")]}

    learned = learn_mappings_from_garmin(hevy_exercises, garmin_response, already_known_template_ids=set())

    assert learned == []


def test_bails_out_entirely_if_active_set_count_does_not_match_hevy():
    # Safety net: if the totals don't line up (e.g. the Hevy workout was
    # edited after syncing), positional grouping becomes unsafe — refuse to
    # guess rather than risk writing a wrong mapping.
    hevy_exercises = [_hevy_exercise("CUSTOM123", 2)]  # expects 2 ACTIVE entries
    garmin_response = {"exerciseSets": [_active("BENCH_PRESS", "BARBELL_BENCH_PRESS")]}  # only 1

    learned = learn_mappings_from_garmin(hevy_exercises, garmin_response, already_known_template_ids=set())

    assert learned == []


def test_dedupes_when_the_same_template_id_appears_in_two_exercise_blocks():
    # e.g. the same exercise done in two separate blocks of the workout —
    # only the first resolved occurrence is kept.
    hevy_exercises = [_hevy_exercise("CUSTOM123", 1, "Block 1"), _hevy_exercise("CUSTOM123", 1, "Block 2")]
    garmin_response = {"exerciseSets": [
        _active("BENCH_PRESS", "BARBELL_BENCH_PRESS"),
        _active("BENCH_PRESS", "BARBELL_BENCH_PRESS"),
    ]}

    learned = learn_mappings_from_garmin(hevy_exercises, garmin_response, already_known_template_ids=set())

    assert learned == [LearnedMapping(template_id="CUSTOM123", category="BENCH_PRESS", name="BARBELL_BENCH_PRESS")]


def test_skips_exercise_whose_grouped_sets_disagree_on_identity():
    # If Garmin's grouped sets for one exercise don't agree on (category,
    # name), something's inconsistent — refuse to guess which is right.
    hevy_exercises = [_hevy_exercise("CUSTOM123", 2)]
    garmin_response = {"exerciseSets": [
        _active("BENCH_PRESS", "BARBELL_BENCH_PRESS"),
        _active("SQUAT", "BARBELL_BACK_SQUAT"),
    ]}

    learned = learn_mappings_from_garmin(hevy_exercises, garmin_response, already_known_template_ids=set())

    assert learned == []


def test_ignores_rest_sets_with_no_exercises():
    hevy_exercises = [_hevy_exercise("CUSTOM123", 1)]
    garmin_response = {"exerciseSets": [_rest(), _active("BENCH_PRESS", "BARBELL_BENCH_PRESS")]}

    learned = learn_mappings_from_garmin(hevy_exercises, garmin_response, already_known_template_ids=set())

    assert learned == [LearnedMapping(template_id="CUSTOM123", category="BENCH_PRESS", name="BARBELL_BENCH_PRESS")]
