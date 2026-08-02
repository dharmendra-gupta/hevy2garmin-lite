"""Phase 0 spike for the "learn exact names from manual Garmin corrections"
feature (see the approved plan). Throwaway diagnostic script — not part of
the TDD-covered src/ tree, deleted once the feasibility question is answered.

Two subcommands:

  push  - push ONE set with category only (name=None, probability=95.0) into
          the most recent Strength Training activity, so there's something
          to manually correct in Garmin Connect's "Choose an Exercise" UI.
  read  - GET the activity's current exerciseSets and print the raw JSON so
          we can inspect whether a manual correction round-trips as a
          fit_tool-shaped name string.

Run via: docker compose run --rm --entrypoint python spike -m scripts.spike_learn_readback push
         docker compose run --rm --entrypoint python spike -m scripts.spike_learn_readback read
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta

from src.garmin_client import TokenLoadError, get_garmin_client
from src.matcher import parse_garmin_gmt
from src.push import get_existing_exercise_sets, push_exercise_sets

STRENGTH_TRAINING_TYPE_KEY = "strength_training"
MAX_AGE_DAYS = 7


def find_recent_strength_activity(client) -> dict | None:
    activities = client.get_activities(0, 20)
    now = datetime.now(UTC)
    for activity in activities:
        activity_type = activity.get("activityType") or {}
        if activity_type.get("typeKey") != STRENGTH_TRAINING_TYPE_KEY:
            continue
        start_gmt = activity.get("startTimeGMT")
        if not start_gmt:
            continue
        start = parse_garmin_gmt(start_gmt)
        if (now - start) > timedelta(days=MAX_AGE_DAYS):
            continue
        return activity
    return None


def cmd_push(client, activity: dict) -> int:
    activity_id = activity["activityId"]
    activity_start = parse_garmin_gmt(activity["startTimeGMT"])
    set_start = activity_start + timedelta(seconds=30)

    payload = {
        "activityId": activity_id,
        "exerciseSets": [{
            "exercises": [{"category": "BENCH_PRESS", "name": None, "probability": 95.0}],
            "duration": 45.0,
            "repetitionCount": 8,
            "weight": 60000.0,
            "setType": "ACTIVE",
            "startTime": set_start.strftime("%Y-%m-%dT%H:%M:%S.0"),
            "wktStepIndex": 0,
            "messageIndex": 0,
        }],
    }
    print(f"About to PUT this single-set payload to activity {activity_id}:")
    print(json.dumps(payload, indent=2))
    print(f"\nThis is a REAL write to activity {activity_id} on your live Garmin account.")
    confirm = input("Type 'yes' to proceed: ").strip().lower()
    if confirm != "yes":
        print("Aborted — no changes made.")
        return 0

    push_exercise_sets(client, activity_id, payload)
    print("\nPush complete. Now go to Garmin Connect (app or web), open this activity's Sets tab,")
    print("and manually pick a specific exercise for that set (e.g. Barbell Bench Press) via")
    print("the 'Choose an Exercise' UI. Once saved, run this script again with `read`.")
    print(f"\nActivity URL: https://connect.garmin.com/modern/activity/{activity_id}")
    return 0


def cmd_read(client, activity: dict) -> int:
    activity_id = activity["activityId"]
    result = get_existing_exercise_sets(client, activity_id)
    print(f"Raw exerciseSets GET response for activity {activity_id}:\n")
    print(json.dumps(result, indent=2))
    return 0


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("push", "read"):
        print("Usage: python -m scripts.spike_learn_readback [push|read]")
        return 1
    mode = sys.argv[1]

    try:
        client = get_garmin_client()
    except TokenLoadError as e:
        print(f"FAILED to load Garmin tokens: {e}")
        return 1
    print("OK: Garmin tokens loaded.\n")

    activity = find_recent_strength_activity(client)
    if activity is None:
        print(f"No Strength Training activity found in the last {MAX_AGE_DAYS} days.")
        return 1

    activity_id = activity["activityId"]
    print(f"Using activity {activity_id}: {activity.get('activityName')}\n")

    if mode == "push":
        return cmd_push(client, activity)
    return cmd_read(client, activity)


if __name__ == "__main__":
    sys.exit(main())
