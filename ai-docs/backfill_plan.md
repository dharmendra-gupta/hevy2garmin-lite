# Backfill historical Hevy workouts — plan (not yet implemented)

## Context

The app has only ever synced workouts going forward from whenever the webhook/polling started working — and the webhook was silently broken (wrong payload shape assumption) until 2026-08-02, so real historical workouts never got enriched. The user wants a way to catch up: walk Hevy's full workout history and sync anything that has a matching **watch-recorded** Garmin activity, using the exact same match-or-skip rule the app already enforces everywhere else — never fabricate a Garmin entry for a Hevy-only workout. That guarantee is not new code; it's `find_best_match()` returning `None`, exactly as today.

## Key findings from research

- `src/matcher.py:find_best_match()` (already reused unmodified) is what enforces "only workouts recorded in Garmin" — it filters candidates to `activityType.typeKey == "strength_training"` and returns `None` if nothing overlaps within `MATCH_TOLERANCE_MINUTES`. `sync_one_workout()` (`src/sync.py:60`) treats that as `no_watch_match` and moves on. Backfill needs zero new logic here — it just needs to feed these existing functions a wider candidate pool than the regular cycle does.
- **Hevy side**: no full workout-list function exists yet. `src/hevy_client.py` only wraps the events change-feed (`fetch_workout_events`, max 10/page) and single-workout fetch. Need a new `fetch_all_workouts()` against Hevy's `GET /v1/workouts?page=&pageSize=` — **unverified shape, same as the webhook payload was** before it turned out wrong. Must be confirmed live before trusting field names, same discipline as the `parse_webhook_payload` fix. (This live check has NOT been done yet.)
- **Garmin side**: `python-garminconnect==0.3.8`'s `Garmin.get_activities_by_date(startdate, enddate, activitytype=None, sortorder=None)` exists, auto-paginates internally, and is exactly what's needed to fetch activities across an arbitrary historical span (the regular cycle only ever fetches the 20 most recent via `get_activities(0, 20)`, which is useless for backfill). **`"strength_training"` is not a valid `activitytype` filter value in this library version** — fetch unfiltered and let `find_best_match()`'s existing internal `_is_strength_training()` check do the filtering, unchanged.
- `db.recent_sync_history(limit=500)` (used to build `already_claimed` today) caps out at 500 rows. Backfill processes much more history at once than a normal cycle — needs an unbounded variant so a real double-claim can't slip past the in-memory pre-filter (the DB's `garmin_activity_id UNIQUE` constraint would catch it, but `record_sync()`'s `ON CONFLICT(hevy_workout_id)` clause doesn't handle that second constraint, so a real conflict would raise, not fail gracefully).

## Design

**Scope for v1: full history, no date-range UI** (confirmed with user). The ask didn't call for partial ranges, and idempotency (existing `content_hash`/`sync_status` check in `sync_one_workout`) already makes reruns cheap and safe — so "backfill" is a single button, always processes everything, and naturally resumes-by-skipping if interrupted or re-run. Date-range filtering can be added later without restructuring anything here.

### 1. `src/hevy_client.py` — new `fetch_all_workouts()`

```python
def fetch_all_workouts(page_size: int = 10) -> list[dict]:
    """Fetch every workout on the account, paginating GET /v1/workouts.
    UNVERIFIED shape — confirm live before trusting field names (see
    parse_webhook_payload's history: the originally assumed webhook shape
    was also never real)."""
```
Mirrors `fetch_workout_events`'s exact while-loop pagination pattern (`src/hevy_client.py:75-91`) — same `_get`, same page/pageSize params, same "stop when a page comes back short" termination. Each returned dict is the same workout shape `fetch_workout()` already returns (`id`, `start_time`, `end_time`, `exercises`, etc.), since it's the same underlying resource.

**First real step of implementation must be a live smoke call** (`docker compose run --rm spike` style, or a one-off script) against the real Hevy account to confirm: the endpoint path, the response envelope key (`workouts`? `data`?), and that `page`/`pageSize` behave as assumed — exactly the same live-verification discipline used to catch the webhook payload bug.

### 2. `src/db.py` — unbounded claimed-activity lookup

Add `all_claimed_garmin_activity_ids() -> set[int]` (no `LIMIT`), used only by backfill. Leave `recent_sync_history(limit=500)` and its regular callers untouched.

### 3. `src/sync.py` — new `run_backfill()`

