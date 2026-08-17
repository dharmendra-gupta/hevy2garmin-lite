"""Coverage for sync_one_workout()'s orchestration — previously untested.
Focuses on the two behaviors added alongside today's bug fixes: pushing the
Hevy workout title into Garmin's activity name, and distinguishing a full
"synced" from a "synced_partial" (names silently stripped by push.py's
Invalid-Sub-Category fallback) sync_status."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from garminconnect import GarminConnectAuthenticationError

from src.db import Database
from src.mapping import ExerciseMapper
from src.push import SetPushCircuitBreaker
from src.sync import sync_one_workout


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


@pytest.fixture
def mapper(tmp_path, db):
    return ExerciseMapper(tmp_path / "exercise_mappings.json", db)


def _hevy_workout(title="RTT · Lower A (Mon)"):
    return {
        "id": "w1",
        "title": title,
        "start_time": "2026-08-17T10:00:00Z",
        "end_time": "2026-08-17T10:45:00Z",
        "exercises": [],
    }


def _garmin_activity(activity_id=999):
    return {
        "activityId": activity_id,
        "activityType": {"typeKey": "strength_training"},
        "startTimeGMT": "2026-08-17 10:00:00",
        "duration": 2700,
    }


def test_sync_one_workout_pushes_hevy_title_as_activity_name(db, mapper):
    client = MagicMock()
    with (
        patch("src.sync.get_existing_exercise_sets", return_value=None),
        patch("src.sync.push_exercise_sets", return_value=False) as mock_push_sets,
        patch("src.sync.push_activity_name") as mock_push_name,
    ):
        status = sync_one_workout(
            db, mapper, client, "w1", _hevy_workout(), [_garmin_activity()], set(),
            SetPushCircuitBreaker(),
        )
    assert status == "synced"
    mock_push_sets.assert_called_once()
    mock_push_name.assert_called_once_with(client, 999, "RTT · Lower A (Mon)")


def test_sync_one_workout_skips_rename_when_workout_has_no_title(db, mapper):
    client = MagicMock()
    workout = _hevy_workout(title="")
    with (
        patch("src.sync.get_existing_exercise_sets", return_value=None),
        patch("src.sync.push_exercise_sets", return_value=False),
        patch("src.sync.push_activity_name") as mock_push_name,
    ):
        status = sync_one_workout(
            db, mapper, client, "w1", workout, [_garmin_activity()], set(),
            SetPushCircuitBreaker(),
        )
    assert status == "synced"
    mock_push_name.assert_not_called()


def test_sync_one_workout_reports_synced_partial_when_names_were_stripped(db, mapper):
    client = MagicMock()
    with (
        patch("src.sync.get_existing_exercise_sets", return_value=None),
        patch("src.sync.push_exercise_sets", return_value=True),  # stripped
        patch("src.sync.push_activity_name"),
    ):
        status = sync_one_workout(
            db, mapper, client, "w1", _hevy_workout(), [_garmin_activity()], set(),
            SetPushCircuitBreaker(),
        )
    assert status == "synced_partial"
    record = db.get_sync_record("w1")
    assert record["sync_status"] == "synced_partial"


def test_sync_one_workout_tolerates_rename_failure_as_best_effort(db, mapper):
    # A non-auth failure renaming the activity must not fail a sync whose
    # exercise-set push already succeeded — the sets are more valuable than
    # the title, same "best effort" precedent as the exerciseSets backup.
    client = MagicMock()
    with (
        patch("src.sync.get_existing_exercise_sets", return_value=None),
        patch("src.sync.push_exercise_sets", return_value=False),
        patch("src.sync.push_activity_name", side_effect=RuntimeError("network blip")),
    ):
        status = sync_one_workout(
            db, mapper, client, "w1", _hevy_workout(), [_garmin_activity()], set(),
            SetPushCircuitBreaker(),
        )
    assert status == "synced"


def test_sync_one_workout_fails_if_rename_hits_a_dead_session(db, mapper):
    # An auth failure during rename must propagate like any other auth
    # failure in this function — the caller needs to reset the cached client.
    client = MagicMock()
    with (
        patch("src.sync.get_existing_exercise_sets", return_value=None),
        patch("src.sync.push_exercise_sets", return_value=False),
        patch("src.sync.push_activity_name", side_effect=GarminConnectAuthenticationError("dead session")),
        patch("src.sync.reset_garmin_client") as mock_reset,
    ):
        status = sync_one_workout(
            db, mapper, client, "w1", _hevy_workout(), [_garmin_activity()], set(),
            SetPushCircuitBreaker(),
        )
    assert status == "failed"
    mock_reset.assert_called_once()


def test_sync_one_workout_dry_run_never_calls_rename(db, mapper):
    client = MagicMock()
    with patch("src.sync.push_activity_name") as mock_push_name:
        status = sync_one_workout(
            db, mapper, client, "w1", _hevy_workout(), [_garmin_activity()], set(),
            SetPushCircuitBreaker(), dry_run=True,
        )
    assert status == "synced"
    mock_push_name.assert_not_called()
