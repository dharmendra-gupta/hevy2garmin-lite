# RAG.md — dense lookup reference

Not narrative. `grep` this file for a keyword and read only the matching block instead of opening source files — that's the point of it existing separately from `CLAUDE.md` (guardrails, auto-loaded) and `ai-docs/implementation_plan.md` (reasoning/history). If a fact here and a fact in source ever disagree, source wins — this file can drift; update it when you change the thing it describes.

---

## File → purpose (one line each)

| File | Purpose |
|---|---|
| `src/config.py` | `Settings` (pydantic-settings, reads `.env`) — every env var, all defaults |
| `src/garmin_client.py` | Self-healing singleton Garmin client + MFA dashboard flow |
| `src/hevy_client.py` | Hevy REST calls: poll events, fetch workout, webhook subscription CRUD, webhook payload parsing |
| `src/matcher.py` | Hevy workout ↔ Garmin activity matching (overlap + drift + type) |
| `src/mapping.py` | `exercise_template_id` → `ExerciseIdentity(category, name, probability)` resolution via `fit_tool`, override file (now category+name capable), unmapped tracking |
| `src/learn.py` | Matches a Garmin `exerciseSets` GET response back to Hevy exercises by `startTime`, extracts validated (category, name) corrections a user made manually in Garmin's UI |
| `src/template_map_source.py` | Raw ported data: `TEMPLATE_TO_FIT: dict[str, tuple[cat_id, sub_id]]`, ~350 entries |
| `src/timeline.py` | Synthesizes per-set `start_offset_s`/`duration_s` (Hevy gives no real timestamps) |
| `src/push.py` | Builds the `exerciseSets` PUT payload; does the actual push; 401-normalization + Invalid-Sub-Category strip-and-retry live here |
| `src/sync.py` | Orchestrator: `sync_one_workout()` is the shared core, 3 callers |
| `src/main.py` | FastAPI app, all HTTP routes, scheduler wiring, webhook receiver |
| `src/db.py` | SQLite schema + all queries (no ORM) |
| `src/templates/index.html` | The entire dashboard UI (single file, vanilla JS, no build step) |
| `scripts/spike_push_test.py` | Phase G live validation push (`docker compose run --rm spike`) |
| `scripts/spike_learn_readback.py` | Phase 0 spike for the "learn from Garmin" feature — push/read subcommands, throwaway once feature is fully E2E-verified |
| `scripts/trigger_garmin_reauth.py` | Standalone credential bootstrap, superseded by `app`'s own self-heal |
| `pyproject.toml` | `[tool.ruff]` config — line-length 125, `B008` ignored (FastAPI `Depends()` in defaults is intentional) |
| `.github/workflows/test.yml` | CI: `lint`/`test` jobs (both via `docker compose`) on push/PR to `main`, then a `notify` job that PR-comments on success |
| `.github/workflows/publish.yml` | CI: builds/pushes `ghcr.io/<owner>/<repo>` multi-arch on a successful `main` "Run Tests" run or a GitHub release — ported from `garmin-scale-sync`'s own workflow |

---

## Config keys (`src/config.py`, all `Settings` fields, exact defaults)

```
HEVY_API_KEY: str = ""
GARMIN_TOKEN_SOURCE_DIR: str = "/app/garmin_tokens_source"   # container-internal, fixed, not user-set
GARMIN_EMAIL: str = ""
GARMIN_PASSWORD: str = ""
HEVY_WEBHOOK_AUTH_TOKEN: str = ""
PUBLIC_BASE_URL: str = ""
WEBHOOK_RETRY_DELAYS_MINUTES: str = "5,10,15"                # comma string; .webhook_retry_delays_minutes property -> list[int]
MATCH_TOLERANCE_MINUTES: int = 15
SYNC_INTERVAL_MINUTES: int = 15
POLLING_ENABLED_DEFAULT: bool = False
WORKING_SET_SECONDS: int = 40
WARMUP_SET_SECONDS: int = 25
REST_BETWEEN_SETS_SECONDS: int = 75
REST_BETWEEN_EXERCISES_SECONDS: int = 120
PORT: int = 8000
API_BASIC_AUTH_USERNAME: str = "admin"
API_BASIC_AUTH_PASSWORD: str = "change_me"
PERSIST_LOGS: bool = False
DRY_RUN: bool = False
NTFY_TOPIC_URL: str = ""
TELEGRAM_BOT_TOKEN: str = ""
TELEGRAM_CHAT_ID: str = ""
DATA_DIR: str = "/app/data"
```
Derived properties: `db_path` = `{DATA_DIR}/hevy2garmin_lite.db`, `override_mappings_path` = `{DATA_DIR}/exercise_mappings.json`, `webhook_retry_delays_minutes` = parsed `list[int]`.