```python
def run_backfill(db: Database, mapper: ExerciseMapper, dry_run: bool = False,
                  progress_cb: Callable[[int, int], None] | None = None) -> SyncRunResult:
```
Closely mirrors `run_sync_cycle()` (`src/sync.py:154-227`), differing only in data sourcing:
1. `client = get_garmin_client()` — same `TokenLoadError` handling.
2. `workouts = hevy_client.fetch_all_workouts()` instead of `poll_events(since)`. No cursor read/write — backfill is intentionally separate from the polling cursor (`last_poll_timestamp` is untouched).
3. Compute `min(start_time)`/`max(start_time)` across the fetched workouts, pad by a day on each side, call `client.get_activities_by_date(startdate, enddate)` **once** (it auto-paginates) instead of `get_activities(0, GARMIN_ACTIVITY_FETCH_LIMIT)`.
4. `already_claimed = db.all_claimed_garmin_activity_ids()` instead of the 500-row-limited version.
5. Sort workouts newest-first (matches `get_activities_by_date`'s own default `sortorder`, and surfaces results for the workouts the user most likely cares about first).
6. Loop calling the existing `sync_one_workout()` unchanged, same `SetPushCircuitBreaker` pattern (reuse `max_consecutive_failures=3` as `run_sync_cycle` does). Call `progress_cb(processed_count, total_count)` after each, if given — powers the dashboard's live progress display without needing SSE/websockets.
7. Return the same `SyncRunResult` shape (`source_deleted` always 0 — backfill isn't event-based, there's nothing to delete-detect).

### 4. `src/main.py` — endpoint + background execution + status

- New in-memory state: `_backfill_thread: threading.Thread | None`, `_backfill_progress: dict` (e.g. `{"running": bool, "processed": int, "total": int}`), mirroring the existing `_login_thread`/`_run_login_in_background` pattern (`src/main.py:57, 254-256`) rather than `BackgroundTasks` (webhook's pattern) — this is a long-running, pollable job, not a fire-and-forget request-scoped task.
- `POST /v1/backfill`: no-ops with a clear status if already running (same shape as `trigger_login`'s no-op branches); otherwise starts the thread, which calls `run_backfill(db, mapper, dry_run=settings.DRY_RUN, progress_cb=...)`, updates `_backfill_progress` as it goes, and on completion logs the final summary via `_log_event("backfill", "failed" if result.errors else "success", summary)` — extending the existing 3-way source tagging (`webhook`/`polling`/`manual`) to a 4th, reusing `_log_event` unchanged.
- `GET /v1/backfill/status`: returns `_backfill_progress` for the dashboard to poll.

### 5. `src/templates/index.html` — new panel

- A "Backfill" glass-panel: one button ("Backfill All History"), a progress line ("Processing 42 / 210…" while running, sourced from `/v1/backfill/status`, polled the same way `refresh()` already runs every 15s), and it's a no-op-safe button (disabled while `_backfill_progress.running` is true).
- Add `"backfill"` as a 4th option in the existing `#logSourceFilter` dropdown and a matching `.source-backfill` CSS tag color, next to the existing webhook/polling/manual ones (`src/templates/index.html`'s `.source-*` rules).
- Results otherwise surface through the existing Logs panel and Sync History table unchanged — no new results view needed.

## Explicitly out of scope for v1

- Date-range filtering (mentioned above — clean extension point later, not needed now).
- Any code path that creates a Garmin activity from Hevy-only data — never happens; `find_best_match` returning `None` is final, matching the project's core non-negotiable design constraint.
- Rate-limiting/throttling beyond the existing per-call `tenacity` retry and the circuit breaker — flagged as a real risk to watch on the first live run (personal-scale account; likely fine, but a large history means many sequential Garmin pushes in one background job).

## Testing plan (TDD, per project guardrail)

- `tests/test_hevy_client.py`: `fetch_all_workouts()` pagination — patch `src.hevy_client._get` with `unittest.mock.patch` (this codebase's established mocking convention, e.g. `tests/test_push_auth_normalization.py`'s `MagicMock` usage — the `responses` library is an unused dev dependency, not the established pattern here), assert it pages until a short page, and asserts the aggregated result.
- `tests/test_db_idempotency.py`: `all_claimed_garmin_activity_ids()` — returns more than 500 rows' worth of ids when present (regression test for the exact limit this replaces).
- New `tests/test_sync.py` (sync.py currently has zero unit coverage — `run_sync_cycle`/`sync_one_workout` are thin orchestrators over already-tested pieces): test `run_backfill()`'s date-range computation and its wiring (mocked `db`/`client`/`mapper` via `MagicMock`, matching `test_push_auth_normalization.py`'s style) — specifically that it calls `get_activities_by_date` with the right computed span, respects `already_claimed`, and calls `progress_cb` correctly. Don't re-test `sync_one_workout`'s internals (already implicitly covered elsewhere) or Hevy/Garmin's real APIs.
- Live verification step (not pytest): confirm `GET /v1/workouts`'s real response shape against the real Hevy account before trusting it, same as the webhook fix. **Not done yet.**
- `docker compose run --rm test` / `docker compose run --rm lint` after implementation, same as every prior change this session.

## Verification (end-to-end, once implemented)

1. `docker compose up -d app`, click "Backfill All History" on the dashboard.
2. Confirm `/v1/backfill/status` progresses and completes.
3. Confirm Sync History shows newly-synced historical workouts, and any Hevy-only (no watch match) workouts do **not** appear as synced — they should be absent from Garmin-side effects entirely, consistent with `no_watch_match` handling elsewhere.
4. Re-click "Backfill All History" — confirm it's fully idempotent (everything reports `skipped_idempotent`, nothing re-pushed).
5. Check Logs with the new "Backfill" source filter shows the run.

## Status

**Planning only — nothing implemented yet.** Wait for explicit go-ahead before starting Phase 1 (Hevy client) or any other step above.
