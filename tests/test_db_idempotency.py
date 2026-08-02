import sqlite3

import pytest

from src.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


def test_record_and_retrieve_sync(db):
    db.record_sync("hevy-1", 12345, "synced", "hash-a")
    row = db.get_sync_record("hevy-1")
    assert row["garmin_activity_id"] == 12345
    assert row["sync_status"] == "synced"


def test_rerun_after_crash_is_idempotent(db):
    # Simulates: row written, process dies before more work happens, rerun
    # must recognize the workout is already synced and not double-push.
    db.record_sync("hevy-1", 12345, "synced", "hash-a")
    row = db.get_sync_record("hevy-1")
    assert row is not None
    assert row["content_hash"] == "hash-a"
    # A second identical sync attempt should see the same row, unchanged status.
    row_again = db.get_sync_record("hevy-1")
    assert row_again["sync_status"] == "synced"


def test_updated_workout_content_hash_changes(db):
    db.record_sync("hevy-1", 12345, "synced", "hash-a")
    db.record_sync("hevy-1", 12345, "synced", "hash-b")  # workout edited, re-synced
    row = db.get_sync_record("hevy-1")
    assert row["content_hash"] == "hash-b"


def test_garmin_activity_id_unique_constraint(db):
    db.record_sync("hevy-1", 12345, "synced", "hash-a")
    with pytest.raises(sqlite3.IntegrityError):
        db.record_sync("hevy-2", 12345, "synced", "hash-b")  # same Garmin activity, different workout


def test_is_activity_claimed(db):
    assert db.is_activity_claimed(12345) is False
    db.record_sync("hevy-1", 12345, "synced", "hash-a")
    assert db.is_activity_claimed(12345) is True


def test_no_watch_match_does_not_claim_an_activity(db):
    db.record_sync("hevy-1", None, "no_watch_match", "hash-a")
    row = db.get_sync_record("hevy-1")
    assert row["garmin_activity_id"] is None
    assert row["sync_status"] == "no_watch_match"


def test_deleted_event_marks_source_deleted_without_touching_garmin_id(db):
    db.record_sync("hevy-1", 12345, "synced", "hash-a")
    db.mark_source_deleted("hevy-1")
    row = db.get_sync_record("hevy-1")
    assert row["sync_status"] == "source_deleted"
    assert row["garmin_activity_id"] == 12345  # untouched — we never delete from Garmin automatically


def test_unmapped_exercise_recorded_and_cleared(db):
    db.record_unmapped_exercise("TID1", "My Custom Exercise")
    rows = db.list_unmapped_exercises()
    assert any(r["template_id"] == "TID1" for r in rows)

    db.record_unmapped_exercise("TID1", "My Custom Exercise")  # seen again
    row = [r for r in db.list_unmapped_exercises() if r["template_id"] == "TID1"][0]
    assert row["occurrences"] == 2

    db.clear_unmapped_exercise("TID1")
    assert not any(r["template_id"] == "TID1" for r in db.list_unmapped_exercises())


def test_last_poll_timestamp_roundtrip(db):
    assert db.get_last_poll_timestamp() is None
    db.set_last_poll_timestamp("2026-08-01T10:00:00+00:00")
    assert db.get_last_poll_timestamp() == "2026-08-01T10:00:00+00:00"


def test_polling_enabled_uses_default_until_explicitly_set(db):
    assert db.get_polling_enabled(default=False) is False
    assert db.get_polling_enabled(default=True) is True  # no row yet — default wins either way


def test_polling_enabled_roundtrip_and_overrides_default(db):
    db.set_polling_enabled(True)
    assert db.get_polling_enabled(default=False) is True

    db.set_polling_enabled(False)
    assert db.get_polling_enabled(default=True) is False
