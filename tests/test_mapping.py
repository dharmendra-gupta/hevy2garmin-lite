import json
from unittest.mock import patch

import pytest

from src.db import Database
from src.mapping import CATEGORY_NAMES, CONFIDENT_PROBABILITY, FALLBACK_CATEGORY, ExerciseMapper, validate_catalog
from src.template_map_source import TEMPLATE_TO_FIT


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


@pytest.fixture
def mapper(tmp_path, db):
    return ExerciseMapper(tmp_path / "exercise_mappings.json", db)


def test_catalog_validation_runs_without_raising():
    issues = validate_catalog()
    assert isinstance(issues["non_hex_template_ids"], list)
    assert isinstance(issues["unresolved_categories"], list)


def test_catalog_validation_flags_known_non_hex_ids():
    # These three are known upstream data quirks (see template_map_source.py docstring).
    issues = validate_catalog()
    assert "4288G454" in issues["non_hex_template_ids"]
    assert "9373FSD1" in issues["non_hex_template_ids"]
    assert "32HKJ34K" in issues["non_hex_template_ids"]


def test_every_resolved_category_is_a_known_garmin_name(mapper):
    # Every template ID must resolve to a category actually present in
    # CATEGORY_NAMES.values() (what the Garmin API accepts) — guards the
    # "silently degrades to an unvalidated category" failure class.
    valid_names = set(CATEGORY_NAMES.values())
    for template_id in TEMPLATE_TO_FIT:
        identity = mapper.resolve(template_id, "irrelevant")
        assert identity.category in valid_names


def test_known_bench_press_resolves_category_and_name(mapper):
    # Confirmed live (Phase G, 2026-08-01): both the category AND the
    # specific fit_tool-resolved name render correctly on watch-recorded
    # activities, provided probability is also set. This is the regression
    # test for that finding — reverses the project's original assumption.
    identity = mapper.resolve("79D0BB3A", "Bench Press (Barbell)")
    assert identity.category == "BENCH_PRESS"
    assert identity.name == "BARBELL_BENCH_PRESS"
    assert identity.probability == CONFIDENT_PROBABILITY


def test_known_deadlift_resolves_category_and_name(mapper):
    identity = mapper.resolve("C6272009", "Deadlift (Barbell)")
    assert identity.category == "DEADLIFT"
    assert identity.name == "BARBELL_DEADLIFT"


def test_category_names_covers_the_full_real_fit_tool_enum():
    # Regression: CATEGORY_NAMES used to be a hand-typed subset that stopped
    # at category id 32, silently missing an entire second tier (33-53:
    # BIKE, MOVE, BATTLE_ROPE, ELLIPTICAL, INDOOR_BIKE, INDOOR_ROW,
    # STAIR_STEPPER, BANDED_EXERCISES, RUN_INDOOR, etc.) that fit_tool's own
    # ExerciseCategory enum has always had real string names for. Confirmed
    # live 2026-08-17: a genuine Garmin-corrected exercise came back with
    # category="BANDED_EXERCISES" (id 37) and failed validation purely
    # because our table didn't contain it — not because Garmin lacks a name.
    from fit_tool.profile.profile_type import ExerciseCategory

    real = {member.value: member.name for member in ExerciseCategory}
    assert real == CATEGORY_NAMES


def test_unresolvable_category_falls_back_with_no_name(mapper):
    # A category id that genuinely doesn't exist even in fit_tool's own
    # enum (not merely missing from a hand-typed subset) must still fall
    # back safely rather than crash or attempt a name resolution.
    fake_template_id = "FAKE_CATEGORY_ID_TEST"
    with patch.dict(TEMPLATE_TO_FIT, {fake_template_id: (99999, 0)}):
        identity = mapper.resolve(fake_template_id, "irrelevant")
    assert identity.category == FALLBACK_CATEGORY
    assert identity.name is None


def test_unmapped_template_id_falls_back_and_is_recorded(mapper, db):
    identity = mapper.resolve("NOT_A_REAL_ID", "Some Custom Exercise")
    assert identity.category == FALLBACK_CATEGORY
    assert identity.name is None
    unmapped = db.list_unmapped_exercises()
    assert any(row["exercise_name"] == "Some Custom Exercise" for row in unmapped)


def test_override_file_wins_over_base_catalog(tmp_path, db):
    override_path = tmp_path / "exercise_mappings.json"
    override_path.write_text(json.dumps({
        "version": 1,
        "mappings": {"79D0BB3A": {"category": "TOTAL_BODY", "note": "user override test"}},
    }))
    mapper = ExerciseMapper(override_path, db)
    # Base catalog says BENCH_PRESS for this id; override says TOTAL_BODY.
    identity = mapper.resolve("79D0BB3A", "Bench Press (Barbell)")
    assert identity.category == "TOTAL_BODY"
    # Overrides are category-only by design — never specify a subcategory name.
    assert identity.name is None


def test_saving_mapping_clears_it_from_unmapped_list(mapper, db):
    mapper.resolve("NOT_A_REAL_ID", "Some Custom Exercise")
    assert any(r["exercise_name"] == "Some Custom Exercise" for r in db.list_unmapped_exercises())

    mapper.save_override("NOT_A_REAL_ID", "TOTAL_BODY")
    assert not any(
        r["template_id"] == "NOT_A_REAL_ID" for r in db.list_unmapped_exercises()
    )
    assert mapper.resolve("NOT_A_REAL_ID", "Some Custom Exercise").category == "TOTAL_BODY"


