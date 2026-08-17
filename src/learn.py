"""Learn exact (category, name) mappings for custom/unmapped exercises by
reading back a user's manual correction in Garmin Connect's own "Choose an
Exercise" UI (see the "learn from Garmin" feature plan).

Matches by ARRAY POSITION / per-exercise set-count grouping, not by
reconstructing a timeline. The original design reconstructed a synthesized
timeline and joined Garmin's exerciseSets back to Hevy exercises by
startTime string equality — fragile in practice, because any drift between
the timeline-tuning config in effect at push-time vs at learn-time (e.g.
the user adjusted the dashboard's Timeline Tuning sliders in between) broke
the exact match, even though nothing was actually wrong. Confirmed live
2026-08-17 (activity 24010259873) that Garmin's exerciseSets GET response
instead preserves the *exact* array order/grouping it was pushed in, even
around a manually corrected entry: wktStepIndex/messageIndex null out on
the corrected ACTIVE entry, but its array POSITION never moves. So we split
Garmin's ACTIVE-only entries into consecutive groups sized by each Hevy
exercise's own set count, in the same order — purely structural, no time
reconstruction, no config dependency, no drift to chase.

Kept separate from mapping.py/push.py to avoid a circular import (push.py
already imports from mapping.py; this needs both).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.mapping import _validate_category_name_pair

logger = logging.getLogger("hevy2garmin_lite.learn")


@dataclass(frozen=True)
class LearnedMapping:
    template_id: str
    category: str
    name: str


def learn_mappings_from_garmin(
    hevy_exercises: list[dict],
    garmin_exercise_sets: dict,
    already_known_template_ids: set[str],
) -> list[LearnedMapping]:
    active_entries = [s for s in garmin_exercise_sets.get("exerciseSets", []) if s.get("exercises")]
    set_counts = [len(ex.get("sets", [])) for ex in hevy_exercises]

    if len(active_entries) != sum(set_counts):
        logger.warning(
            "Garmin ACTIVE set count (%d) does not match Hevy's total set count (%d) for this activity — "
            "positional grouping would be unsafe, refusing to guess. Learning nothing this run.",
            len(active_entries), sum(set_counts),
        )
        return []

    learned: list[LearnedMapping] = []
    seen_template_ids: set[str] = set()
    cursor = 0
    for exercise_idx, count in enumerate(set_counts):
        group = active_entries[cursor:cursor + count]
        cursor += count

        template_id = hevy_exercises[exercise_idx].get("exercise_template_id")
        if not template_id or template_id in seen_template_ids or template_id in already_known_template_ids:
            continue

        identities = {(entry["exercises"][0].get("category"), entry["exercises"][0].get("name")) for entry in group}
        if len(identities) != 1:
            logger.warning(
                "Exercise %d (template_id=%s) has inconsistent (category, name) across its %d Garmin "
                "set(s) — refusing to guess which is right, skipping.",
                exercise_idx, template_id, count,
            )
            continue

        category, name = next(iter(identities))
        if not category or not name or not _validate_category_name_pair(category, name):
            continue

        learned.append(LearnedMapping(template_id=template_id, category=category, name=name))
        seen_template_ids.add(template_id)

    return learned
