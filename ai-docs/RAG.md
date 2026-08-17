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
| `src/templates/index.html` | The entire dashboard UI (single file, vanilla JS, no build step) — dark glassmorphic theme matching `garmin-scale-sync`'s (added 2026-08-02), own `#D6470F` accent instead of GSS's blue/purple. Loads Inter from Google Fonts CDN client-side (browser-only dependency, not a build step) |
| `scripts/spike_push_test.py` | Phase G live validation push (`docker compose run --rm spike`) |
| `scripts/spike_learn_readback.py` | Phase 0 spike for the "learn from Garmin" feature — push/read subcommands, throwaway once feature is fully E2E-verified |
| `scripts/trigger_garmin_reauth.py` | Standalone credential bootstrap, superseded by `app`'s own self-heal |
| `pyproject.toml` | `[tool.ruff]` config — line-length 125, `B008` ignored (FastAPI `Depends()` in defaults is intentional) |
| `Dockerfile` | Multi-stage: `base` (prod-only, `requirements.txt`) then `dev` (`FROM base`, adds `requirements-dev.txt`) — see "Docker image: size and memory" below |
| `requirements.txt` / `requirements-dev.txt` | Prod deps / test+lint-only extras (`pytest`, `pytest-mock`, `pytest-xdist`, `responses`, `ruff`) — split 2026-08-02 so the published image doesn't carry dev tooling |
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
SYNC_INTERVAL_MINUTES: int = 15               # fallback default only — live value is dashboard/sync_meta-controlled, see below
POLLING_ENABLED_DEFAULT: bool = False
WORKING_SET_SECONDS: int = 40                 # fallback default only — live value is dashboard/sync_meta-controlled, see below
WARMUP_SET_SECONDS: int = 25                  # ditto
REST_BETWEEN_SETS_SECONDS: int = 75           # ditto
REST_BETWEEN_EXERCISES_SECONDS: int = 120     # ditto
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
| GET/POST | `/v1/settings/polling` | Basic | Body `{"enabled": bool, "interval_minutes": int \| null}` on POST — `interval_minutes` omitted keeps the current value; `< 1` → 400. Both persisted in `sync_meta`, reflected live by rescheduling the APScheduler job (remove-then-re-add, so an interval change takes effect even while already enabled) |
| GET/POST | `/v1/settings/timeline` | Basic | Body `{"working_set_seconds", "warmup_set_seconds", "rest_between_sets_seconds", "rest_between_exercises_seconds"}` (all required ints) on POST; any `< 0` → 400. Persisted in `sync_meta` (added 2026-08-02), read fresh by `sync.py:_timeline_config_from_settings(db)` on every sync — no restart needed |
| POST | `/v1/webhooks/hevy` | `Authorization` header == `HEVY_WEBHOOK_AUTH_TOKEN` (not Basic — Hevy's servers call this, not a browser) | Body is `{"workoutId": "<uuid>"}` (confirmed live 2026-08-02 — see below); empty/missing `workoutId` → `{"status":"ignored"}` |
| GET | `/v1/logs` | Basic | Optional `?source=webhook\|polling\|manual` query param filters by trigger source; omit for all. Entries logged before this field existed have no `source` key and are only returned unfiltered |
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
    # returns: "synced" | "synced_partial" | "no_watch_match" | "skipped_idempotent" | "failed"
```
`synced_partial` (added 2026-08-17) means the push succeeded but `push_exercise_sets()` had to strip every exercise name in the workout after a 400 Invalid-Sub-Category (see the `exerciseSets` payload section below) — distinct from a full `"synced"` so this isn't silently indistinguishable in the dashboard. Both `"synced"` and `"synced_partial"` count as "already done" for the idempotency content-hash check, and both enable the "Map from Garmin" button in Sync History (`canLearn` in `index.html`).

After a successful `push_exercise_sets()` call (and only if the Hevy workout has a non-empty `title`), `sync_one_workout()` also calls `push.py:push_activity_name(client, activity_id, title)` to rename the Garmin activity from its generic auto-label (e.g. "Strength") to Hevy's real workout title (e.g. "RTT · Lower A (Mon)") — added 2026-08-17, this was previously never implemented at all (only `exerciseSets` were ever pushed). Best-effort: a non-auth failure here is logged and swallowed, not fatal to the sync (the sets already landed); a `GarminConnectAuthenticationError` propagates like any other auth failure in this function.

Three callers, each tagged with a distinct `/v1/logs` `source` (added 2026-08-02 — see HTTP routes and Known gotchas):
1. `run_sync_cycle(db, mapper, dry_run)` via `main._scheduled_sync()` (default `source="polling"`) — periodic timer path, loops Hevy events, handles updated/deleted
2. `sync_workout_by_id(db, mapper, workout_id, dry_run)` — webhook path (`source="webhook"`), always re-fetches fresh via `hevy_client.fetch_workout()`
3. Dashboard "Sync Now" / `POST /v1/sync-now` → `main._scheduled_sync(source="manual")` → `run_sync_cycle()` — same function as #1, different `source` tag so logs distinguish a periodic run from a manual click

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
Resolution order: user override (`data/exercise_mappings.json`) → bundled catalog (`TEMPLATE_TO_FIT` lookup, category via `CATEGORY_NAMES` — built from `fit_tool`'s real `ExerciseCategory` enum, not hand-typed, see FIT category enum section below — name via **dynamic runtime `fit_tool` lookup** mirroring `hevy2garmin`'s `_exercise_to_string()`) → fallback `TOTAL_BODY`, `name=None`, miss recorded to `unmapped_exercises`.

**Do not hand-write subcategory name strings.** A guessed-wrong value (`BARBELL_SQUAT` when the real `fit_tool` enum value is `BARBELL_BACK_SQUAT`) caused a live 400 "Invalid Sub-Category" during development. Always resolve via `fit_tool.profile.profile_type` dynamically, or leave `name=None`.

**Overrides can now carry a name, not just a category** (added for the "learn from Garmin" feature — `src/learn.py` + `POST /v1/mappings/learn-from-garmin/{id}`): `save_override(template_id, category, note="", name=None)` validates any given `name` via `mapping._validate_category_name_pair()` (same fit_tool cross-check, reversed direction — confirms `name` is a genuine member of `category`'s enum) and raises `ValueError` on mismatch. This is safe specifically because the only caller that ever passes a `name` is `learn_mappings_from_garmin()`, which sources it from Garmin's own confirmed state (a manual "Choose an Exercise" UI correction), never a hand-guess. Old category-only override entries (no `"name"` key in `data/exercise_mappings.json`) still resolve fine — `name` is optional/nullable, loaded as `None` if absent.

**`learn_mappings_from_garmin()` (`src/learn.py`)** — signature `(hevy_exercises, garmin_exercise_sets, already_known_template_ids) -> list[LearnedMapping]` (simplified 2026-08-17 — no longer takes `activity_start`/`activity_duration_s`/`timeline_config`, see below). Matches a Garmin `GET exerciseSets` response back to Hevy exercises by **array position / set-count grouping**, not `startTime` and not `wktStepIndex`/`messageIndex`. Filters to ACTIVE-only entries (empty `exercises` = REST), splits them into consecutive groups sized by each Hevy exercise's own set count, in order. Bails out entirely (returns `[]`, logs a warning) if total Garmin ACTIVE count ≠ total Hevy set count — positional grouping is only safe when the totals agree. Bails out per-exercise if its grouped Garmin sets disagree on `(category, name)`. Only surfaces template_ids not already covered by `mapper.known_template_ids()`. **`known_template_ids()` = bundled catalog entries that actually resolve to a specific name ∪ overrides that already carry a validated `name`** (fixed 2026-08-02 for overrides, fixed again 2026-08-17 for the bundled-catalog side — see Known gotchas). A category-only override, or a bundled catalog entry whose category/subcategory can't be resolved (falls back to generic `TOTAL_BODY`/`name=None`), both stay eligible for learning: correct it in Garmin's UI, then re-run "Map from Garmin" on that workout and it overwrites the guess/fallback with the real name.

**Why not `startTime` anymore**: the original design reconstructed the same synthesized timeline `push.py` used at push time and joined on `startTime` string equality (`wktStepIndex`/`messageIndex` null out on a manually-corrected entry — confirmed live 2026-08-01, activity `23810842954` — so index-based joining looked unusable). This broke in production: any drift between the timeline-tuning config in effect at push-time vs. learn-time (e.g. the user changed the dashboard's Timeline Tuning sliders in between) desynced the reconstructed times, silently. Re-diagnosed live 2026-08-17 (activity `24010259873`, full raw `exerciseSets` dump) that Garmin's response preserves *exact array position* even around a corrected entry — only `wktStepIndex`/`messageIndex` null out, not the entry's place in the array. Position-based grouping is purely structural (no time, no config) and was verified end-to-end against that real corrected entry before shipping.

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
- **Atomic**: one exercise with an invalid subcategory 400s the *entire* payload, no per-exercise error. `push_exercise_sets()` retries once with `_strip_all_names()` (category + probability kept) on `GarminConnectConnectionError` matching `"Invalid Sub-Category"`. **Returns `bool`** (added 2026-08-17): `True` if this fallback fired (every name in the workout got stripped), `False` on a clean first-try push — `sync_one_workout()` uses this to set `sync_status = "synced_partial"` instead of `"synced"`, so a silently-degraded push is no longer indistinguishable from a fully successful one (previously only logged to container stdout via `logger.warning`, invisible in the dashboard).
- **Activity title**: `push.py:push_activity_name(client, activity_id, title)` (added 2026-08-17) calls `Garmin.set_activity_name(activity_id, title)` — a library-provided high-level method, but like `exerciseSets` it goes through the low-level `self.client.put(...)` internally, **not** the decorated `connectapi()`, so it needs the same `_reraise_401_as_auth_error()` treatment. `sync_one_workout()` calls it with Hevy's workout-level `title` field right after a successful `push_exercise_sets()`. **Unverified**: the exact Hevy workout-level `title` field name hasn't been confirmed against a live `fetch_workout()` response in this session — same discipline as the webhook-payload fix applies if it turns out wrong.

---

## Hevy API surface (`src/hevy_client.py`)

| Function | Endpoint | Notes |
|---|---|---|
| `poll_events(since)` | `GET /v1/workouts/events` | `pageSize` max 10, paginated |
| `fetch_workout(id)` | `GET /v1/workouts/{id}` | Returns `None` on 404 |
| `register_webhook(url, token)` | `POST /v1/webhook-subscription` | Body `{"url", "auth_token"}` |
| `get_webhook_subscription()` | `GET /v1/webhook-subscription` | `None` on 404 |
| `ensure_webhook_registered(url, token)` | idempotent wrapper | No-ops if already pointing at `url` |
| `parse_webhook_payload(raw)` | pure fn | Extracts `raw["workoutId"]`; `None` if missing/empty. No `event` field exists in the real payload — see below |
| `parse_hevy_timestamp(raw)` | pure fn | ISO 8601 `Z`-suffixed → UTC-aware `datetime` |

Auth header: `{"api-key": HEVY_API_KEY}`. Base URL: `https://api.hevyapp.com`.

**Webhook payload shape — confirmed live 2026-08-02** (captured via a temp webhook receiver on a real Hevy account, request forwarded from `cf-connecting-ip` on Cloudflare, `user-agent: node-fetch`): the body is exactly `{"workoutId": "<uuid>"}` — no `event` field, camelCase key, no nested `workout` object. The originally assumed shape (an `{"event": "workout.created", "workout": {...}}` envelope, "corroborated across third-party integrations, not Hevy's own official docs") was wrong — every real webhook call was silently parsed to `None` and `{"status":"ignored"}` returned, so **no webhook had ever actually synced a workout before this fix**, despite delivery, auth token, and registration all working correctly. Fixed in `parse_webhook_payload` (`src/hevy_client.py`) — it no longer gates on an `event` field at all, since Hevy apparently only ever sends this one shape for `workout.created` (consistent with it being the only event Hevy fires).

---

## Matching rule (`src/matcher.py:find_best_match`)

Match requires **all** of: ≥70% temporal overlap (`MIN_OVERLAP_PCT = 0.70`), start drift ≤ `MATCH_TOLERANCE_MINUTES`, `activityType.typeKey == "strength_training"`, activity not already claimed. Ties broken by smallest drift. **Always uses `startTimeGMT`** (naive UTC string, `"%Y-%m-%d %H:%M:%S"`) — never `startTimeLocal`.

**Two different `startTimeGMT` formats exist, from two different Garmin calls — do not conflate them.** `client.get_activities()` (list endpoint, used for matching above) returns `"2026-08-01 09:03:06"` (space separator, no fraction) — parsed by `parse_garmin_gmt()`. `client.get_activity(id)` (single-activity endpoint, used by the "learn from Garmin" flow to get `summaryDTO.startTimeGMT`/`summaryDTO.duration`) returns `"2026-08-01T09:03:06.0"` (ISO `T` separator, trailing fractional seconds) — parsed by the separate `parse_activity_summary_gmt()`. Confirmed live 2026-08-01 against activity `23810842954`; using the wrong parser raises `ValueError` on the format mismatch.

---

## SQLite schema (`src/db.py`)

```sql
synced_workouts(hevy_workout_id PK, garmin_activity_id UNIQUE, sync_status, content_hash, synced_at)
  -- sync_status: synced | synced_partial | no_watch_match | failed | source_deleted
unmapped_exercises(template_id PK, exercise_name, first_seen_at, occurrences)
sync_meta(key PK, value)
  -- keys used: last_poll_timestamp, polling_enabled ("true"/"false" strings),
  --            polling_interval_minutes (stringified int, default SYNC_INTERVAL_MINUTES),
  --            working_set_seconds / warmup_set_seconds / rest_between_sets_seconds /
  --            rest_between_exercises_seconds (stringified ints, defaults from Settings)
```
`Database` class wraps all queries — no raw SQL elsewhere in the codebase.

---

## FIT category enum (`src/mapping.py:CATEGORY_NAMES`)

**Built dynamically from `fit_tool.profile.profile_type.ExerciseCategory`'s real members** (`mapping.py:_load_category_names()`, fixed 2026-08-17) — not a hand-typed table. It previously stopped at id 32 and silently missed ids 33-53 entirely:

```
0 BENCH_PRESS   1 CALF_RAISE   2 CARDIO   3 CARRY   4 CHOP   5 CORE   6 CRUNCH
7 CURL   8 DEADLIFT   9 FLYE   10 HIP_RAISE   11 HIP_STABILITY   12 HIP_SWING
13 HYPEREXTENSION   14 LATERAL_RAISE   15 LEG_CURL   16 LEG_RAISE   17 LUNGE
18 OLYMPIC_LIFT   19 PLANK   20 PLYO   21 PULL_UP   22 PUSH_UP   23 ROW
24 SHOULDER_PRESS   25 SHOULDER_STABILITY   26 SHRUG   27 SIT_UP   28 SQUAT
29 TOTAL_BODY   30 TRICEPS_EXTENSION   31 WARM_UP   32 RUN
33 BIKE   34 CARDIO_SENSORS   35 MOVE   36 POSE   37 BANDED_EXERCISES
38 BATTLE_ROPE   39 ELLIPTICAL   40 FLOOR_CLIMB   41 INDOOR_BIKE
42 INDOOR_ROW   43 LADDER   44 SANDBAG   45 SLED   46 SLEDGE_HAMMER
47 STAIR_STEPPER   49 SUSPENSION   50 TIRE   52 RUN_INDOOR   53 BIKE_OUTDOOR
65534 UNKNOWN
```
(48 and 51 are gaps in `fit_tool`'s own enum, not omissions here.) `template_map_source.py` data previously showed category IDs **33, 36, 38, 39, 41, 42, 47, 52** as "no confirmed string name" — that was never true, they were just missing from the old hand-typed table; all resolve correctly now. `FALLBACK_CATEGORY = "TOTAL_BODY"` is only hit by a category id that doesn't exist in `ExerciseCategory` at all.

Confirmed real `fit_tool` subcategory names (verified against `bikemap/fit_tool` source, not guessed): `BENCH_PRESS` sub `1` = `BARBELL_BENCH_PRESS`; `DEADLIFT` sub `0` = `BARBELL_DEADLIFT`; `SQUAT` sub `6` = `BARBELL_BACK_SQUAT` (**not** `BARBELL_SQUAT` — a wrong guess that caused a real live rejection).

Three template IDs contain non-hex characters (upstream data quirk, kept as-is): `4288G454`, `9373FSD1`, `32HKJ34K`. Flagged by `mapping.validate_catalog()`, not silently accepted.

---

## Docker image: size and memory (measured 2026-08-02, target is a 1GB Oracle Free Tier VM)

`Dockerfile` is multi-stage: `base` (`requirements.txt` only — `python:3.12-slim`, `fastapi`/`uvicorn`/`garminconnect`/`fit_tool`/etc.) and `dev` (`FROM base`, adds `requirements-dev.txt` — `pytest`, `pytest-mock`, `pytest-xdist`, `responses`, `ruff`). `docker-compose.yml` sets `target: base` for `app`/`spike`/`reauth` and `target: dev` for `test`/`lint`; `publish.yml`'s `docker/build-push-action` step also pins `target: base` so GHCR never ships dev tooling. Before this split (single-stage, one shared `requirements.txt`), the published image was 293MB; `base` alone is **258MB**.

Measured idle RSS of the running `app` container: **~42–44MB** (`docker stats`), confirmed via a live `docker compose up -d app` + `docker stats --no-stream` run — comfortably inside the 1GB target even with `garmin-scale-sync` and OS/Docker overhead running alongside it. `docker-compose.yml` now sets `mem_limit`/`mem_reservation` on every service (`app`: `256m`/`128m`, `spike`/`reauth`/`lint`: `256m`, `test`: `512m`, since `pytest-xdist` forks one worker per core) as a safety rail — not because normal usage is close to those numbers, but so a leak in this or a sibling container can't OOM the whole box. Compose (non-swarm) honors top-level `mem_limit`/`mem_reservation` directly; confirmed via `docker inspect`'s `HostConfig.Memory`.

`test` runs pytest in parallel via `pytest-xdist` (`-n auto`, one worker per core — same pattern `garmin-scale-sync`'s own CI uses), cutting the 65-test suite from ~17s to ~9s locally.

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
- **`GARMIN_TOKEN_HOST_DIR` only does anything inside this repo's own `docker-compose.yml`.** A hand-written compose file (e.g. deploying this app alongside `garmin-scale-sync` on one host) must mount the shared host path to `/app/garmin_tokens_source` explicitly — omitting it doesn't error, the app just self-heals into its own private, unshared token, and each service silently shows a different auth status with no obvious link. Confirmed live 2026-08-02: exactly this happened on the user's Oracle VM — a bundled compose file mounted `./gss-data:/app/data` for both services but had no second volume line for `hevy2garmin-lite`'s token path at all.
- **`garmin-scale-sync` does not expose a separate token volume** — it stores its session at `{its DATA_DIR}/.garminconnect` (a subfolder of its own `/app/data` mount, set via its `GARMINTOKENS` env var in its `config.py`), not at the data-mount root. "Pointing both services at the same directory" means mounting that `.garminconnect` subfolder into this app's `/app/garmin_tokens_source`, e.g. `./gss-data/.garminconnect:/app/garmin_tokens_source` — not `./gss-data` itself.
- **`probability`, not `name`, gates exercise-identity rendering on watch-recorded activities.** The original assumption ("Garmin ignores pushed names regardless of validity") was wrong — confirmed live 2026-08-01. Always send `CONFIDENT_PROBABILITY`.
- Never hand-guess a `fit_tool` subcategory name string — resolve dynamically or send `None`. A wrong guess 400s the *entire* atomic push.
- Hevy webhook fires **only** on `workout.created` — never edits/deletes. Polling (`run_sync_cycle`) is what catches those.
- **The real Hevy webhook body is `{"workoutId": "<uuid>"}`, not `{"event": "workout.created", "workout": {...}}`.** The original assumed shape was never real — confirmed live 2026-08-02, see the Hevy API surface section. If a future Hevy payload sample doesn't parse, check the actual raw body before guessing again; a wrong assumption here fails *silently* (`{"status":"ignored"}`, HTTP 200), not with an error.
- **`docker logs` showing `Scheduler started` does not mean polling is on** — APScheduler's `BackgroundScheduler` always starts unconditionally (webhook retry jobs need it too); the actual interval polling job (`POLL_JOB_ID`) is only added if `db.get_polling_enabled(...)` is `True`. Check `GET /v1/status`'s `polling_enabled` field, not the presence of that log line.
- `/v1/logs` entries carry a `source` field (`webhook` / `polling` / `manual`, added 2026-08-02) so the three `sync_one_workout()` callers are distinguishable — see that section above. `_scheduled_sync(source=...)` is shared by both the periodic timer (`"polling"`) and `POST /v1/sync-now` (`"manual"`); they're the same underlying `run_sync_cycle()` call, only the tag differs.
- `POST /v1/auth/login` does **not** force-reset an already-cached client if status is `authenticated` — matches garmin-scale-sync's own no-op behavior, not a bug.
- **A manual "Choose an Exercise" correction in Garmin's own UI nulls out `wktStepIndex`/`messageIndex`** on that set — confirmed live 2026-08-01. `learn.py` cannot join on those; it matches on `startTime` instead, which does survive.
- **A category-only override used to permanently block "learn from Garmin" for that exercise** — `known_template_ids()` counted any override, name or not, as "already known," so an accidental dashboard "Save" click (or a deliberate quick category fix) could never be upgraded to a real name afterward, with no delete/reset endpoint to undo it either. Fixed live 2026-08-02: only name-bearing overrides count as known now. If this regresses, the symptom is "Map from Garmin" silently finding nothing for an exercise you know you corrected in Garmin's UI.
- **The same bug existed one layer up, in the bundled catalog itself, until 2026-08-17.** `known_template_ids()` used to count *any* `TEMPLATE_TO_FIT` membership as "known," even for the ~18/428 entries whose category id or subcategory can't actually be resolved (fall back to generic `TOTAL_BODY`/`name=None`). Real example hit live: "Terminal Knee Extension Stretch" was permanently un-learnable no matter how many times it was corrected in Garmin's UI. Fixed: `known_template_ids()` now calls `resolve()` on every catalog id and only counts it as known if a specific name actually comes back.
- **That same fix didn't actually take effect on first deploy** — `src/learn.py:learn_mappings_from_garmin()` had its own second, redundant gate, `if template_id in TEMPLATE_TO_FIT or template_id in already_known_template_ids: continue`, independent of whatever `known_template_ids()` computed. The `TEMPLATE_TO_FIT` half of that check alone still blocked any catalog-present-but-fallback-resolved exercise, so a user who deployed the `mapping.py` fix still saw "no changes found" clicking "Map from Garmin." Fixed same day by removing the redundant `template_id in TEMPLATE_TO_FIT` clause — `already_known_template_ids` (i.e. `mapper.known_template_ids()`) is the single source of truth now. **Lesson**: when a helper like `known_template_ids()` changes what "known" means, grep every caller for a duplicate ad-hoc check of the same underlying data, not just the one call site being fixed.
- **`CATEGORY_NAMES` was a hand-typed table that silently missed 19 real categories** (ids 33-53: `BIKE`, `MOVE`, `BATTLE_ROPE`, `ELLIPTICAL`, `INDOOR_BIKE`, `INDOOR_ROW`, `STAIR_STEPPER`, `BANDED_EXERCISES`, `RUN_INDOOR`, etc.) until 2026-08-17 — confirmed by diffing it against `fit_tool.profile.profile_type.ExerciseCategory`'s real members. This is exactly why the 8 category ids previously documented as "unresolved, falls back to TOTAL_BODY" (`33, 36, 38, 39, 41, 42, 47, 52`) behaved that way — not a genuine Garmin limitation, just an incomplete table on our side that was never cross-checked against the package it claims to mirror. Symptom if this regresses: `_validate_category_name_pair()` rejects an otherwise-real (category, name) pair that Garmin itself just returned.
- **`learn_mappings_from_garmin()`'s original `startTime`-based join was replaced with array-position/set-count grouping on 2026-08-17** — see the Exercise identity resolution section above for the full story. If "Map from Garmin" ever silently learns nothing again, check for a `WARNING` log line first (`main.py`'s route logs one either way) — either "ACTIVE set count does not match" (Hevy workout edited after syncing) or "inconsistent (category, name) across its N Garmin set(s)" (something odd in the grouped data) — both are deliberate safety bail-outs, not silent failures.
- **`timeline.py`'s scale factor used to apply to Hevy's *explicit* `duration_seconds` sets too, not just estimated ones** — until 2026-08-17. A real 30s stretch/plank could render as 45s+ on Garmin whenever the ideal total didn't exactly equal the real activity duration (i.e. almost always). The one test covering this path happened to set them equal, so `scale` was trivially `1.0` and never caught it. Fixed: explicit durations are excluded from the scale computation and never multiplied by it.
- **Garmin's activity title (e.g. "Strength") was never touched by this codebase at all, until 2026-08-17** — only `exerciseSets` were ever pushed. Fixed via `push.py:push_activity_name()`, called from `sync_one_workout()` with Hevy's workout-level `title`. If the rename doesn't take effect, first check whether Hevy's real field name for the workout title actually is `title` — this was implemented without a live confirmation call (see the `exerciseSets` payload section's note on this).
- `client.get_activity(id)` and `client.get_activities()` return `startTimeGMT` in **two different string formats** — see the Matching rule section above. Using `parse_garmin_gmt` on a `get_activity()` response (or vice versa) raises `ValueError`.

---

## Test file → coverage map (`tests/`, 95 tests total, run via `docker compose run --rm test`)

| File | Covers |
|---|---|
| `test_matcher.py` | Overlap/drift/type matching, UTC-vs-local regression, back-to-back sessions, both `startTimeGMT` format parsers |
| `test_db_idempotency.py` | Sync record CRUD, `UNIQUE` constraint, idempotent reruns, polling toggle + interval persistence, timeline-tuning seconds persistence (all 4 keys, parametrized) |
| `test_mapping.py` | Category+name resolution (incl. known Bench Press/Deadlift regression values), override precedence (now category+name), unmapped recording, non-hex ID validation, `_validate_category_name_pair` (incl. the live-confirmed `BANDED_EXERCISES`/`LEG_EXTENSION` regression), `CATEGORY_NAMES` completeness against the real `fit_tool` enum, `known_template_ids()` scoping (category-only overrides *and* bundled-catalog fallback entries both stay learnable; property test asserting membership matches actual `resolve()` outcome) |
| `test_learn.py` | `learn_mappings_from_garmin`: array-position/set-count grouping (incl. the live-reproduced case where `wktStepIndex`/`messageIndex` are null on a corrected entry), rejects invalid pairs, skips already-known template_ids, dedupes repeated template_ids across exercise blocks, bails out on ACTIVE-count mismatch, bails out on inconsistent identity within a group |
| `test_timeline.py` | Set ordering, rest placement, scale-clamp overflow guards, explicit `duration_seconds` sets are never scaled even when `scale != 1.0` |
| `test_hevy_client.py` | `parse_webhook_payload` against the confirmed-live `{"workoutId": "<uuid>"}` shape; missing/empty `workoutId` ignored |
| `test_config.py` | `WEBHOOK_RETRY_DELAYS_MINUTES` parsing |
| `test_push_auth_normalization.py` | 401-normalization, `reraise=True` regression, strip-and-retry on Invalid Sub-Category (now asserts the returned `bool`), `push_activity_name()`'s own 401-normalization |
| `test_sync.py` (new 2026-08-17) | `sync_one_workout()` orchestration: pushes Hevy's `title` as the activity name, skips rename when title is empty, `synced_partial` status when names were stripped, rename failure is best-effort (doesn't fail the sync) vs. an auth failure during rename (does) |

FastAPI routes themselves are **not** covered by automated tests (verified via live `curl` smoke tests instead) — see `CLAUDE.md` for why.
