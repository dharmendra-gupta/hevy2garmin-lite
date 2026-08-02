from src.hevy_client import parse_webhook_payload


def test_workout_created_payload_parsed():
    # Confirmed live 2026-08-02 via a temp webhook receiver: Hevy's real
    # payload is just {"workoutId": "<uuid>"} — no "event" field, camelCase
    # key, no nested workout object. The originally assumed shape (an
    # "event": "workout.created" envelope with a nested "workout" object)
    # was never real; every actual webhook call was silently ignored.
    raw = {"workoutId": "2af57df7-afc1-4eb7-a4f4-ca2665645f59"}
    parsed = parse_webhook_payload(raw)
    assert parsed is not None
    assert parsed["workout_id"] == "2af57df7-afc1-4eb7-a4f4-ca2665645f59"


def test_missing_workout_id_ignored():
    assert parse_webhook_payload({}) is None


def test_empty_workout_id_ignored():
    assert parse_webhook_payload({"workoutId": ""}) is None