def test_save_override_rejects_unknown_category(mapper):
    with pytest.raises(ValueError):
        mapper.save_override("SOME_ID", "NOT_A_REAL_CATEGORY")


# --- Learned (category, name) overrides, sourced from reading back a user's
# manual Garmin correction (see the "learn from Garmin" feature plan) -------

def test_validate_category_name_pair_accepts_a_known_real_combination():
    from src.mapping import _validate_category_name_pair
    assert _validate_category_name_pair("BENCH_PRESS", "BARBELL_BENCH_PRESS") is True


def test_validate_category_name_pair_rejects_a_bogus_name():
    from src.mapping import _validate_category_name_pair
    assert _validate_category_name_pair("BENCH_PRESS", "NOT_A_REAL_SUBCATEGORY") is False


def test_validate_category_name_pair_accepts_a_previously_missing_category():
    # Live regression 2026-08-17: BANDED_EXERCISES (id 37) is real and valid
    # in fit_tool but was entirely absent from the old hand-typed table.
    from src.mapping import _validate_category_name_pair
    assert _validate_category_name_pair("BANDED_EXERCISES", "LEG_EXTENSION") is True


def test_validate_category_name_pair_rejects_unknown_category():
    from src.mapping import _validate_category_name_pair
    assert _validate_category_name_pair("NOT_A_REAL_CATEGORY", "BARBELL_BENCH_PRESS") is False


def test_save_override_accepts_a_validated_name_and_resolve_returns_it(mapper):
    mapper.save_override("CUSTOM_ID", "BENCH_PRESS", note="learned from Garmin", name="BARBELL_BENCH_PRESS")
    identity = mapper.resolve("CUSTOM_ID", "My Custom Press")
    assert identity.category == "BENCH_PRESS"
    assert identity.name == "BARBELL_BENCH_PRESS"
    assert identity.probability == CONFIDENT_PROBABILITY


def test_save_override_rejects_a_name_that_does_not_match_the_category(mapper):
    with pytest.raises(ValueError):
        mapper.save_override("CUSTOM_ID", "BENCH_PRESS", name="BARBELL_BACK_SQUAT")


def test_existing_category_only_overrides_still_resolve_with_no_name(mapper):
    # Backward compat: overrides saved before `name` support must keep working.
    mapper.save_override("OLD_ID", "TOTAL_BODY")
    identity = mapper.resolve("OLD_ID", "Some Old Override")
    assert identity.category == "TOTAL_BODY"
    assert identity.name is None


# --- known_template_ids() scoping for "learn from Garmin" -------------------

def test_bundled_catalog_ids_are_known(mapper):
    assert "79D0BB3A" in mapper.known_template_ids()


def test_category_only_override_is_not_known(mapper):
    # A category-only override (e.g. a quick dashboard "assign a category"
    # fix, or an accidental Save click) must stay eligible for "learn from
    # Garmin" to upgrade into a real name — it's a guess, not a resolution.
    mapper.save_override("CUSTOM_ID", "BENCH_PRESS")
    assert "CUSTOM_ID" not in mapper.known_template_ids()


def test_name_bearing_override_is_known(mapper):
    # A validated (category, name) pair, sourced from Garmin's own confirmed
    # state, genuinely doesn't need re-learning.
    mapper.save_override("CUSTOM_ID", "BENCH_PRESS", name="BARBELL_BENCH_PRESS")
    assert "CUSTOM_ID" in mapper.known_template_ids()


def test_bundled_catalog_id_that_falls_back_to_total_body_is_not_known(mapper):
    # A catalog entry whose category id doesn't exist even in fit_tool's own
    # enum resolves to generic TOTAL_BODY/name=None — that's a guess, not a
    # resolution, and must stay eligible for "learn from Garmin" just like a
    # hand-guessed category-only override. Before the fix, mere presence in
    # TEMPLATE_TO_FIT counted as "known" regardless of what it actually
    # resolved to, permanently blocking correction. (Since CATEGORY_NAMES
    # now covers fit_tool's complete enum — see test_category_names_covers_
    # the_full_real_fit_tool_enum — no *real* ported catalog entry hits this
    # path anymore, so a fake one is patched in to exercise it.)
    fake_template_id = "FAKE_UNRESOLVED_CATEGORY_TEST"
    with patch.dict(TEMPLATE_TO_FIT, {fake_template_id: (99999, 0)}):
        assert fake_template_id not in mapper.known_template_ids()


def test_known_template_ids_matches_actual_resolution_outcome(mapper):
    # Property check: a template_id counts as "known" if and only if
    # resolve() actually produces a specific (non-fallback, named) identity.
    known = mapper.known_template_ids()
    for template_id in TEMPLATE_TO_FIT:
        identity = mapper.resolve(template_id, "irrelevant")
        has_specific_identity = identity.category != FALLBACK_CATEGORY and identity.name is not None
        assert (template_id in known) == has_specific_identity, template_id