`scratch_token_dir` (`{DATA_DIR}/scratch_tokens`) was removed 2026-08-01 — dead since the self-heal revision (leftover from the old copy-then-load design), zero references anywhere in `src/`/`scripts/`. `.garminconnect/` (via `GARMIN_TOKEN_HOST_DIR`) is the one real, live shared token store — see Authentication in `ai-docs/architecture.md`.

`CONFIDENT_PROBABILITY = 95.0` lives in `src/mapping.py`, **not** `config.py` — a fixed code constant, not an env var (deliberate — see mapping section below).

Docker-compose-only var (not read by the app itself): `GARMIN_TOKEN_HOST_DIR` — host path for the token volume mount. **Do not confuse with `GARMIN_TOKEN_SOURCE_DIR`** (container-internal) — this exact naming collision was a real bug once.

---

## HTTP routes (`src/main.py`)

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/` | Basic | Dashboard HTML |
| GET | `/v1/health` | none | Docker `HEALTHCHECK` target |
| GET | `/v1/status` | Basic | Non-blocking — never triggers login itself |
| POST | `/v1/auth/login` | Basic | Mirrors garmin-scale-sync's `initiate_login()`; no-ops if already authenticated/mfa-pending |
| POST | `/v1/auth/mfa` | Basic | Body `{"code": str}` |
| POST | `/v1/sync-now` | Basic | Runs `run_sync_cycle()` regardless of polling toggle state |
| GET/POST | `/v1/settings/polling` | Basic | Body `{"enabled": bool}` on POST; persisted in `sync_meta` |
| POST | `/v1/webhooks/hevy` | `Authorization` header == `HEVY_WEBHOOK_AUTH_TOKEN` (not Basic — Hevy's servers call this, not a browser) | Body must have `event: "workout.created"`; anything else → `{"status":"ignored"}` |
| GET | `/v1/logs` | Basic | |
| GET | `/v1/mappings/unmapped` | Basic | |
| GET | `/v1/mappings/categories` | Basic | Sorted list of valid category strings |
| POST | `/v1/mappings` | Basic | Body `{"template_id", "category", "note"}` — category-only, hand-entered |
| GET | `/v1/sync-history` | Basic | Last 50 rows of `synced_workouts` |
| POST | `/v1/mappings/learn-from-garmin/{hevy_workout_id}` | Basic | "Learn from Garmin" — reads back a manually-corrected exercise (category **and** name); 404 if no synced Garmin activity for that workout; 502 if the Garmin session died mid-request (resets the cached client) |

---

## `sync_one_workout()` — the shared core (`src/sync.py`)

```python
def sync_one_workout(db, mapper, client, workout_id: str, workout: dict,
                      garmin_activities: list[dict], already_claimed: set[int],
                      breaker: SetPushCircuitBreaker, dry_run: bool = False) -> str:
    # returns: "synced" | "no_watch_match" | "skipped_idempotent" | "failed"
```
Three callers:
1. `run_sync_cycle(db, mapper, dry_run)` — polling path, loops Hevy events, handles updated/deleted
2. `sync_workout_by_id(db, mapper, workout_id, dry_run)` — webhook path, always re-fetches fresh via `hevy_client.fetch_workout()`
3. Dashboard "Sync Now" → `main._scheduled_sync()` → `run_sync_cycle()`

---

## Exercise identity resolution (`src/mapping.py`)

```python
@dataclass(frozen=True)
class ExerciseIdentity:
    category: str
    name: str | None          # specific fit_tool subcategory name, or None
    probability: float | None  # CONFIDENT_PROBABILITY (95.0), or None only for total failure

