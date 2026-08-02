from src.timeline import TimelineConfig, build_set_timeline

DEFAULT_CONFIG = TimelineConfig(
    working_set_seconds=40, warmup_set_seconds=25,
    rest_between_sets_seconds=75, rest_between_exercises_seconds=120,
)


def make_exercise(num_sets: int, reps=8, weight_kg=60.0, warmup=False):
    return {
        "exercise_template_id": "D04AC939",
        "title": "Squat (Barbell)",
        "sets": [
            {"type": "warmup" if warmup else "normal", "reps": reps, "weight_kg": weight_kg}
            for _ in range(num_sets)
        ],
    }


def test_ordering_preserved_and_rest_fills_gaps():
    exercises = [make_exercise(2), make_exercise(1)]
    # ideal: 40+75+40+120+40 = 315s, give exactly that so scale == 1.0
    entries = build_set_timeline(exercises, activity_duration_s=315, config=DEFAULT_CONFIG)

    active = [e for e in entries if e.set_type == "ACTIVE"]
    rests = [e for e in entries if e.set_type == "REST"]
    assert len(active) == 3
    assert len(rests) == 2  # between the 2 sets of ex0, and between ex0 and ex1 — none after the last set

    # monotonic, non-overlapping
    prev_end = 0.0
    for e in entries:
        assert e.start_offset_s >= prev_end - 1e-6
        prev_end = e.start_offset_s + e.duration_s

    # exercise index sequence matches input order
    assert [e.exercise_idx for e in active] == [0, 0, 1]


def test_no_rest_after_final_set():
    exercises = [make_exercise(1)]
    entries = build_set_timeline(exercises, activity_duration_s=40, config=DEFAULT_CONFIG)
    assert len(entries) == 1
    assert entries[0].set_type == "ACTIVE"


def test_scale_clamp_upper_bound_no_overflow_past_activity_end():
    # 90-minute logged plan compressed against a 20-minute (1200s) activity —
    # scale would want to be far below MIN_SCALE; clamp should still ensure
    # no entry's end exceeds activity_duration_s.
    exercises = [make_exercise(20)]  # lots of sets, large ideal_total
    activity_duration_s = 1200.0
    entries = build_set_timeline(exercises, activity_duration_s, config=DEFAULT_CONFIG)
    assert entries
    for e in entries:
        assert e.start_offset_s <= activity_duration_s + 1e-6
        assert (e.start_offset_s + e.duration_s) <= activity_duration_s + 1e-6


def test_scale_clamp_lower_bound_no_overflow():
    # A 2-minute logged workout against a 60-minute activity — scale would
    # want to be far above MAX_SCALE (2.0); still must not exceed the
    # activity's real duration once clamped.
    exercises = [make_exercise(1)]
    activity_duration_s = 3600.0
    entries = build_set_timeline(exercises, activity_duration_s, config=DEFAULT_CONFIG)
    for e in entries:
        assert (e.start_offset_s + e.duration_s) <= activity_duration_s + 1e-6


def test_explicit_duration_seconds_used_for_cardio_sets():
    exercise = {
        "exercise_template_id": "D8F7F851",
        "title": "Cycling",
        "sets": [{"type": "normal", "duration_seconds": 300, "reps": None, "weight_kg": None}],
    }
    entries = build_set_timeline([exercise], activity_duration_s=300, config=DEFAULT_CONFIG)
    active = [e for e in entries if e.set_type == "ACTIVE"]
    assert len(active) == 1
    assert abs(active[0].duration_s - 300) < 1e-6


def test_empty_exercises_returns_empty_timeline():
    assert build_set_timeline([], activity_duration_s=600, config=DEFAULT_CONFIG) == []


def test_warmup_only_workout_does_not_crash():
    exercises = [make_exercise(2, warmup=True)]
    entries = build_set_timeline(exercises, activity_duration_s=100, config=DEFAULT_CONFIG)
    assert len(entries) >= 2
