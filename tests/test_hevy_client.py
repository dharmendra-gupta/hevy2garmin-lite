from src.hevy_client import parse_webhook_payload


def test_workout_created_event_parsed():
    raw = {
        "event": "workout.created",
        "timestamp": "2026-08-01T12:30:00Z",
        "workout": {"id": "abc-123", "title": "Push Day"},
    }
    parsed = parse_webhook_payload(raw)
    assert parsed is not None
    assert parsed["workout_id"] == "abc-123"
    assert parsed["workout"]["title"] == "Push Day"


def test_non_created_event_ignored():
    # Hevy only fires workout.created — anything else must be ignored, not guessed at.
    raw = {"event": "workout.updated", "workout": {"id": "abc-123"}}
    assert parse_webhook_payload(raw) is None


def test_missing_event_field_ignored():
    assert parse_webhook_payload({"workout": {"id": "abc-123"}}) is None


def test_falls_back_to_top_level_workout_id_if_no_embedded_workout():
    raw = {"event": "workout.created", "workout_id": "xyz-789"}
    parsed = parse_webhook_payload(raw)
    assert parsed["workout_id"] == "xyz-789"
    assert parsed["workout"] is None