def resolve(self, template_id: str | None, exercise_title: str) -> ExerciseIdentity
```
Resolution order: user override (`data/exercise_mappings.json`) → bundled catalog (`TEMPLATE_TO_FIT` lookup, category via static `CATEGORY_NAMES`, name via **dynamic runtime `fit_tool` lookup** mirroring `hevy2garmin`'s `_exercise_to_string()`) → fallback `TOTAL_BODY`, `name=None`, miss recorded to `unmapped_exercises`.

**Do not hand-write subcategory name strings.** A guessed-wrong value (`BARBELL_SQUAT` when the real `fit_tool` enum value is `BARBELL_BACK_SQUAT`) caused a live 400 "Invalid Sub-Category" during development. Always resolve via `fit_tool.profile.profile_type` dynamically, or leave `name=None`.

**Overrides can now carry a name, not just a category** (added for the "learn from Garmin" feature — `src/learn.py` + `POST /v1/mappings/learn-from-garmin/{id}`): `save_override(template_id, category, note="", name=None)` validates any given `name` via `mapping._validate_category_name_pair()` (same fit_tool cross-check, reversed direction — confirms `name` is a genuine member of `category`'s enum) and raises `ValueError` on mismatch. This is safe specifically because the only caller that ever passes a `name` is `learn_mappings_from_garmin()`, which sources it from Garmin's own confirmed state (a manual "Choose an Exercise" UI correction), never a hand-guess. Old category-only override entries (no `"name"` key in `data/exercise_mappings.json`) still resolve fine — `name` is optional/nullable, loaded as `None` if absent.

**`learn_mappings_from_garmin()` (`src/learn.py`)** matches a Garmin `GET exerciseSets` response back to Hevy exercises by **`startTime` string equality**, not `wktStepIndex`/`messageIndex` — confirmed live 2026-08-01 (activity `23810842954`) that both come back `null` on an entry a user manually corrected via Garmin's UI, even though `startTime` survives intact. It reconstructs the same synthesized timeline `push.py:build_exercise_sets_payload` used originally (`build_set_timeline(hevy_exercises, activity_duration_s)`) to compute each Hevy exercise's expected `startTime`, then joins on that. Requires `activity_start`/`activity_duration_s` sourced from `client.get_activity(activity_id)` (**not** `get_activities()`) — see the `parse_activity_summary_gmt` note below for why that needs a different timestamp parser. Only surfaces template_ids not already covered by `mapper.known_template_ids()` (bundled catalog ∪ existing overrides) — scoped to genuinely unmapped/custom exercises.

---

## Garmin `exerciseSets` payload (exact shape, `src/push.py:build_exercise_sets_payload`)

```json
{
  "activityId": 12345,
  "exerciseSets": [
    {
      "exercises": [{"category": "BENCH_PRESS", "name": "BARBELL_BENCH_PRESS", "probability": 95.0}],
      "duration": 45.0,
      "repetitionCount": 8,
      "weight": 60000.0,
      "setType": "ACTIVE",
      "startTime": "2026-08-01T10:15:30.0",
      "wktStepIndex": 0,
      "messageIndex": 0
    },
    {"exercises": [], "duration": 75.0, "setType": "REST",
     "startTime": "2026-08-01T10:16:15.0", "wktStepIndex": 0, "messageIndex": 1}
  ]
}
```
- `weight` is **grams** (`weight_kg * 1000`), not kg.
- `probability` is the field that actually gates rendering on Garmin's side — **not** `name`. Confirmed live 2026-08-01: `probability: null`/`0.0` → renders as "Choose an Exercise"/"Unknown" regardless of category validity. A real value (95.0 confirmed working; 12.3 worked on web but not mobile) unlocks correct rendering, including the specific `name` if resolved. Always send `mapping.CONFIDENT_PROBABILITY`.
- `startTime` format: `"%Y-%m-%dT%H:%M:%S.0"` (literal `.0`, no timezone offset).
- Endpoint: `PUT /activity-service/activity/{id}/exerciseSets`, undocumented. Call pattern: `client.client.put("connectapi", path, json=payload, api=True)` — **not** `client.connectapi()` (that's GET-only).
- Backup-before-push: `GET` same path via `client.connectapi(path)` (the high-level, decorated wrapper — fine for GET).
- **Atomic**: one exercise with an invalid subcategory 400s the *entire* payload, no per-exercise error. `push_exercise_sets()` retries once with `_strip_all_names()` (category + probability kept) on `GarminConnectConnectionError` matching `"Invalid Sub-Category"`.

---

## Hevy API surface (`src/hevy_client.py`)

| Function | Endpoint | Notes |
|---|---|---|
| `poll_events(since)` | `GET /v1/workouts/events` | `pageSize` max 10, paginated |
| `fetch_workout(id)` | `GET /v1/workouts/{id}` | Returns `None` on 404 |
| `register_webhook(url, token)` | `POST /v1/webhook-subscription` | Body `{"url", "auth_token"}` |
| `get_webhook_subscription()` | `GET /v1/webhook-subscription` | `None` on 404 |
| `ensure_webhook_registered(url, token)` | idempotent wrapper | No-ops if already pointing at `url` |
| `parse_webhook_payload(raw)` | pure fn | Only accepts `event == "workout.created"`; else `None` |
| `parse_hevy_timestamp(raw)` | pure fn | ISO 8601 `Z`-suffixed → UTC-aware `datetime` |

Auth header: `{"api-key": HEVY_API_KEY}`. Base URL: `https://api.hevyapp.com`. Webhook endpoint shapes corroborated across third-party integrations, **not Hevy's own official docs** — unverified against a live response.

