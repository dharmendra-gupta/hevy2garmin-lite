"""Exercise -> Garmin (category, name) resolution (Phase D of the plan).

REVISED: live testing (Phase G) disproved the original premise that Garmin
ignores pushed exercise names on watch-recorded activities. It doesn't —
that only appeared true because our first test sent `probability: null`.
With a real `probability` value AND a correct fit_tool-resolved subcategory
`name`, the specific exercise (e.g. "Bench Press (Barbell)") renders
correctly on both web and mobile, on a genuinely watch-recorded activity.
So we now resolve full identity, not just category — mirroring
hevy2garmin's own `_exercise_to_string()` mechanism exactly (same
`fit-tool` package, same dynamic enum lookup), since we'd already ported
their (category, subcategory) source data and were only discarding half of it.
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from src.template_map_source import TEMPLATE_TO_FIT

logger = logging.getLogger("hevy2garmin_lite.mapping")

# FIT SDK category id -> Garmin API category string. Matches fit_tool's own
# ExerciseCategory enum exactly (verified against bikemap/fit_tool source).
CATEGORY_NAMES: dict[int, str] = {
    0: "BENCH_PRESS", 1: "CALF_RAISE", 2: "CARDIO", 3: "CARRY",
    4: "CHOP", 5: "CORE", 6: "CRUNCH", 7: "CURL", 8: "DEADLIFT",
    9: "FLYE", 10: "HIP_RAISE", 11: "HIP_STABILITY", 12: "HIP_SWING",
    13: "HYPEREXTENSION", 14: "LATERAL_RAISE", 15: "LEG_CURL",
    16: "LEG_RAISE", 17: "LUNGE", 18: "OLYMPIC_LIFT", 19: "PLANK",
    20: "PLYO", 21: "PULL_UP", 22: "PUSH_UP", 23: "ROW",
    24: "SHOULDER_PRESS", 25: "SHOULDER_STABILITY", 26: "SHRUG",
    27: "SIT_UP", 28: "SQUAT", 29: "TOTAL_BODY",
    30: "TRICEPS_EXTENSION", 31: "WARM_UP", 32: "RUN",
    65534: "UNKNOWN",
}

FALLBACK_CATEGORY = "TOTAL_BODY"

# Confirmed live (Phase G, 2026-08-01): a real, high probability value is
# what actually unlocks rendering — null/low values silently fail on at
# least the mobile app. We're not guessing Hevy's confidence, we're
# asserting our own: the user explicitly logged this exercise in Hevy, so
# a high fixed value is honest, not a fudge.
CONFIDENT_PROBABILITY = 95.0


@dataclass(frozen=True)
class ExerciseIdentity:
    category: str
    name: str | None  # None means "send category only" — either no subcategory data, or resolution failed
    probability: float | None  # None only for the FALLBACK_CATEGORY case — see resolve()


def _resolve_subcategory_name(cat_id: int, sub_id: int) -> str | None:
    """Mirrors hevy2garmin's _exercise_to_string() exactly: dynamically look
    up the (category, subcategory) pair against fit_tool's real enum
    classes, e.g. BENCH_PRESS (0) -> BenchPressExerciseName -> .name.
    Returns None if the pair can't be resolved (unknown category, sub_id
    out of range for that category's enum, etc.) rather than guessing."""
    try:
        import fit_tool.profile.profile_type as pt
        from fit_tool.profile.profile_type import ExerciseCategory

        cat_name = ExerciseCategory(cat_id).name
        sub_enum_cls = getattr(pt, cat_name.title().replace("_", "") + "ExerciseName", None)
        if sub_enum_cls is not None:
            return sub_enum_cls(sub_id).name
    except (ValueError, AttributeError, ImportError) as e:
        logger.debug("Could not resolve subcategory (%s, %s): %s", cat_id, sub_id, e)
    return None


_CATEGORY_IDS_BY_NAME: dict[str, int] = {name: cat_id for cat_id, name in CATEGORY_NAMES.items()}


def _validate_category_name_pair(category: str, name: str) -> bool:
    """Confirms (category, name) is a genuine fit_tool combination. Used
    anywhere a name string didn't come from our own dynamic resolution
    (e.g. read back from a user's manual Garmin correction) — never trust
    one without this check, per the module docstring on hand-guessed names
    400ing the entire push."""
    cat_id = _CATEGORY_IDS_BY_NAME.get(category)
    if cat_id is None:
        return False
    try:
        import fit_tool.profile.profile_type as pt
        from fit_tool.profile.profile_type import ExerciseCategory

        cat_name = ExerciseCategory(cat_id).name
        sub_enum_cls = getattr(pt, cat_name.title().replace("_", "") + "ExerciseName", None)
        if sub_enum_cls is None:
            return False
        return name in sub_enum_cls.__members__
    except (ValueError, AttributeError, ImportError):
        return False


def _is_valid_hex_id(template_id: str) -> bool:
    try:
        int(template_id, 16)
        return True
    except ValueError:
        return False


