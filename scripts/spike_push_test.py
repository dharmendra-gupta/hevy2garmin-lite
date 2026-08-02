"""Phase G spike — the go/no-go gate for this entire project.

Validates two things against a REAL watch-recorded Strength Training
activity, before any of Phases C/D/E are trusted:

  1. Does a null-name + valid-category push render as the category's label
     (e.g. "Bench Press") in Garmin Connect, or does it show "Unknown" /
     "Choose an Exercise"?
  2. Does a category-only (no subcategory) payload avoid the "Invalid
     Sub-Category" 400 rejection class entirely, as we're assuming?

This performs a REAL, LIVE write against your Garmin account (it pushes 2
sets into your most recent Strength Training activity). It backs up the
activity's existing exerciseSets first and prints them so you can manually
restore if needed, and it will not proceed without explicit confirmation.

Run: docker compose run --rm spike
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta

from src.garmin_client import TokenLoadError, get_garmin_client
from src.matcher import parse_garmin_gmt
from src.push import get_existing_exercise_sets, push_exercise_sets

STRENGTH_TRAINING_TYPE_KEY = "strength_training"
MAX_AGE_DAYS = 7  # refuse to touch an activity older than this, as a sanity guard


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


def build_test_payload(activity_id: int, activity_start: datetime) -> dict:
    """Two sets, no subcategory, no name — the simplest possible probe."""
    set1_start = activity_start + timedelta(seconds=30)
    set2_start = activity_start + timedelta(seconds=120)
    fmt = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.0")  # noqa: E731

    return {
        "activityId": activity_id,
        "exerciseSets": [
            {
                "exercises": [{"category": "BENCH_PRESS", "name": None, "probability": None}],
                "duration": 45.0,
                "repetitionCount": 8,
                "weight": 60000.0,  # grams
                "setType": "ACTIVE",
                "startTime": fmt(set1_start),
                "wktStepIndex": 0,
                "messageIndex": 0,
            },
            {
                "exercises": [{"category": "ROW", "name": None, "probability": None}],
                "duration": 45.0,
                "repetitionCount": 10,
                "weight": 40000.0,
                "setType": "ACTIVE",
                "startTime": fmt(set2_start),
                "wktStepIndex": 1,
                "messageIndex": 1,
            },
        ],
    }


def main() -> int:
    print("=== Hevy2Garmin Lite — Phase G spike ===\n")

    try:
        client = get_garmin_client()
    except TokenLoadError as e:
        print(f"FAILED to load Garmin tokens: {e}")
        print("This blocks everything else in the project — fix token sharing before continuing.")
        return 1
    print("OK: Garmin tokens loaded credential-free.\n")

    activity = find_recent_strength_activity(client)
    if activity is None:
        print(f"No Strength Training activity found in the last {MAX_AGE_DAYS} days.")
        print("Record one on your watch, then re-run this spike.")
        return 1

    activity_id = activity["activityId"]
    activity_start = parse_garmin_gmt(activity["startTimeGMT"])
    print(f"Found activity {activity_id}: {activity.get('activityName')} at {activity_start.isoformat()}")
    print(f"Duration: {activity.get('duration')}s\n")

    print("Backing up existing exerciseSets before touching anything...")
    backup = get_existing_exercise_sets(client, activity_id)
    print(json.dumps(backup, indent=2)[:2000])
    print("^ SAVE THIS OUTPUT if you want to manually restore it later.\n")

    payload = build_test_payload(activity_id, activity_start)
    print("About to PUT this payload (null names, valid categories, no subcategory):")
    print(json.dumps(payload, indent=2))

    print(f"\nThis is a REAL write to activity {activity_id} on your live Garmin account.")
    confirm = input("Type 'yes' to proceed: ").strip().lower()
    if confirm != "yes":
        print("Aborted — no changes made.")
        return 0

    push_exercise_sets(client, activity_id, payload)
    print("\nPush complete. Now check Garmin Connect (app or web) on this activity and report:")
    print("  1. Do the two exercises show as 'Bench Press' and 'Row' (or generic category labels)?")
    print("  2. Or do they show as 'Unknown' / 'Choose an Exercise'?")
    print("  3. Did the push succeed cleanly, or did you get a 400 'Invalid Sub-Category' error above?")
    print(f"\nActivity URL: https://connect.garmin.com/modern/activity/{activity_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