---

## Matching rule (`src/matcher.py:find_best_match`)

Match requires **all** of: ≥70% temporal overlap (`MIN_OVERLAP_PCT = 0.70`), start drift ≤ `MATCH_TOLERANCE_MINUTES`, `activityType.typeKey == "strength_training"`, activity not already claimed. Ties broken by smallest drift. **Always uses `startTimeGMT`** (naive UTC string, `"%Y-%m-%d %H:%M:%S"`) — never `startTimeLocal`.

**Two different `startTimeGMT` formats exist, from two different Garmin calls — do not conflate them.** `client.get_activities()` (list endpoint, used for matching above) returns `"2026-08-01 09:03:06"` (space separator, no fraction) — parsed by `parse_garmin_gmt()`. `client.get_activity(id)` (single-activity endpoint, used by the "learn from Garmin" flow to get `summaryDTO.startTimeGMT`/`summaryDTO.duration`) returns `"2026-08-01T09:03:06.0"` (ISO `T` separator, trailing fractional seconds) — parsed by the separate `parse_activity_summary_gmt()`. Confirmed live 2026-08-01 against activity `23810842954`; using the wrong parser raises `ValueError` on the format mismatch.

---

## SQLite schema (`src/db.py`)

```sql
synced_workouts(hevy_workout_id PK, garmin_activity_id UNIQUE, sync_status, content_hash, synced_at)
  -- sync_status: synced | no_watch_match | failed | source_deleted
unmapped_exercises(template_id PK, exercise_name, first_seen_at, occurrences)
sync_meta(key PK, value)
  -- keys used: last_poll_timestamp, polling_enabled ("true"/"false" strings)
```
`Database` class wraps all queries — no raw SQL elsewhere in the codebase.

---

## FIT category enum (`src/mapping.py:CATEGORY_NAMES`)

```
0 BENCH_PRESS   1 CALF_RAISE   2 CARDIO   3 CARRY   4 CHOP   5 CORE   6 CRUNCH
7 CURL   8 DEADLIFT   9 FLYE   10 HIP_RAISE   11 HIP_STABILITY   12 HIP_SWING
13 HYPEREXTENSION   14 LATERAL_RAISE   15 LEG_CURL   16 LEG_RAISE   17 LUNGE
18 OLYMPIC_LIFT   19 PLANK   20 PLYO   21 PULL_UP   22 PUSH_UP   23 ROW
24 SHOULDER_PRESS   25 SHOULDER_STABILITY   26 SHRUG   27 SIT_UP   28 SQUAT
29 TOTAL_BODY   30 TRICEPS_EXTENSION   31 WARM_UP   32 RUN   65534 UNKNOWN
```
`template_map_source.py` data also uses category IDs **33, 36, 38, 39, 41, 42, 47, 52** with no confirmed string name — resolution falls back to `TOTAL_BODY`, no name attempted, for these. `FALLBACK_CATEGORY = "TOTAL_BODY"`.

Confirmed real `fit_tool` subcategory names (verified against `bikemap/fit_tool` source, not guessed): `BENCH_PRESS` sub `1` = `BARBELL_BENCH_PRESS`; `DEADLIFT` sub `0` = `BARBELL_DEADLIFT`; `SQUAT` sub `6` = `BARBELL_BACK_SQUAT` (**not** `BARBELL_SQUAT` — a wrong guess that caused a real live rejection).

Three template IDs contain non-hex characters (upstream data quirk, kept as-is): `4288G454`, `9373FSD1`, `32HKJ34K`. Flagged by `mapping.validate_catalog()`, not silently accepted.