def validate_catalog() -> dict:
    """Report data-quality issues in the ported catalog without raising.
    Intended to run at startup (logged) and in CI (asserted on)."""
    issues = {
        "non_hex_template_ids": [],
        "unresolved_categories": [],
    }
    seen_unresolved_cats: set[int] = set()
    for template_id, (cat_id, _sub_id) in TEMPLATE_TO_FIT.items():
        if not _is_valid_hex_id(template_id):
            issues["non_hex_template_ids"].append(template_id)
        if cat_id not in CATEGORY_NAMES and cat_id not in seen_unresolved_cats:
            seen_unresolved_cats.add(cat_id)
            issues["unresolved_categories"].append(cat_id)
    return issues


class ExerciseMapper:
    """Resolves a Hevy exercise_template_id to a full Garmin exercise
    identity (category + specific name where resolvable), layering a user
    override file over the bundled base catalog, and recording misses for
    the dashboard's Mappings page."""

    def __init__(self, override_path: Path, db):
        self._override_path = override_path
        self._db = db
        self._overrides = self._load_overrides()

    def _load_overrides(self) -> dict[str, tuple[str, str | None]]:
        if not self._override_path.exists():
            return {}
        try:
            data = json.loads(self._override_path.read_text())
            return {k: (v["category"], v.get("name")) for k, v in data.get("mappings", {}).items()}
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.error("Failed to parse override mapping file %s: %s", self._override_path, e)
            return {}

    def known_template_ids(self) -> set[str]:
        """Template ids that don't need "learn from Garmin" — bundled catalog
        entries that actually resolve to a specific (category, name), plus
        user overrides that already carry a validated name. A category-only
        override (a quick dashboard "assign a category" fix, or an
        accidental Save click) is deliberately NOT counted as known here:
        it's a guess, not a resolution, and should stay eligible for "learn
        from Garmin" to upgrade into a real name rather than being silently
        skipped forever. The same applies to a *bundled* catalog entry that
        falls back to generic TOTAL_BODY/name=None (unresolved category id,
        or unresolved subcategory) — mere presence in TEMPLATE_TO_FIT is not
        a resolution either."""
        catalog_with_name = {tid for tid in TEMPLATE_TO_FIT if self.resolve(tid, "").name is not None}
        overrides_with_name = {tid for tid, (_category, name) in self._overrides.items() if name is not None}
        return catalog_with_name | overrides_with_name

    def resolve(self, template_id: str | None, exercise_title: str) -> ExerciseIdentity:
        # User override wins. Historically category-only (a safe,
        # always-valid fallback for exercises the bundled catalog can't
        # resolve at all); now may also carry a name, but only ever one
        # read back from Garmin's own confirmed state (see save_override) —
        # never hand-guessed.
        if template_id and template_id in self._overrides:
            category, name = self._overrides[template_id]
            return ExerciseIdentity(category, name, CONFIDENT_PROBABILITY)

        if template_id and template_id in TEMPLATE_TO_FIT:
            cat_id, sub_id = TEMPLATE_TO_FIT[template_id]
            category = CATEGORY_NAMES.get(cat_id, FALLBACK_CATEGORY)
            if category == FALLBACK_CATEGORY:
                # Category itself unresolved (e.g. ids 33/36/38/41/42/47/52
                # with no confirmed fit_tool name) — don't attempt a name.
                return ExerciseIdentity(FALLBACK_CATEGORY, None, CONFIDENT_PROBABILITY)
            name = _resolve_subcategory_name(cat_id, sub_id)
            return ExerciseIdentity(category, name, CONFIDENT_PROBABILITY)

        logger.warning(
            "Unmapped exercise template_id=%r title=%r — falling back to %s",
            template_id, exercise_title, FALLBACK_CATEGORY,
        )
        if self._db is not None:
            self._db.record_unmapped_exercise(template_id, exercise_title)
        return ExerciseIdentity(FALLBACK_CATEGORY, None, CONFIDENT_PROBABILITY)

    def save_override(self, template_id: str, category: str, note: str = "", name: str | None = None) -> None:
        """Write a user override, used by the dashboard's Mappings page and
        by the 'learn from Garmin' flow (src/learn.py). `name` is optional
        and, if given, must be a genuine fit_tool subcategory for `category`
        — validated here, never trusted as-is. This is safe to accept
        because callers only ever pass a name read back from Garmin's own
        confirmed state (a manual UI correction), never a hand-guessed one."""
        if category not in set(CATEGORY_NAMES.values()):
            raise ValueError(f"Unknown Garmin category string: {category!r}")
        if name is not None and not _validate_category_name_pair(category, name):
            raise ValueError(f"{name!r} is not a valid fit_tool subcategory for category {category!r}")

        self._override_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": 1, "mappings": {}}
        if self._override_path.exists():
            with contextlib.suppress(json.JSONDecodeError):
                data = json.loads(self._override_path.read_text())
        entry = {"category": category, "note": note}
        if name is not None:
            entry["name"] = name
        data.setdefault("mappings", {})[template_id] = entry
        self._override_path.write_text(json.dumps(data, indent=2, sort_keys=True))
        self._overrides[template_id] = (category, name)
        if self._db is not None:
            self._db.clear_unmapped_exercise(template_id)
