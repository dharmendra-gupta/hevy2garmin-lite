# Architecture reference

Detailed module deep-dives and historical bug context, split out of `CLAUDE.md` to keep that file a fast-orientation summary. Read this when working on the sync pipeline, authentication, mapping, or timeline synthesis specifically. For *why* decisions were made (not just *what* they are), see `ai-docs/implementation_plan.md`.

---

## The sync pipeline (`src/sync.py` is the hub)

`sync_one_workout()` is the single core function — given an already-fetched Hevy workout and an already-fetched list of Garmin activities, it matches, builds the payload, and pushes. Three different callers share it:
- `run_sync_cycle()` — the polling reconciliation path (processes every Hevy event since the last cursor; handles *updated*/*deleted* events, which the webhook cannot)
- `sync_workout_by_id()` — the webhook path (always re-fetches the single workout fresh rather than trusting the webhook's embedded payload, whose exact shape isn't verified against a live response)
- The dashboard's manual "Sync Now" button (`main.py`) calls the same reconciliation cycle as polling, independent of whether periodic polling is enabled

**Trigger model**: Hevy's `workout.created` webhook (`POST /v1/webhooks/hevy` in `main.py`) is the fast primary trigger, with retries at `WEBHOOK_RETRY_DELAYS_MINUTES` (default 5/10/15 min) if no matching watch activity is found yet — the watch may not have synced to Garmin Connect over Bluetooth by the time Hevy's workout closes. Polling (`/v1/workouts/events`) is a slower reconciliation safety net, **off by default**, toggled live from the dashboard (`POST /v1/settings/polling`, persisted in SQLite `sync_meta` — the one deliberate exception to "no live config editing"). Its interval is also live-editable this way (added 2026-08-02, `polling_interval_minutes` in `sync_meta`) — `SYNC_INTERVAL_MINUTES` in `.env` is now only the fallback default before it's ever been set from the dashboard. The set-timeline tuning knobs (`WORKING_SET_SECONDS`, `WARMUP_SET_SECONDS`, `REST_BETWEEN_SETS_SECONDS`, `REST_BETWEEN_EXERCISES_SECONDS` — see Set timeline synthesis below) got the same treatment the same day, via `POST /v1/settings/timeline`. "No live config editing" is really "no live editing of anything Garmin-auth- or infra-related" at this point — these are all sync-behavior tuning knobs, which is exactly the category meant to be dashboard-adjustable.

---

## Why there's no FIT file anywhere in this codebase

Sets are pushed directly via the undocumented `PUT /activity-service/activity/{id}/exerciseSets` endpoint (`src/push.py`), not by generating/uploading a FIT file. This was a scoped-down design decision: earlier drafts considered regenerating a replacement FIT (`replace` strategy) or writing a text description, both rejected — `replace` destroys exactly the training metrics (Training Effect, EPOC, etc.) that are the reason to own a Garmin device in the first place. See `ai-docs/implementation_plan.md` §Objective and Phase C/F for the full reasoning.

**Revised finding (Phase G, 2026-08-01)**: the plan originally believed Garmin ignores pushed exercise *names* on activities it recorded itself, so this codebase only ever sent a `category`, never a `name`. Live diagnostic testing overturned this — the actual missing ingredient was `probability`, not `name`. With a real `probability` value and a correctly `fit_tool`-resolved subcategory `name`, the specific exercise renders correctly on both web and mobile. `src/mapping.py` now resolves the full `(category, name)` pair (using the real `fit_tool` package, same as `hevy2garmin`), not category alone — see the Exercise mapping section below and `ai-docs/implementation_plan.md`'s "Phase G Findings" for the full diagnostic sequence.

**The activity *title* is a separate thing from exercise names, and was never pushed at all until 2026-08-17.** All of the above is about each *exercise's* identity within `exerciseSets`. Garmin's activity-level title (the one line at the top of the activity, e.g. defaulting to generic "Strength") is a completely different field, set via `Garmin.set_activity_name(activity_id, title)` — a call this codebase never made until a real first-use report ("Hevy said 'RTT · Lower A (Mon)', Garmin just said 'Strength'") surfaced it as a gap, not a regression. `sync_one_workout()` now calls `push.py:push_activity_name()` with Hevy's workout-level `title` right after a successful `push_exercise_sets()`, best-effort (a rename failure doesn't fail the sync).

---

## Set timeline synthesis (`src/timeline.py`)

Hevy never gives per-set timestamps — only weight/reps/type and sometimes `duration_seconds` for the workout as a whole. `build_set_timeline()` estimates one (working/warmup set durations, inter-set/inter-exercise rest) and scales it to the real activity duration (clamped to `[0.3, 2.0]`), **anchored to the watch activity's own start/end, never Hevy's** — the two can differ by `MATCH_TOLERANCE_MINUTES`, and anchoring to the wrong clock silently skews the whole timeline.

**Explicit `duration_seconds` sets (a real timed stretch/plank/hold Hevy logged) are never scaled** — fixed 2026-08-17. The global `scale` factor exists to fit *estimated* set/rest durations into the real activity duration; before this fix it was also applied to explicit durations, so a genuine 30s Hevy value could render as 45s+ on Garmin whenever the ideal total didn't happen to exactly equal the real activity duration (in practice, almost always). `scale` is now computed only over the flexible (estimated-set + rest) portion of the budget; explicit durations pass through unscaled and are only clamped against overrunning the activity's real end, same as everything else.

The four estimation inputs (`working_set_seconds`, `warmup_set_seconds`, `rest_between_sets_seconds`, `rest_between_exercises_seconds`) are live-editable from the dashboard's Timeline Tuning panel (`GET`/`POST /v1/settings/timeline`, added 2026-08-02) — `sync.py:_timeline_config_from_settings(db)` reads the current `sync_meta` values fresh on every `sync_one_workout()` call, falling back to the `.env` `Settings` defaults only if never explicitly set. Only changes where sets get estimated to *fall within* the scaled timeline — the clamp to the real activity duration means a bad tuning value skews relative spacing, not overall accuracy.

---

## Exercise mapping (`src/mapping.py` + `src/template_map_source.py`)

`template_map_source.py` is a verbatim port of `hevy2garmin`'s `exercise_template_id → (category_id, subcategory_id)` table. Three template IDs in that upstream data contain non-hex characters (`4288G454`, `9373FSD1`, `32HKJ34K`) — kept as-is since that's what upstream actually ships, flagged by `validate_catalog()` rather than silently accepted.

`ExerciseMapper.resolve()` returns an `ExerciseIdentity(category, name, probability)`:
- **`category`** — static lookup via `CATEGORY_NAMES` (FIT SDK category id → Garmin API string, e.g. `0 → "BENCH_PRESS"`). Ids present in the ported data with no confirmed Garmin string (`33, 36, 38, 39, 41, 42, 47, 52`) fall back to `TOTAL_BODY`.
- **`name`** — resolved **dynamically at runtime** via the real `fit_tool` package (`pip install fit-tool`, same version `hevy2garmin` pins), mirroring their `_exercise_to_string()` exactly: `ExerciseCategory(cat_id).name` → look up `{CategoryName}ExerciseName` enum class → `.name` of the subcategory member. Returns `None` (send category only) if resolution fails for any reason — never guess a subcategory string by hand; a wrong one 400s the *entire* push (see below).
- **`probability`** — always `mapping.CONFIDENT_PROBABILITY` (`95.0`) when a category was resolved at all. This is the field that actually gates whether Garmin renders the identification — not `name`'s presence, which was the original, overturned assumption.

A user override file (`data/exercise_mappings.json`) layers over the bundled catalog and wins if present. Overrides are safe to hand-enter as **category-only** via the dashboard's Mappings page (`POST /v1/mappings`) — never a hand-specified subcategory name (hand-guessing subcategory strings is exactly what caused a live 400 rejection during development — see `ai-docs/implementation_plan.md` Phase G Findings #3). Every miss is recorded to a SQLite `unmapped_exercises` table that the dashboard surfaces for one-click category fixing.

**The atomic-rejection hazard**: `exerciseSets` PUT is all-or-nothing — one exercise with a subcategory Garmin's server rejects (400 "Invalid Sub-Category") fails the whole batch, with no per-exercise error to tell you which one. `push.py`'s `push_exercise_sets()` retries **once** with every name stripped (category + probability kept, which Garmin always accepts) rather than `hevy2garmin`'s full per-exercise bisect — a deliberate simpler tradeoff at this project's scale. **This used to be silent** — `push_exercise_sets()` now returns `bool` (True if it stripped), and `sync_one_workout()` surfaces that as `sync_status = "synced_partial"` instead of an indistinguishable `"synced"` (added 2026-08-17) — visible in the dashboard's Sync History and Logs, and still eligible for "Map from Garmin".

**`known_template_ids()` used to under-count what actually needs learning, in two layers — both fixed now.** First (2026-08-02): a category-only *override* counted as "known" even though it's a guess, not a resolution. Second (2026-08-17, same bug one layer up): a *bundled catalog* entry whose category id or subcategory can't be resolved by `fit_tool` also falls back to generic `TOTAL_BODY`/`name=None` — but mere presence in `TEMPLATE_TO_FIT` used to count it as "known" regardless, permanently hiding it from "Map from Garmin" no matter how many times it was corrected in Garmin's own UI (real example: "Terminal Knee Extension Stretch"). `known_template_ids()` now checks the actual `resolve()` outcome for every catalog id, not just catalog membership.

### Learning exact names for custom exercises (`src/learn.py`)

A category-only override still shows as a generic category label, never the specific exercise — no way to hand-enter an exact name safely (see above). Instead, `POST /v1/mappings/learn-from-garmin/{hevy_workout_id}` lets the user manually correct the exercise in Garmin Connect's own "Choose an Exercise" UI, then reads that correction back and saves it as a *validated* (category, name) override — safe because the name came from Garmin's own confirmed state, not a guess.

Live-tested 2026-08-01 (activity `23810842954`): a manual UI correction round-trips as a genuine `fit_tool` name string through `GET exerciseSets` — but `wktStepIndex`/`messageIndex` both come back `null` on the corrected entry, so `learn_mappings_from_garmin()` can't join a Garmin response entry back to its Hevy exercise by index. It instead reconstructs the same synthesized timeline `push.py` used originally (`build_set_timeline`) and joins on `startTime`, which does survive. It needs `activity_start`/`activity_duration_s` from `client.get_activity(activity_id)` — a **different** endpoint than the one used for matching (`client.get_activities()`), returning `startTimeGMT` in a different string format; see `ai-docs/RAG.md`'s Matching rule section for both formats and their parsers. `mapping.save_override()` now accepts an optional `name`, validated via `_validate_category_name_pair()` (same `fit_tool` cross-check `resolve()` uses, run in reverse) before it's trusted.

`src/learn.py` is a separate module from `mapping.py`/`push.py` specifically to avoid a circular import — `push.py` already imports from `mapping.py`, and `learn.py` needs both.

---

## Authentication (`src/garmin_client.py`) — read this before touching auth code

Self-healing, shared-token-store design, deliberately mirroring `garmin-scale-sync`'s own pattern (a separate project on the same host): a cached singleton `Garmin` client that performs a full credential re-login (using `GARMIN_EMAIL`/`GARMIN_PASSWORD`) directly into the shared token directory only when the cached token is rejected by the API — routine syncs never touch credentials. MFA is handled via a dashboard flow (`threading.Event` + `POST /v1/auth/mfa`), not a terminal prompt, since there's no interactive terminal in a running container.

**Two real bugs were found and fixed here, and the same class of bug can reappear if this pattern isn't followed for any new Garmin API call added later:**

1. `push_exercise_sets()` has to bypass the library's high-level `Garmin.connectapi()` (it's GET-only, and this needs PUT), calling the low-level `client.client.put(...)` directly instead. That path does **not** get the library's automatic 401 → `GarminConnectAuthenticationError` translation — it raises a raw `GarminConnectConnectionError("API Error 401...")`. `push.py`'s `_reraise_401_as_auth_error()` normalizes this at that exact boundary.
2. Both `push_exercise_sets()` and `get_existing_exercise_sets()` are wrapped in `tenacity`'s `@retry(...)`. **`reraise=True` is required** — without it, after exhausting retry attempts tenacity raises its own `tenacity.RetryError` wrapping the original exception, so any `except GarminConnectAuthenticationError` catch downstream never fires. This silently broke `reset_garmin_client()` (the mechanism that clears a poisoned cached client so the next sync cycle re-logs in fresh) for the entire time it existed, until a test written against the expected behavior caught it.

If you add a new `@retry`-decorated Garmin API call whose exception type callers branch on, it needs `reraise=True`, and if it can't go through the high-level `connectapi()`/`get_*()` wrappers, it needs the same 401-normalization treatment.
