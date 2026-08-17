# Hevy2Garmin Lite — Implementation Plan

**Objective**: You always record strength workouts on both your watch and in Hevy. This project enriches the Garmin watch activity **in place** with Hevy's exercise names, sets, reps, and weights — without ever deleting the activity or touching the FIT file. Your watch already computed Training Effect, EPOC, recovery, VO2max impact, and calories; this project's only job is to attach the structured set data Hevy captured that the watch cannot.

> **Scope decision (deliberate, not a limitation)**: earlier drafts of this plan considered regenerating a replacement FIT file (`replace`) or writing a text description (`describe`). Both are **out of scope**. `replace` destroys the exact training metrics that are the entire reason to own a Garmin device — a self-defeating trade for this project's target user. `describe` is unstructured text, not real data. If you aren't wearing your watch, there is nothing worth syncing — a Hevy-only workout is simply not synced, and this is treated as a normal, logged outcome, not an error.

> **Update (Phase G, 2026-08-01)**: the plan originally assumed exercise *names* could never render on a watch-recorded activity (only category), based on `hevy2garmin`'s documented finding. Live testing disproved this — the missing ingredient was `probability`, not `name`. See Phase C and the Phase G Findings section below. This changed §3 Phase C/D materially; §5's "why we don't send names" reasoning is now historical, not current.