---

## Known gotchas (one-liner each)

- `Garmin.connectapi()` is **GET-only**, no `method=` kwarg — PUT needs `client.client.put("connectapi", path, json=..., api=True)`. (`push.py`)
- Any `@retry(...)`-wrapped Garmin call whose exception type callers branch on **must** have `reraise=True`, or `tenacity.RetryError` hides the real exception from every downstream `except`. (`push.py`)
- A 401 through `client.client.put(...)` raises raw `GarminConnectConnectionError`, not `GarminConnectAuthenticationError` — normalized via `push.py:_reraise_401_as_auth_error`, matched on `"API Error 401"` in the message string.
- `docker-compose.yml` pins `name: hevy2garmin-lite` at the top level (added 2026-08-01) so Compose's project name — and therefore every built image tag (`hevy2garmin-lite-app`, `-test`, `-lint`, etc.) — stays stable regardless of what the checkout directory is called. Before this, Compose derived the project name from the folder (`hevva2`, an accidental leftover name), which is why older images/docs may reference `hevva2-*` tags.
- `docker compose build <service>` can serve a stale `COPY src/` layer even after real source changes — use `--no-cache` if a fix doesn't seem to apply.
- Each compose service (`app`/`test`/`spike`/`reauth`/`lint`) has its **own image** — rebuilding one doesn't rebuild the others.
- `docker compose run --rm lint` only bind-mounts `tests/` (`src/`/`scripts/` are `COPY`'d at build). Running `ruff check --fix` through it silently discards fixes to `src/`/`scripts/` when the container exits — use a one-off `docker run` with those dirs mounted (or the `hevy2garmin-lite-lint` image directly) instead.
- `GARMIN_TOKEN_HOST_DIR` (compose/host) vs `GARMIN_TOKEN_SOURCE_DIR` (container-internal, fixed) — do not set the latter from `.env`.
- **`probability`, not `name`, gates exercise-identity rendering on watch-recorded activities.** The original assumption ("Garmin ignores pushed names regardless of validity") was wrong — confirmed live 2026-08-01. Always send `CONFIDENT_PROBABILITY`.
- Never hand-guess a `fit_tool` subcategory name string — resolve dynamically or send `None`. A wrong guess 400s the *entire* atomic push.
- Hevy webhook fires **only** on `workout.created` — never edits/deletes. Polling (`run_sync_cycle`) is what catches those.
- `POST /v1/auth/login` does **not** force-reset an already-cached client if status is `authenticated` — matches garmin-scale-sync's own no-op behavior, not a bug.
- **A manual "Choose an Exercise" correction in Garmin's own UI nulls out `wktStepIndex`/`messageIndex`** on that set — confirmed live 2026-08-01. `learn.py` cannot join on those; it matches on `startTime` instead, which does survive.
- `client.get_activity(id)` and `client.get_activities()` return `startTimeGMT` in **two different string formats** — see the Matching rule section above. Using `parse_garmin_gmt` on a `get_activity()` response (or vice versa) raises `ValueError`.

---

## Test file → coverage map (`tests/`, 65 tests total, run via `docker compose run --rm test`)

| File | Covers |
|---|---|
| `test_matcher.py` | Overlap/drift/type matching, UTC-vs-local regression, back-to-back sessions, both `startTimeGMT` format parsers |
| `test_db_idempotency.py` | Sync record CRUD, `UNIQUE` constraint, idempotent reruns, polling toggle persistence |
| `test_mapping.py` | Category+name resolution (incl. known Bench Press/Deadlift regression values), override precedence (now category+name), unmapped recording, non-hex ID validation, `_validate_category_name_pair` |
| `test_learn.py` | `learn_mappings_from_garmin`: `startTime`-based matching, rejects invalid pairs, skips already-known template_ids, dedupes multi-set exercises |
| `test_timeline.py` | Set ordering, rest placement, scale-clamp overflow guards |
| `test_hevy_client.py` | `parse_webhook_payload` — only `workout.created` accepted |
| `test_config.py` | `WEBHOOK_RETRY_DELAYS_MINUTES` parsing |
| `test_push_auth_normalization.py` | 401-normalization, `reraise=True` regression, strip-and-retry on Invalid Sub-Category |

FastAPI routes themselves are **not** covered by automated tests (verified via live `curl` smoke tests instead) — see `CLAUDE.md` for why.
