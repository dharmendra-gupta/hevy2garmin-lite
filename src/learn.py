"""Learn exact (category, name) mappings for custom/unmapped exercises by
reading back a user's manual correction in Garmin Connect's own "Choose an
Exercise" UI (see the "learn from Garmin" feature plan).

Live spike (2026-08-01, activity 23810842954): a manual UI correction reads
back as a genuine fit_tool name string via GET exerciseSets — but
wktStepIndex/messageIndex both come back null on the corrected entry, so we
can't join a Garmin response entry back to the Hevy exercise it belongs to
by index. startTime *does* survive intact, so matching here reconstructs the
same synthesized timeline push.py used originally and joins on that instead.

Kept separate from mapping.py/push.py to avoid a circular import (push.py
already imports from mapping.py; this needs both).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from src.mapping import _validate_category_name_pair
from src.timeline import TimelineConfig, build_set_timeline


@dataclass(frozen=True)
class LearnedMapping:
    template_id: str
    category: str
    name: str


def _format_garmin_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.0")


def learn_mappings_from_garmin(
    hevy_exercises: list[dict],
    garmin_exercise_sets: dict,
    activity_start: datetime,
    activity_duration_s: float,
    already_known_template_ids: set[str],
    timeline_config: TimelineConfig | None = None,
) -> list[LearnedMapping]:
    entries = build_set_timeline(hevy_exercises, activity_duration_s, timeline_config)
    exercise_idx_by_start_time: dict[str, int] = {
        _format_garmin_time(activity_start + timedelta(seconds=entry.start_offset_s)): entry.exercise_idx
        for entry in entries
        if entry.set_type == "ACTIVE"
    }

    learned: list[LearnedMapping] = []
    seen_template_ids: set[str] = set()

    for garmin_set in garmin_exercise_sets.get("exerciseSets", []):
        exercise_idx = exercise_idx_by_start_time.get(garmin_set.get("startTime"))
        if exercise_idx is None:
            continue

        exercises_field = garmin_set.get("exercises") or []
        if not exercises_field:
            continue

        template_id = hevy_exercises[exercise_idx].get("exercise_template_id")
        if not template_id or template_id in seen_template_ids:
            continue
        # Deliberately NOT `template_id in TEMPLATE_TO_FIT` here — a catalog
        # entry can be present but still resolve to generic TOTAL_BODY/no
        # name (unresolved category/subcategory). already_known_template_ids
        # (mapper.known_template_ids()) already accounts for that correctly;
        # a raw catalog-membership check here would re-introduce the exact
        # bug fixed in mapping.py 2026-08-17, just one layer up.
        if template_id in already_known_template_ids:
            continue

        category = exercises_field[0].get("category")
        name = exercises_field[0].get("name")
        if not category or not name or not _validate_category_name_pair(category, name):
            continue

        learned.append(LearnedMapping(template_id=template_id, category=category, name=name))
        seen_template_ids.add(template_id)

    return learned