> **⚠️ TARGET AUDIENCE**: Pure self-hosting (Docker on a VPS or Raspberry Pi) — you deploy and manage your own infrastructure. If you want a managed, one-click solution instead, use the excellent [hevy2garmin](https://github.com/drkostas/hevy2garmin) project.

**Core Constraint 1: Zero Third-Party Proxies**. No Cloudflare Worker. Authentication shares the same token store `garmin-scale-sync` uses on the Oracle VM, and both services can self-heal it with the account's own credentials when it dies — mirroring `garmin-scale-sync`'s own login pattern exactly (§2, revised from the original strictly-read-only design).

**Core Constraint 2: Minimal Memory Footprint**. Target is a 1GB RAM Oracle Free Tier VM running multiple services.
- `python:3.12-slim` base image (bumped from 3.11 — `garminconnect==0.3.8` requires ≥3.12).
- FastAPI + uvicorn is fine — `garmin-scale-sync` already proves it runs comfortably on this box. No frontend framework, no build step.
- SQLite connections and HTTP sessions closed properly to prevent leaks during polling.

---

## 0. Prerequisites

- **Hevy Pro Subscription**: required for API access.
- **A running `garmin-scale-sync` instance** on the same host, sharing its token directory. Either service can refresh it (§2) — this one no longer strictly depends on the other staying authenticated.
- **You wear your watch for strength training.** This is a hard assumption of the design, not a preference — see the Objective above.

---

## 1. Architecture Overview

- **Language**: Python 3.10+ (container runs 3.12)
- **Environment**: Oracle Free Tier VM (reusing the existing environment)
- **Garmin Library**: `python-garminconnect==0.3.8`, `fit-tool>=0.9.15` (same versions `hevy2garmin` uses)
- **Process model**: A **single container** running FastAPI (dashboard) plus an in-process background scheduler (APScheduler, mirroring `garmin-scale-sync`). Not system cron — the dashboard's "Sync Now" button, the Hevy webhook's retry timers, and the (optional) periodic poll all share process state via the same scheduler.
- **No FIT file handling at all** — no download, no generation, no upload, no delete. Everything happens through one Garmin Connect API call per workout.
- **Trigger model**: Hevy's `workout.created` webhook is the fast primary trigger. Polling (`/v1/workouts/events`) is a slower reconciliation safety net — **off by default**, toggleable live from the dashboard — since webhooks never fire on edits/deletes and can be missed during a restart.
- **Data Flow**:
  1. Hevy calls our webhook (or, if polling is enabled, we poll) for new/changed workouts.
  2. Get a Garmin client — cached singleton; self-heals via a full credential login if the shared token store is dead (§2).
  3. Fetch recent Garmin activities; find the matching **Strength Training** activity (§3 Phase B).
  4. If matched: build a set-timeline + fully-identified (category + specific name) payload (§3 Phases D/E) and push it via `PUT /activity-service/activity/{id}/exerciseSets` (§3 Phase C).
  5. If not matched (webhook path only): retry at +5/+10/+15 min — the watch may not have synced to Garmin Connect yet. Still no match after retries → log it, mark it `no_watch_match`, and move on. Nothing is created.

---

## 2. Authentication Strategy — Self-Healing, Shared Token Store

**Revised from the original design.** The plan originally made this service a strictly read-only token consumer, on the theory that two processes writing the same store is a corruption risk. In practice we decided the resilience win is worth more: Hevy2Garmin Lite now holds its own copy of the account credentials (same account as `garmin-scale-sync`) and can perform a full credential re-login into the **same shared token store**, exactly mirroring `garmin-scale-sync`'s own pattern — so nobody has to babysit `garmin-scale-sync`'s dashboard just to keep this service alive.

This is safe in practice because `python-garminconnect`'s `login()` only does a full credential re-login when the **cached token is already rejected by the API** — routine syncs never touch credentials at all. Two services racing to re-auth at the exact same moment is a narrow, self-correcting edge case (whichever writes second just overwrites with an equally-valid fresh token), not an ongoing conflict.

1. **Singleton client, cached per-process** (`src/garmin_client.py`): a module-level `_client_instance` behind a lock, exactly like `garmin-scale-sync`'s `get_garmin_client()`. First call logs in; every later call in that process returns the cached client — it does **not** re-validate against Garmin on every call.
2. **Login attempt** (no scratch-copy — we're a legitimate co-writer now, so we point straight at the shared directory):
   ```python
   client = Garmin(
       email=settings.GARMIN_EMAIL or None,
       password=settings.GARMIN_PASSWORD or None,
       prompt_mfa=_prompt_mfa_callback,
   )
   client.login(settings.GARMIN_TOKEN_SOURCE_DIR)
   ```
   `python-garminconnect` tries the cached tokens first; only on rejection does it fall back to the full credential login above, persisting fresh tokens back to that same shared path.
3. **MFA handled via the dashboard, not a terminal** — there is no interactive terminal in a running container. `prompt_mfa` blocks on a `threading.Event` (60s timeout) exactly like `garmin-scale-sync`'s `mfa_state` pattern; a box appears on the dashboard when one is pending, and `POST /v1/auth/mfa` submits the code to unblock it.
4. **`POST /v1/auth/login`** mirrors `garmin-scale-sync`'s `initiate_login()` precisely: no-ops with `{"status": "success", "message": "Already authenticated."}` if a client is already cached (it does **not** force a fresh re-validation — neither does theirs), otherwise starts the background login thread. `GET /v1/status` only ever reads cached state (`auth_status()`) — it never triggers a login itself, so checking status can't block an HTTP request for up to 60s waiting on MFA.
5. **Mid-sync failure detection and recovery** — the trickier case, and where two real bugs surfaced during implementation (worth preserving here so they don't get reintroduced):
   - The library's low-level `_run_request()` refreshes proactively near expiry, and on a 401 refreshes once and retries once before raising. Calls through the **high-level, decorated** `Garmin.connectapi()` (e.g. `get_activities()`) correctly surface a dead session as `GarminConnectAuthenticationError`. But the actual `exerciseSets` push has to bypass that wrapper — `connectapi()` is GET-only, so the PUT goes through the **low-level** `client.client.put(...)` directly, which raised the raw, untyped `GarminConnectConnectionError("API Error 401...")` instead. `src/push.py` now normalizes this at that exact boundary (`_reraise_401_as_auth_error`) so every caller sees a consistent exception type regardless of which layer raised it.
   - **The bigger bug**: `push_exercise_sets`/`get_existing_exercise_sets` are wrapped in `tenacity`'s `@retry(stop=stop_after_attempt(3))` **without `reraise=True`**. Without it, tenacity wraps the final exception in its own `tenacity.RetryError` after exhausting attempts — so even a correctly-typed `GarminConnectAuthenticationError` never reached any `except GarminConnectAuthenticationError` block anywhere in the codebase. `reset_garmin_client()` was dead code from the day it was written. Fixed by adding `reraise=True` to both decorators. **Any new `@retry`-wrapped call whose exception type callers branch on must have `reraise=True`, or the same class of bug reappears silently.**
   - On a confirmed `GarminConnectAuthenticationError` anywhere in the sync path, call `reset_garmin_client()` (clears `_client_instance`) so the **next** sync cycle logs in fresh instead of reusing a permanently poisoned client.
6. **If credential login itself fails** (wrong password, account locked, MFA timeout, rate-limited) — log it, fire a notification (§7), and **skip the cycle**. There's no further fallback; a human needs to fix the credentials or check Garmin's account status.

---

## 3. Core Logic & Execution

### Phase A: Learning About New Workouts — Webhook (primary) + Polling (safety net)

**Webhook — the fast path.** Hevy supports `POST /v1/webhook-subscription` (`url`, `auth_token`), firing to our `POST /v1/webhooks/hevy` on every new workout, with a 5-second response budget. **Confirmed live 2026-08-02** (captured via a temp webhook receiver on a real account): the real body is `{"workoutId": "<uuid>"}` — no `event` field, no nested `workout` object. The originally assumed `{"event": "workout.created", "workout": {...}}` envelope (corroborated only across third-party integrations, never Hevy's own docs) was wrong, and the failure was silent: `parse_webhook_payload` returned `None` for every real call, the handler responded `{"status":"ignored"}` with `200`, and delivery/auth/registration all looked completely healthy the whole time — no webhook had ever actually synced a single workout before this was caught and fixed.
- Verify the `Authorization` header against `HEVY_WEBHOOK_AUTH_TOKEN` (our own generated secret, given to Hevy at registration).
- Respond `200` immediately; do the actual sync via FastAPI `BackgroundTasks` so Hevy's 5s budget is never at risk.
- **Retry with backoff if no watch match yet** (`WEBHOOK_RETRY_DELAYS_MINUTES`, default `5,10,15`): the Hevy workout can close before the watch has synced to Garmin Connect over Bluetooth. Each retry is a one-off `scheduler.add_job(..., "date", run_date=...)`. Only re-attempted on `no_watch_match` — not on `failed`, which already went through its own internal retry/backoff and should surface via the circuit breaker rather than retry silently forever.
- **Webhook only fires on `workout.created`** — never on edits or deletes. Registration is idempotent, done at startup if `PUBLIC_BASE_URL` + `HEVY_WEBHOOK_AUTH_TOKEN` + `HEVY_API_KEY` are all set; logged and skipped (not fatal) otherwise.

**Polling — the reconciliation safety net, default OFF.** `GET /v1/workouts/events?since=<last_poll_timestamp>` (max `pageSize` 10) — a change feed, far cheaper than paginating the full workout list.
- Toggleable live from the dashboard (`GET`/`POST /v1/settings/polling`) without a container restart — the scheduler job is added/removed dynamically, not just gated internally, so a disabled poll doesn't even wake up.
- Catches what the webhook structurally cannot: *updated* events (re-push against the already-linked `garmin_activity_id`, guarded against edit loops by a content hash of the Hevy payload), *deleted* events (**never** touch Garmin automatically — log it, mark the row `source_deleted`; undoing a push is too destructive to do unattended), and any webhook delivery Hevy failed to make (e.g. during a container restart).
- The dashboard's manual **"Sync Now"** button runs this same reconciliation cycle on demand, independent of whether the periodic timer is enabled — it's the answer to "I edited/deleted a workout and don't want to wait."

**State Management** (shared by both paths): SQLite table keyed on `hevy_workout_id`, storing `garmin_activity_id` (**`UNIQUE`** — one Garmin activity can only ever be claimed by one Hevy workout), `sync_status` (`synced` / `no_watch_match` / `failed` / `source_deleted`), `content_hash`, `synced_at`. Every sync checks this table first, making reruns idempotent independent of the polling cursor. Also backs the dashboard's sync history.

### Phase B: Activity Matching
- **Timezone normalization**: compare only UTC-aware timestamps. Hevy's `start_time`/`end_time` are ISO 8601 `Z`-suffixed (UTC) — parse directly. Garmin's `get_activities()` returns both `startTimeLocal` (naive local) and `startTimeGMT` (naive UTC); **always use `startTimeGMT`.**
- **Matching heuristic** (mirrors `hevy2garmin`'s proven rule — naive start-time-only matching mismatches back-to-back sessions): **≥70% temporal overlap** between the Hevy workout and the Garmin activity, **and** start drift within `MATCH_TOLERANCE_MINUTES` (default 15), **and** activity type "Strength Training".
- **Multiple candidates**: pick highest overlap, break ties by smallest drift; skip any activity already claimed in SQLite.
- **No match** → `sync_status = no_watch_match`, log and move on. No fallback creation of a new activity — see Objective.

### Phase C: Pushing Sets Into the Watch Activity (the entire update path)

`PUT /activity-service/activity/{id}/exerciseSets` — undocumented but confirmed working (`hevy2garmin` issue #111/#112, live-tested). No FIT generation, no upload, no deletion; the activity keeps every metric your watch computed.

**Revised (Phase G, 2026-08-01): we now send real exercise names, not `name: null`.** The original plan believed Garmin unconditionally ignores pushed exercise identity on watch-recorded activities (per `hevy2garmin` v0.5.8's finding). Live diagnostic testing disproved this — see **Phase G Findings** below for the full sequence. The actual, confirmed mechanism:

1. Resolve the specific subcategory `name` via the real `fit_tool` package, dynamically — exactly mirroring `hevy2garmin`'s own `_exercise_to_string()` (same package, same lookup pattern: `ExerciseCategory(cat_id).name` → `{CategoryName}ExerciseName` enum class → `.name` of the subcategory).
2. Always send a real, high `probability` value (`mapping.CONFIDENT_PROBABILITY = 95.0`) alongside it. **This was the actual missing ingredient** — `probability: null` (or 0.0) is what caused every earlier attempt to render as "Choose an Exercise" / "Unknown", not the presence or absence of `name`. We aren't guessing Hevy's confidence; we're asserting our own — the user explicitly logged this exercise in Hevy, so a high fixed value is honest, not a fudge.
3. Handle the atomic-rejection case: the `exerciseSets` PUT is all-or-nothing, so a single subcategory Garmin's server rejects (400 "Invalid Sub-Category" — `hevy2garmin` issues #199/#222) fails the *entire* batch. `push.py` retries **once** with every name stripped (category + probability kept) rather than hevy2garmin's full bisect-per-exercise machinery — a deliberate simpler tradeoff at this project's scale. Upgrade to bisect if this proves to lose more identifications than expected in practice.

**Resilience kept regardless**: a simple circuit breaker (disable pushing for the rest of the run after N consecutive failures of any kind — network, auth, unexpected 4xx) and a **pre-push backup of the activity's existing exerciseSets** (via GET) so a bad push can be diagnosed/restored if needed.

### Phase D: Exercise → (Category, Name) Mapping

**Revised (Phase G):** since we now send real names, we resolve the **full** `(category, subcategory)` pair, not category alone. We'd already ported `hevy2garmin`'s complete `exercise_template_id → (category_id, subcategory_id)` table (`src/template_map_source.py`) — the original plan just discarded the subcategory half on the (now-overturned) assumption it would never be usable.

**Resolution** (`src/mapping.py`), keyed on Hevy's **`exercise_template_id`** (never the exercise name — it breaks for non-English users and custom exercises):
1. **User override file** — `./data/exercise_mappings.json` in the mounted volume; **category-only by design** (a safe, always-valid fallback for exercises the bundled catalog can't resolve at all — not meant to hand-specify a subcategory name). Wins over the bundle.
2. **Bundled catalog** (`src/template_map_source.py`, ~350 entries ported verbatim from `hevy2garmin`) — look up `(cat_id, sub_id)`, resolve `category` via the static `CATEGORY_NAMES` table, then dynamically resolve the specific `name` via `fit_tool` (see Phase C). If the category itself has no confirmed `fit_tool` name (ids 33/36/38/39/41/42/47/52 — present in the ported data but with no entry in `CATEGORY_NAMES`), fall back to `TOTAL_BODY` with no name attempted.
3. **Fallback** — `TOTAL_BODY (29)`, no name, and **record the miss**.

**Surfacing misses**: every sync writes unmapped `exercise_template_id`s (with the human-readable name) to a SQLite `unmapped_exercises` table. The dashboard's Mappings page lists **only these** with a category picker that writes the override file, and clears the row the moment a mapping is saved.

**Validate the catalog at startup and in CI** against the FIT library's actually-implemented category enum (`hevy2garmin` issue #201 shows unimplemented categories silently degrade — don't let that happen invisibly here). Also flags three known non-hex template IDs in the ported upstream data (`4288G454`, `9373FSD1`, `32HKJ34K`) rather than silently accepting them.

### Phase E: Set Timeline Synthesis

Even without FIT files, the `exerciseSets` payload still requires **per-set `startTime`/`duration`** positioned within the real activity window — confirmed directly from `hevy2garmin`'s own `build_exercise_sets_payload()`, which runs the identical synthesis algorithm as their FIT generator. This step is not avoidable by skipping FIT.

**Why**: Hevy's API gives `start_time`/`end_time` for the **workout only** — individual sets carry weight/reps/type and sometimes `duration_seconds`, but never a timestamp. There is no real per-set time to use; one must be estimated.

**Algorithm** (ported from `hevy2garmin`):
1. Set duration: `duration_seconds` if present (cardio/isometric), else `working_set_seconds` (40) / `warmup_set_seconds` (25).
2. Rest after each set: `rest_between_sets_seconds` (75) within an exercise, `rest_between_exercises_seconds` (120) between exercises, `0` after the last.
3. Scale to reality: `scale = activity_duration_s / ideal_total`, **clamped to [0.3, 2.0]**.
4. Walk a cursor assigning each set a `startTime`/`duration`.

**Anchor to the watch activity's own start/end, not Hevy's.** The two can differ by up to `MATCH_TOLERANCE_MINUTES`; anchoring to the wrong clock silently skews or drops HR association (`hevy2garmin` v0.6.3 regression). The activity is the ground truth; Hevy is ordering information only.

**Clamp scale-saturation overflow**: when `scale` hits either bound, explicitly clamp the final set's end to the activity's real end and assert no set ever carries a timestamp past it.

Expose `working_set_seconds`, `warmup_set_seconds`, `rest_between_sets_seconds`, `rest_between_exercises_seconds` as config.

### Phase F: Metrics — What We Touch and What We Don't

| Metric | Source | Status |
|---|---|---|
| Sets / reps / weights | Hevy | ✅ pushed, exact |
| Exercise category | Our mapping | ✅ pushed, correct |
| Exercise name (e.g. "Bench Press (Barbell)") | Our mapping (`fit_tool`) | ✅ **pushed and renders correctly** — reversed from the original plan; see Phase G Findings |
| Heart rate, calories, Training Effect, EPOC, recovery, VO2max impact | Watch, already on the activity | ✅ **untouched** — this is the entire point of `in_place` |

We compute **nothing** metric-wise. No calorie formula, no FIT parsing. The watch already did that work correctly; our only contribution is the structured set data it can't capture on its own. Unlike the original plan's assumption, that contribution now includes full exercise identification, not just numeric sets.

### Phase G: Spike — Live Validation (complete)

**Status: fully validated live, on a genuinely watch-recorded activity, across multiple rounds of diagnostic testing.** `scripts/spike_push_test.py` (`docker compose run --rm spike`) is the canonical validator; the findings below came from a follow-up diagnostic (`scripts/diag_field_combinations.py`, deleted after use — see §4).

**Findings, in the order discovered:**

1. **First attempt** (`category: BENCH_PRESS`/`ROW`, `name: null`, `probability: null`) → rendered as **"Choose an Exercise"** (web) / **"Unknown"** (mobile) on a confirmed genuinely watch-recorded activity (created live on-watch, only edited for time/duration afterward — doesn't affect the `manufacturer` field). This matched the original plan's expectation and looked like confirmation of `hevy2garmin`'s "names are ignored" finding.
2. **Category-only + real probability** (`category: SQUAT`, `probability: 98.5`; `category: DEADLIFT`, `probability: 12.3`; both `name: null`) → **Squat rendered correctly on both web and mobile. Deadlift rendered correctly on web but showed "Unknown" on mobile.** This isolated `probability` — not `name` — as the field that actually gates rendering, and suggested mobile applies a confidence threshold somewhere between 12.3 and 98.5 that web doesn't (or has a lower one).
3. **Real name + no probability, batched with a bad guess** (`BARBELL_BENCH_PRESS` + `BARBELL_SQUAT`, both `probability: null`) → **entire batch rejected, 400 "Invalid Sub-Category."** `BARBELL_SQUAT` was later confirmed *wrong* (`fit_tool`'s real value for that subcategory id is `BARBELL_BACK_SQUAT`) — so this result was inconclusive as to whether a *correct* name would have been rejected too.
4. **Confirmed-correct names + real probability** (`category: BENCH_PRESS, name: BARBELL_BENCH_PRESS, probability: 95.0`; `category: DEADLIFT, name: BARBELL_DEADLIFT, probability: 95.0` — both values verified against `bikemap/fit_tool`'s actual source before sending) → **push succeeded with no rejection, and both exercises rendered with their specific names correctly on both web and mobile**, muscle body-map included.

**Conclusion**: `probability` is the field that unlocks rendering (not `name`'s presence/absence, which was the original plan's entire premise); a real, correctly-resolved `name` renders the specific exercise once `probability` is also present; mobile likely has a probability threshold that web doesn't share, so always send a high fixed value. The "Invalid Sub-Category" rejection class is real and must be handled (Phase C's strip-and-retry), but is triggered by genuinely wrong subcategory values — not, as originally feared, an inherent property of sending names at all to a watch-recorded activity.

---

## 3a. "Learn from Garmin" — exact names for custom exercises (added 2026-08-01)

**Problem**: a Hevy exercise the user created themselves has no `exercise_template_id` match in the ported catalog. It always falls back to `TOTAL_BODY`/no name, and the existing fix (dashboard override) is deliberately category-only — hand-guessing a subcategory name risks the atomic-rejection hazard above.

**Idea**: instead of guessing, read the name back from Garmin. If the user manually corrects the exercise via Garmin Connect's own "Choose an Exercise" UI, that value is one Garmin's own backend already considers valid — no guess required.

**Spike (2026-08-01, activity `23810842954`, same go/no-go discipline as Phase G)**: pushed a Bench Press set with `name=None`, `probability=95.0`. User manually corrected it to "Barbell Bench Press" in Garmin's UI. `GET exerciseSets` read back:
```json
{"category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS", "probability": 100.0}
```
`name` round-trips as a genuine `fit_tool` enum string — **feasibility confirmed, full build approved.** One wrinkle found in the same test: `wktStepIndex`/`messageIndex` both came back `null` on the corrected entry (Garmin's manual-edit path doesn't preserve them), so matching a corrected Garmin set back to the Hevy exercise it belongs to can't use those fields. `startTime` did survive intact, so `src/learn.py` matches on that instead — see `ai-docs/architecture.md`'s "Learning exact names" section and `ai-docs/RAG.md` for the exact mechanism and the two-different-`startTimeGMT`-formats gotcha this uncovered (`get_activities()` vs `get_activity()`).

**Built**: `src/learn.py` (`learn_mappings_from_garmin`), `mapping.py`'s override storage extended to optionally carry a validated `name`, `POST /v1/mappings/learn-from-garmin/{hevy_workout_id}` endpoint, a "Re-check Garmin" button per synced row in the dashboard's Sync History table. 13 new tests (`test_learn.py` + additions to `test_mapping.py`/`test_matcher.py`), full suite green (65/65).

**Not yet done**: a full live end-to-end pass through the *actual* sync pipeline (custom exercise logged in Hevy → real `sync_one_workout()` → manually corrected in Garmin → "Re-check Garmin" clicked → next sync of that same exercise renders correctly unattended). The Phase 0 spike validated the feasibility and the read-back mechanism directly, but not through a `synced_workouts` record with a real `hevy_workout_id`/`garmin_activity_id` pair, since the disposable test activity was pushed to directly via a spike script rather than the real sync path.

---

## 4. Step-by-Step Implementation

1. **Spike first (Phase G)** — go/no-go gate before any further work. *(Complete — see Phase G Findings.)*
2. **Scaffold**: Docker project, token copy-then-load helper (§2).
3. **Port Hevy client + SQLite state** (Phase A), including updated/deleted event handling.
4. **Build the category+name catalog** (Phase D) — port `hevy2garmin`'s `template_map.py` in full (category *and* subcategory), resolve names dynamically via `fit_tool`; validate against the FIT enum in CI.
5. **Implement matching** (Phase B) — 70% overlap + drift + type heuristic.
6. **Implement timeline synthesis** (Phase E) and the `exerciseSets` payload builder (Phase C) with name + probability + strip-and-retry, anchored to the watch activity.
7. **Wire the sync loop**: scheduler + Hevy fetcher + matcher + payload builder + push, checking SQLite first for idempotency.
8. **Dashboard**: status, manual sync, logs, mappings page (§7).
9. **Verify end-to-end** against a real workout: confirm the specific exercise name renders correctly and that Training Effect/EPOC/recovery/calories on the activity are **unchanged** from before the push. *(Done for the diagnostic activity; recommend one more pass against a real, non-disposable workout before fully trusting this in production.)*

---

## 5. Differences From `hevy2garmin`

1. **No third-party proxy** — self-healing shared-token auth instead of `hevy2garmin`'s Cloudflare Worker login proxy (§2).
2. **Single strategy, not three** — `hevy2garmin` supports `replace`/`merge`/`describe` because it serves users who may not always wear a watch. We only serve the always-wearing-a-watch case, so we don't need `replace`'s FIT generation pipeline or `describe`'s text-only fallback. (We *do* now use the same `merge`-equivalent mechanism they do — real names + `fit_tool` — see Phase C; this is no longer a point of difference, it's convergent.)
3. **Simpler rejection recovery**: a single strip-all-names-and-retry on "Invalid Sub-Category," versus `hevy2garmin`'s per-exercise bisect. Accepted tradeoff at this project's scale — revisit if it proves to lose more names than expected.
4. **Bulletproof Retry Logic**: exponential backoff (`tenacity`) on all network calls. *No proactive rate-limit budgeting is planned, by design* — a single user logs a handful of workouts a day against a 15-minute poll; retry-on-failure suffices.

---

## 6. Testing Strategy

`pytest`, all offline except the spike/diagnostic scripts.

### Unit Tests — 52 passing, run via `docker compose run --rm test`
- **Time-Matching** (`test_matcher.py`): midnight-spanning workouts, activities exactly on the `MATCH_TOLERANCE_MINUTES` edge, ≥70% overlap rule, back-to-back sessions that must **not** cross-match. Explicit assertion that `startTimeGMT` (not `startTimeLocal`) drives matching, including a regression test simulating what would happen if `startTimeLocal` were used by mistake.
- **Claim Exclusivity / Idempotency** (`test_db_idempotency.py`): two Hevy workouts can never claim the same `garmin_activity_id` (`UNIQUE` constraint); an already-synced workout is skipped on rerun including crash-after-push; deleted events mark `source_deleted` without touching the stored `garmin_activity_id`; polling-toggle persistence roundtrips and respects the passed-in default.
- **Mapping** (`test_mapping.py`): every resolved category is a real Garmin category string; confirmed exact `(category, name)` resolution for known exercises (Bench Press → `BENCH_PRESS`/`BARBELL_BENCH_PRESS`, Deadlift → `DEADLIFT`/`BARBELL_DEADLIFT`); categories with no confirmed `fit_tool` name fall back with no name attempted; catalog validation flags the three known non-hex template IDs rather than silently accepting them; override file wins over the bundle and is category-only by design; unmapped IDs fall back to `TOTAL_BODY` and are recorded, then clear from the unmapped list the moment a mapping is saved.
- **Timeline** (`test_timeline.py`): ordering and rest-gap placement, no rest after the final set, both scale-clamp saturation directions (90-min log vs 20-min activity; 2-min log vs 60-min activity) never let a set timestamp exceed the activity end, explicit `duration_seconds` used for cardio/isometric sets, empty-exercise and warmup-only inputs don't crash.
- **Webhook payload parsing** (`test_hevy_client.py`): only `workout.created` is accepted; other event types and malformed payloads are ignored, not guessed at.
- **Config** (`test_config.py`): `WEBHOOK_RETRY_DELAYS_MINUTES` comma-separated parsing, including whitespace and the empty-string (no-retries) case.
- **Auth exception normalization & rejection recovery** (`test_push_auth_normalization.py`): the mid-sync token-death regression from §2.5 — a 401 through the low-level PUT bypass is normalized to `GarminConnectAuthenticationError` (not left as `GarminConnectConnectionError`), non-401 connection errors pass through unchanged, the backup call propagates auth failures instead of swallowing them while still swallowing ordinary ones (404, network blips); the strip-and-retry fallback strips names but keeps category/probability, succeeds on a stripped retry, and gives up cleanly if the stripped retry also fails.
- **API Mocking**: `responses`/`pytest-mock` available for `garminconnect` and Hevy — CI needs no real credentials.

### Integration Tests & Dry-Run
- **Dry-Run Mode**: `DRY_RUN=true` fetches, matches, and builds the payload without calling the `exerciseSets` PUT (or the credential login itself — see `lifespan`'s dry-run guard).
- **Spike (manual, not CI)**: Phase G — complete, see findings above.
- **Not yet covered by automated tests**: the FastAPI routes themselves (webhook auth rejection, MFA submission endpoint, login endpoint state machine) are verified by live `curl` smoke tests against a running container, not `TestClient` — the module-level `db`/`mapper`/`scheduler` singletons in `main.py` make per-test isolation more work than it was worth for this pass. Worth revisiting if the route logic grows more complex.

---

## 7. Production & DevOps

### Configuration
All config via `.env` (no live editing, except the polling toggle — a narrow, deliberate carve-out stored in SQLite `sync_meta`, not a general config-editing feature).

| Key | Notes |
|---|---|
| `HEVY_API_KEY` | Requires Hevy Pro |
| `GARMIN_TOKEN_SOURCE_DIR` | Container-internal path, fixed at `/app/garmin_tokens_source` — do not set from `.env` |
| `GARMIN_TOKEN_HOST_DIR` | Host-side path for the docker-compose volume mount (distinct from the above — a real naming collision bug during development, see git history) |
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | Self-healing re-auth only (§2) — same account as `garmin-scale-sync` |
| `HEVY_WEBHOOK_AUTH_TOKEN` | Our generated secret; sent back by Hevy as the `Authorization` header |
| `PUBLIC_BASE_URL` | Required for webhook auto-registration; leave blank to run polling/manual-only |
| `WEBHOOK_RETRY_DELAYS_MINUTES` | Default `5,10,15` |
| `MATCH_TOLERANCE_MINUTES` | Default 15 |
| `SYNC_INTERVAL_MINUTES` | Default 15 (only relevant if polling is enabled) |
| `POLLING_ENABLED_DEFAULT` | Default `false` — dashboard toggle overrides this once set |
| `WORKING_SET_SECONDS` / `WARMUP_SET_SECONDS` | Phase E; defaults 40 / 25 |
| `REST_BETWEEN_SETS_SECONDS` / `REST_BETWEEN_EXERCISES_SECONDS` | Phase E; defaults 75 / 120 |
| `API_BASIC_AUTH_USERNAME` / `API_BASIC_AUTH_PASSWORD` | Dashboard Basic Auth |
| `PERSIST_LOGS`, `DRY_RUN` | Same semantics as `garmin-scale-sync` |
| `NTFY_*` / `TELEGRAM_*` | Alerting — not yet wired up (planned, see below) |

`CONFIDENT_PROBABILITY` (Phase C/D) is a fixed code constant (`95.0`, in `src/mapping.py`), not an env var — deliberately not exposed as config since it's an internal implementation detail of getting Garmin to render identifications, not a user-facing tuning knob.

### Lightweight Web Dashboard & Alerting
Mirrors `garmin-scale-sync`: FastAPI, single static HTML template, HTTP Basic Auth (`secrets.compare_digest`), in-memory log ring buffer. No frontend framework.
- **Status**: Garmin auth state (`unauthenticated` / `checking` / `mfa_required` / `authenticated`, non-blocking read), Hevy key configured, webhook registration state, polling on/off, last run summary.
- **Login to Garmin** button + **MFA code box** (appears automatically when one is pending) — drives §2's self-healing flow without waiting for a sync cycle to trigger it.
- **Polling toggle**: live on/off, calling `POST /v1/settings/polling`; persisted in SQLite so it survives restarts.
- **Mappings Page** (Phase D): only the exercises that failed to map, sourced from `unmapped_exercises`, each with a category picker.
- **Manual Sync**: "Sync Now" hitting the shared reconciliation cycle — works regardless of whether periodic polling is enabled.
- **Logs View**: `deque(maxlen=50)` + optional `PERSIST_LOGS` JSON file. The SQLite table backs durable per-workout history.
- **Push Notifications**: NTFY / Telegram on token failure and repeated push failures (circuit breaker tripped) — **not yet implemented**, config keys exist but nothing sends yet.

### Infrastructure & Deployment
- **Docker Compose services**: `app` (the real service), `spike` (Phase G, `tools` profile), `reauth` (optional manual credential bootstrap, `tools` profile — largely superseded by `app`'s own self-healing, kept for CLI-only recovery without the web server), `test` (`tools` profile). `app`'s token volume is **read-write** (not `:ro`) — it needs to write fresh tokens back on a successful self-heal.
- **Docker Healthchecks**: reports whether the last successful poll is within a threshold. **`HEALTHCHECK` alone does not restart containers** — pair `restart: unless-stopped` with a watchdog (e.g. `willfarrell/autoheal`) or an app-level exit-on-liveness-failure.
- **Data Retention**: prune sync history and logs older than 30 days.
- **Graceful Shutdown**: `SIGINT`/`SIGTERM` handlers finishing the in-flight API call and closing SQLite cleanly.
- **Multi-Arch CI/CD**: GitHub Actions building `amd64` + `arm64` to GHCR (Oracle Ampere + Raspberry Pi).
- **Core Constraint 2 verified live (2026-08-02)**: split `Dockerfile` into `base`/`dev` stages so the published image (258MB, down from 293MB) never carries `pytest`/`ruff`; measured idle `app` container RSS at ~42MB via `docker stats`; added `mem_limit`/`mem_reservation` to every `docker-compose.yml` service as an OOM safety rail (not because usage is close — see `ai-docs/RAG.md`'s "Docker image: size and memory" section for exact numbers). Also found and fixed a real deployment bug on the Oracle VM: a hand-written multi-service compose file never mounted `/app/garmin_tokens_source` for this app at all, so it silently self-healed into its own private, unshared token instead of the one `garmin-scale-sync` uses — now documented in `README.md`'s "Sharing tokens with garmin-scale-sync" section so it isn't rediscovered the hard way again.

### First real-account usage findings (2026-08-17) — 3 bugs found, all fixed

First real end-to-end use (webhook already fixed, timeline tuning already live) surfaced three separate, previously-untested defects:

1. **Garmin's activity title was never touched at all.** The app only ever pushed `exerciseSets`; nothing anywhere called `Garmin.set_activity_name()`. Garmin kept its own generic auto-label ("Strength") forever, regardless of Hevy's real workout title (e.g. "RTT · Lower A (Mon)"). Fixed by adding `push.py:push_activity_name()` (same 401-normalization treatment as `push_exercise_sets` — it also bypasses `connectapi()`) and calling it from `sync_one_workout()` right after a successful set push, best-effort (a rename failure doesn't fail the sync — the sets already landed).
2. **`timeline.py`'s global `scale` factor was applied to Hevy's *explicit* `duration_seconds` values too**, not just estimated sets — so a real 30s stretch/plank could render as 45s or more on Garmin, whenever the ideal total didn't happen to equal the real activity duration (always, in practice). The only existing test for this path (`test_explicit_duration_seconds_used_for_cardio_sets`) accidentally set `activity_duration_s` exactly equal to the explicit duration, so `scale` was trivially `1.0` and never caught it. Fixed: explicit durations are now excluded from the scale computation entirely and never multiplied by it — only estimated sets and rest gaps flex to fit the remaining time budget.
3. **`ExerciseMapper.known_template_ids()` treated bare presence in the bundled `TEMPLATE_TO_FIT` catalog as "known"**, even when that catalog entry actually resolves to generic `TOTAL_BODY`/`name=None` (an unresolved category id, or unresolved subcategory — ~18 of 428 entries). This is the exact same bug class fixed on 2026-08-02 for category-only *overrides*, just one layer up — the fix back then only patched the override side. Any exercise whose catalog entry falls back to Total Body (a real example hit live: "Terminal Knee Extension Stretch") was permanently invisible to "Map from Garmin," no matter how many times it was corrected in Garmin's UI. Fixed: `known_template_ids()` now checks the actual `resolve()` outcome for every catalog id, not just catalog presence.

Also added, as a side effect of investigating #1 (exercise *name*, not activity name, was the original — wrong — read of the bug report): `push_exercise_sets()` now returns whether it had to strip every exercise name in the batch (the pre-existing atomic Invalid-Sub-Category retry), and `sync_one_workout()` surfaces this as a new `sync_status` value, `synced_partial`, instead of a silently-identical `"synced"` — visible in the dashboard's Sync History (own color) and Logs, and re-enables "Map from Garmin" for those rows (previously gated to `sync_status === 'synced'` only).
