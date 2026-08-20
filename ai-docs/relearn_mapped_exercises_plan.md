# Re-learn already-mapped exercises from Garmin (opt-in)

## Context

"Map from Garmin" (`POST /v1/mappings/learn-from-garmin/{hevy_workout_id}`, `src/learn.py:learn_mappings_from_garmin()`) only ever offers to learn a template_id that isn't already in `mapper.known_template_ids()` — i.e. one with no specific mapping yet (bundled-catalog fallback, or an unnamed override). That's deliberate: the earlier fixes this same day were all about exercises resolving to *nothing specific* getting permanently stuck.

Real report (2026-08-17, live account): "Lateral Dumbbell Raise" already had a *specific* bundled-catalog mapping — just the wrong one. Garmin rendered it as "Leaned Lateral Raise," the user fixed it by hand in Garmin's UI, then clicked "Map from Garmin" — nothing happened, because the template_id already counted as "known." A confidently-wrong specific name is stuck exactly the same way a wrong hand-guess would be, with no way back short of hand-editing `data/exercise_mappings.json`.

**Goal**: an opt-in way to re-check *already-mapped* exercises too, without changing default behavior or weakening any existing safety net.

## Design

**Key insight — no change needed to `learn.py`'s core logic.** `learn_mappings_from_garmin(hevy_exercises, garmin_exercise_sets, already_known_template_ids)` already takes the "known" set as a plain parameter. The caller (`main.py`'s route) already computes it via `mapper.known_template_ids()`. To include already-mapped exercises, the route just needs to pass a smaller (or empty) set instead — no signature change, no new logic path inside `learn.py`, all existing safety nets (`_validate_category_name_pair`, ACTIVE-count bail-out, per-group identity-consistency bail-out) apply unchanged.

### 1. `src/main.py` — `learn_from_garmin` route

New optional query param `include_mapped: bool = False`. When `True`:
```python
already_known = set() if include_mapped else mapper.known_template_ids()
```
Everything else in the route is unchanged.

Also added: when overwriting an identity that was already known (not just filling a blank), the route now logs the old → new change explicitly — e.g. `"correcting CUSTOM123: SQUAT/BARBELL_SQUAT -> SQUAT/BARBELL_BACK_SQUAT"`. Previously `save_override()` silently overwrote with no record of what changed.

### 2. `src/templates/index.html` — checkbox UI

A single checkbox in the "Recent Sync History" panel, not per-row — matches this project's existing simple-single-control pattern (e.g. the Logs source filter dropdown). `learnFromGarmin()` reads the checkbox and includes it as a query param on the POST.

## Safety / why this is low-risk

- Default (unchecked) behavior is byte-for-byte unchanged.
- Checked mode only *widens* which template_ids are considered — it can't bypass `_validate_category_name_pair`, the ACTIVE-count-must-match bail-out, or the per-group consistency check.
- Re-learning an exercise the user did *not* touch in Garmin's UI this cycle just re-saves the same `(category, name)` it already had — a harmless no-op, not a corruption risk.

## Explicitly out of scope for v1

- Per-row (per-exercise) granularity — a single workout-level checkbox is enough; the user re-runs "Map from Garmin" per workout already.
- Any change to `learn_mappings_from_garmin()`'s function signature or internal matching logic.
- Backfill/bulk re-learning across historical workouts not currently in the local sync history — the backfill feature (`ai-docs/backfill_plan.md`) is separate and not yet built. Until then, correcting an exercise from a workout with no local `synced_workouts` row requires either the dashboard's category-only Mappings page, manually editing `data/exercise_mappings.json`, or hitting the Garmin API directly.

## Testing plan

- No new tests needed for `src/learn.py` — its logic and existing test coverage (`tests/test_learn.py`) are untouched; `already_known_template_ids=set()` is already an exercised input.
- `src/main.py` routes aren't unit-tested per this project's established convention (verified via live curl instead — see `CLAUDE.md`). Verified live 2026-08-17: the new `include_mapped` query param is accepted (404 for a nonexistent workout, not a 422 validation error) for `true`/`false`/omitted. Full end-to-end re-learn verification (an already-mapped exercise manually corrected in Garmin, then re-learned with the checkbox on) requires a `synced_workouts` row for a real workout — not available in the local dev checkout's empty DB at implementation time; the underlying `learn_mappings_from_garmin()` call path was already verified live earlier the same day (see `ai-docs/implementation_plan.md`'s "Same-day follow-up" section) and is unchanged here, only the `already_known_template_ids` input varies.

## Status

**Implemented 2026-08-17.** `docker compose run --rm test` (95 passed) and `lint` clean.
