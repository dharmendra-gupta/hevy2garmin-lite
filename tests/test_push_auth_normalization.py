"""Regression coverage for the mid-sync token-death bug: push_exercise_sets
bypasses the library's decorated, auto-translating connectapi() wrapper (it
has to, to do a PUT — see push.py's module docstring), so a dead session
there raises the raw GarminConnectConnectionError instead of
GarminConnectAuthenticationError. Callers only reset the cached client on
the latter, so without normalization a poisoned client stays cached and
every subsequent sync keeps failing until the process restarts."""

from unittest.mock import MagicMock

import pytest
from garminconnect import GarminConnectAuthenticationError, GarminConnectConnectionError

from src.push import _strip_all_names, get_existing_exercise_sets, push_exercise_sets


def _client_whose_put_raises(message: str) -> MagicMock:
    client = MagicMock()
    client.client.put.side_effect = GarminConnectConnectionError(message)
    return client


def test_push_raises_auth_error_not_connection_error_on_401():
    client = _client_whose_put_raises("API Error 401 - ")
    with pytest.raises(GarminConnectAuthenticationError):
        push_exercise_sets(client, activity_id=12345, payload={"activityId": 12345, "exerciseSets": []})


def test_push_reraises_connection_error_unchanged_on_non_401():
    client = _client_whose_put_raises("API Error 503 - Service Unavailable")
    with pytest.raises(GarminConnectConnectionError):
        push_exercise_sets(client, activity_id=12345, payload={"activityId": 12345, "exerciseSets": []})


def test_backup_propagates_auth_error_instead_of_swallowing_it():
    client = MagicMock()
    client.connectapi.side_effect = GarminConnectAuthenticationError("Authentication failed: 401")
    with pytest.raises(GarminConnectAuthenticationError):
        get_existing_exercise_sets(client, activity_id=12345)


def test_backup_still_swallows_ordinary_non_auth_failures():
    # Best-effort for everything else — a missing/404 backup must not abort the sync.
    client = MagicMock()
    client.connectapi.side_effect = GarminConnectConnectionError("API Error 404 - Not Found")
    result = get_existing_exercise_sets(client, activity_id=12345)
    assert result is None


# --- Invalid Sub-Category strip-and-retry (added after Phase G reversed the
# no-names decision — see push.py module docstring) -------------------------

def test_strip_all_names_keeps_category_and_probability():
    payload = {
        "activityId": 1,
        "exerciseSets": [
            {"exercises": [{"category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS", "probability": 95.0}]},
            {"exercises": []},  # a REST set — no exercises to touch
        ],
    }
    stripped = _strip_all_names(payload)
    assert stripped["exerciseSets"][0]["exercises"][0]["name"] is None
    assert stripped["exerciseSets"][0]["exercises"][0]["category"] == "BENCH_PRESS"
    assert stripped["exerciseSets"][0]["exercises"][0]["probability"] == 95.0
    assert stripped["exerciseSets"][1]["exercises"] == []


def test_push_retries_once_with_names_stripped_on_invalid_subcategory():
    client = MagicMock()
    # First call: atomic rejection. Second call (post-strip): succeeds.
    client.client.put.side_effect = [
        GarminConnectConnectionError("API Error 400 - Invalid Sub-Category Passed in the request"),
        None,
    ]
    payload = {
        "activityId": 1,
        "exerciseSets": [{"exercises": [{"category": "BENCH_PRESS", "name": "BOGUS_NAME", "probability": 95.0}]}],
    }
    push_exercise_sets(client, activity_id=1, payload=payload)  # must not raise

    assert client.client.put.call_count == 2
    retried_payload = client.client.put.call_args_list[1].kwargs["json"]
    assert retried_payload["exerciseSets"][0]["exercises"][0]["name"] is None
    assert retried_payload["exerciseSets"][0]["exercises"][0]["category"] == "BENCH_PRESS"


def test_push_gives_up_if_stripped_retry_also_fails():
    client = MagicMock()
    client.client.put.side_effect = GarminConnectConnectionError(
        "API Error 400 - Invalid Sub-Category Passed in the request"
    )
    payload = {
        "activityId": 1,
        "exerciseSets": [{"exercises": [{"category": "BENCH_PRESS", "name": "BOGUS_NAME", "probability": 95.0}]}],
    }
    with pytest.raises(GarminConnectConnectionError):
        push_exercise_sets(client, activity_id=1, payload=payload)
