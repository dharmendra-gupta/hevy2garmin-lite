import json

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


def test_unresolvable_category_falls_back_with_no_name(mapper):
    # Template IDs whose category has no confirmed fit_tool name (33/36/38/
    # 41/42/47/52) must not attempt a name resolution at all.
    unresolved_template_id = next(
        tid for tid, (cat_id, _sub) in TEMPLATE_TO_FIT.items() if cat_id not in CATEGORY_NAMES
    )
    identity = mapper.resolve(unresolved_template_id, "irrelevant")
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
