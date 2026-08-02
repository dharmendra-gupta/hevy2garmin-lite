"""Set timeline synthesis (Phase E of the implementation plan).

Hevy gives no per-set timestamps — only weight/reps/type and sometimes
duration_seconds. We estimate a plausible timeline and scale it to the real
activity duration, anchored to the WATCH activity's own start/end (never
Hevy's start_time — the two can differ by MATCH_TOLERANCE_MINUTES, and
anchoring to the wrong clock silently skews HR association; see plan §3
Phase E and hevy2garmin's v0.6.3 regression).
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_SCALE = 0.3
MAX_SCALE = 2.0


@dataclass
class TimelineConfig:
    working_set_seconds: float = 40.0
    warmup_set_seconds: float = 25.0
    rest_between_sets_seconds: float = 75.0
    rest_between_exercises_seconds: float = 120.0


@dataclass
class SetEntry:
    exercise_idx: int
    set_data: dict
    start_offset_s: float
    duration_s: float
    set_type: str  # "ACTIVE" | "REST"


def _estimate_set_duration(set_data: dict, is_warmup: bool, config: TimelineConfig) -> float:
    explicit = set_data.get("duration_seconds")
    if explicit and explicit > 0:
        return float(explicit)
    return config.warmup_set_seconds if is_warmup else config.working_set_seconds


def build_set_timeline(
    exercises: list[dict],
    activity_duration_s: float,
    config: TimelineConfig | None = None,
) -> list[SetEntry]:
    """Returns a flat, time-ordered list of SetEntry (ACTIVE and REST),
    with start_offset_s/duration_s relative to the activity's own start.
    Guarantees no entry's end exceeds activity_duration_s."""
    config = config or TimelineConfig()

    planned: list[dict] = []
    num_exercises = len(exercises)
    for ex_idx, ex in enumerate(exercises):
        sets = ex.get("sets", [])
        for s_idx, s in enumerate(sets):
            is_warmup = s.get("type", "normal") == "warmup"
            set_dur = _estimate_set_duration(s, is_warmup, config)

            is_last_set_of_exercise = s_idx == len(sets) - 1
            is_last_exercise = ex_idx == num_exercises - 1
            if is_last_set_of_exercise and is_last_exercise:
                rest_dur = 0.0
            elif is_last_set_of_exercise:
                rest_dur = config.rest_between_exercises_seconds
            else:
                rest_dur = config.rest_between_sets_seconds

            planned.append({
                "ex_idx": ex_idx,
                "set_data": s,
                "set_dur": set_dur,
                "rest_dur": rest_dur,
            })

    if not planned:
        return []

    ideal_total = sum(p["set_dur"] + p["rest_dur"] for p in planned)
    scale = activity_duration_s / ideal_total if ideal_total > 0 else 1.0
    scale = max(MIN_SCALE, min(MAX_SCALE, scale))

    entries: list[SetEntry] = []
    cursor = 0.0
    for p in planned:
        scaled_set = p["set_dur"] * scale
        # Clamp: never let a set start past the activity's real end, and
        # never let its end exceed it either (scale-clamp saturation guard —
        # plan §3 Phase E "Scale Clamp Leakage").
        set_start = min(cursor, activity_duration_s)
        set_end = min(cursor + scaled_set, activity_duration_s)
        entries.append(SetEntry(
            exercise_idx=p["ex_idx"],
            set_data=p["set_data"],
            start_offset_s=set_start,
            duration_s=max(0.0, set_end - set_start),
            set_type="ACTIVE",
        ))
        cursor = set_end

        if p["rest_dur"] > 0:
            scaled_rest = p["rest_dur"] * scale
            rest_start = min(cursor, activity_duration_s)
            rest_end = min(cursor + scaled_rest, activity_duration_s)
            if rest_end > rest_start:
                entries.append(SetEntry(
                    exercise_idx=p["ex_idx"],
                    set_data={},
                    start_offset_s=rest_start,
                    duration_s=rest_end - rest_start,
                    set_type="REST",
                ))
            cursor = rest_end

    return entries
