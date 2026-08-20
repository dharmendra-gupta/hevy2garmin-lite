"""FastAPI dashboard + in-process scheduler + Hevy webhook receiver.

Mirrors garmin-scale-sync's dashboard pattern: single static HTML template,
HTTP Basic Auth, in-memory log ring buffer with optional disk persistence.
Auth is now self-healing (garmin_client.py) rather than strictly read-only —
see the plan's §2 revision. Polling is a reconciliation safety net, default
OFF, toggleable here; the webhook is the fast primary trigger for new
workouts.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from garminconnect import GarminConnectAuthenticationError
from pydantic import BaseModel

from src import hevy_client
from src.config import settings
from src.db import Database
from src.garmin_client import (
    TokenLoadError,
    auth_status,
    get_garmin_client,
    reset_garmin_client,
    submit_mfa_code,
)
from src.learn import learn_mappings_from_garmin
from src.mapping import CATEGORY_NAMES, ExerciseMapper, validate_catalog
from src.push import get_existing_exercise_sets
from src.sync import run_sync_cycle, sync_workout_by_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("hevy2garmin_lite")

db = Database(settings.db_path)
mapper = ExerciseMapper(settings.override_mappings_path, db)

log_lock = threading.Lock()
memory_logs: deque = deque(maxlen=50)
LOGS_FILE = os.path.join(settings.DATA_DIR, "sync_logs.json")

scheduler = BackgroundScheduler()
_last_run_summary: dict = {}
_login_thread: threading.Thread | None = None
_login_error_detail: str | None = None

POLL_JOB_ID = "poll"


def _log_event(source: str, status_: str, detail: dict) -> None:
    entry = {"timestamp": datetime.now(UTC).isoformat(), "source": source, "status": status_, "detail": detail}
    with log_lock:
        memory_logs.appendleft(entry)
        if settings.PERSIST_LOGS:
            logs = []
            if os.path.exists(LOGS_FILE):
                try:
                    with open(LOGS_FILE) as f:
                        logs = json.loads(f.read())
                except Exception:
                    logs = []
            logs.insert(0, entry)
            logs = logs[:100]
            try:
                with open(LOGS_FILE, "w") as f:
                    json.dump(logs, f, indent=2)
            except Exception as e:  # noqa: BLE001
                logger.error("Failed to persist logs: %s", e)


def _scheduled_sync(source: str = "polling") -> None:
    """The reconciliation cycle. Shared by the (optional) periodic timer
    (source="polling") and the manual Sync Now button (source="manual") —
    the button works regardless of whether periodic polling is enabled."""
    global _last_run_summary
    logger.info("Starting sync cycle (%s reconciliation)", source)
    result = run_sync_cycle(db, mapper, dry_run=settings.DRY_RUN)
    summary = {
        "synced": result.synced,
        "synced_partial": result.synced_partial,
        "no_match": result.no_match,
        "skipped_idempotent": result.skipped_idempotent,
        "failed": result.failed,
        "source_deleted": result.source_deleted,
        "errors": result.errors,
    }
    _last_run_summary = {**summary, "source": source, "ran_at": datetime.now(UTC).isoformat()}
    _log_event(source, "failed" if result.errors else "success", summary)
    logger.info("Sync cycle complete (%s): %s", source, summary)


def _run_login_in_background() -> None:
    global _login_error_detail
    _login_error_detail = None
    try:
        get_garmin_client()
        logger.info("Background Garmin login succeeded.")
    except TokenLoadError as e:
        _login_error_detail = str(e)
        logger.error("Background Garmin login failed: %s", e)


def _set_polling_job(enabled: bool, interval_minutes: int) -> None:
    # Always remove-and-re-add rather than no-op-if-exists: this is also how
    # an interval *change* while polling is already on takes effect, not
    # just the enabled/disabled toggle.
    if scheduler.get_job(POLL_JOB_ID):
        scheduler.remove_job(POLL_JOB_ID)
    if enabled:
        scheduler.add_job(_scheduled_sync, "interval", minutes=interval_minutes, id=POLL_JOB_ID)
        logger.info("Polling enabled (every %d min)", interval_minutes)
    else:
        logger.info("Polling disabled")


def _handle_webhook_workout(workout_id: str, attempt: int = 0) -> None:
    """Runs after the webhook HTTP response is already sent. Retries at the
    configured delays if no matching watch activity is found yet — handles
    the case where Hevy's workout closes before the watch has synced to
    Garmin Connect over Bluetooth."""
    try:
        result_status = sync_workout_by_id(db, mapper, workout_id, dry_run=settings.DRY_RUN)
    except (TokenLoadError, GarminConnectAuthenticationError) as e:
        reset_garmin_client()
        logger.error("Webhook sync for %s aborted — Garmin auth: %s", workout_id, e)
        _log_event("webhook", "failed", {"workout_id": workout_id, "attempt": attempt, "error": str(e)})
        return
    except Exception as e:  # noqa: BLE001
        logger.error("Webhook sync for %s failed: %s", workout_id, e)
        _log_event("webhook", "failed", {"workout_id": workout_id, "attempt": attempt, "error": str(e)})
        return

    _log_event("webhook", result_status, {"workout_id": workout_id, "attempt": attempt})
    logger.info("Webhook sync attempt %d for %s: %s", attempt, workout_id, result_status)

    if result_status == "no_watch_match":
        delays = settings.webhook_retry_delays_minutes
        if attempt < len(delays):
            delay_minutes = delays[attempt]
            run_at = datetime.now(UTC) + timedelta(minutes=delay_minutes)
            scheduler.add_job(
                _handle_webhook_workout, "date", run_date=run_at,
                args=[workout_id, attempt + 1],
                id=f"webhook-retry-{workout_id}-{attempt + 1}",
                replace_existing=True,
            )
            logger.info("No watch match yet for %s — retrying in %d min", workout_id, delay_minutes)


@asynccontextmanager
async def lifespan(app: FastAPI):
    issues = validate_catalog()
    if issues["non_hex_template_ids"] or issues["unresolved_categories"]:
        logger.warning("Mapping catalog data-quality issues at startup: %s", issues)

    scheduler.start()
    _set_polling_job(
        db.get_polling_enabled(settings.POLLING_ENABLED_DEFAULT),
        db.get_polling_interval_minutes(settings.SYNC_INTERVAL_MINUTES),
    )

    if settings.PUBLIC_BASE_URL and settings.HEVY_WEBHOOK_AUTH_TOKEN and settings.HEVY_API_KEY:
        try:
            webhook_url = settings.PUBLIC_BASE_URL.rstrip("/") + "/v1/webhooks/hevy"
            hevy_client.ensure_webhook_registered(webhook_url, settings.HEVY_WEBHOOK_AUTH_TOKEN)
        except Exception as e:  # noqa: BLE001
            logger.warning("Hevy webhook registration failed at startup (will not block startup): %s", e)
    else:
        logger.info("Hevy webhook not registered — PUBLIC_BASE_URL/HEVY_WEBHOOK_AUTH_TOKEN not fully configured.")

    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Hevy2Garmin Lite", version="1.0.0", lifespan=lifespan)
security = HTTPBasic()


def verify_basic_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    ok_user = secrets.compare_digest(credentials.username.encode(), settings.API_BASIC_AUTH_USERNAME.encode())
    ok_pass = secrets.compare_digest(credentials.password.encode(), settings.API_BASIC_AUTH_PASSWORD.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect credentials", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


class MappingUpdate(BaseModel):
    template_id: str
    category: str
    note: str = ""


class MFASubmission(BaseModel):
    code: str


class PollingSetting(BaseModel):
    enabled: bool
    interval_minutes: int | None = None


class TimelineSetting(BaseModel):
    working_set_seconds: int
    warmup_set_seconds: int
    rest_between_sets_seconds: int
    rest_between_exercises_seconds: int


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(verify_basic_auth)])
async def dashboard():
    template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    return open(template_path).read()


@app.get("/v1/health")
async def health():
    # Unauthenticated on purpose — this is what Docker HEALTHCHECK hits.
    return {"status": "ok"}


@app.get("/v1/status", dependencies=[Depends(verify_basic_auth)])
async def get_status():
    # Non-blocking: reports cached auth state, never triggers a fresh login
    # itself (that could block up to MFA_TIMEOUT_SECONDS). Use
    # POST /v1/auth/login to explicitly trigger one.
    return {
        "garmin_auth": auth_status(),
        "garmin_login_error": _login_error_detail,
        "hevy_api_key_configured": bool(settings.HEVY_API_KEY),
        "webhook_registered": bool(settings.PUBLIC_BASE_URL and settings.HEVY_WEBHOOK_AUTH_TOKEN),
        "polling_enabled": db.get_polling_enabled(settings.POLLING_ENABLED_DEFAULT),
        "dry_run": settings.DRY_RUN,
        "last_run": _last_run_summary,
        "sync_interval_minutes": db.get_polling_interval_minutes(settings.SYNC_INTERVAL_MINUTES),
        "match_tolerance_minutes": settings.MATCH_TOLERANCE_MINUTES,
    }


@app.post("/v1/auth/login", dependencies=[Depends(verify_basic_auth)])
async def trigger_login():
    # Mirrors garmin-scale-sync's initiate_login() exactly: no-ops (with an
    # explanatory status) if there's nothing useful to do, otherwise starts
    # the background login thread. It does NOT force-reset an already-cached
    # client — garmin-scale-sync doesn't either, so neither do we.
    global _login_thread

    if settings.DRY_RUN:
        return {"status": "dry_run", "message": "Login is disabled in dry-run mode."}

    current = auth_status()
    if current["status"] == "mfa_required":
        return {"status": "mfa_required", "message": "MFA code is already requested and waiting."}
    if current["status"] == "authenticated":
        return {"status": "success", "message": "Already authenticated."}

    if _login_thread is None or not _login_thread.is_alive():
        _login_thread = threading.Thread(target=_run_login_in_background, daemon=True)
        _login_thread.start()

    return {"status": "checking", "message": "Garmin login sequence initiated."}


@app.post("/v1/auth/mfa", dependencies=[Depends(verify_basic_auth)])
async def submit_mfa(body: MFASubmission):
    try:
        submit_mfa_code(body.code)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return {"status": "ok"}


@app.post("/v1/sync-now", dependencies=[Depends(verify_basic_auth)])
async def sync_now():
    _scheduled_sync(source="manual")
    return _last_run_summary


@app.get("/v1/settings/polling", dependencies=[Depends(verify_basic_auth)])
async def get_polling_setting():
    return {
        "enabled": db.get_polling_enabled(settings.POLLING_ENABLED_DEFAULT),
        "interval_minutes": db.get_polling_interval_minutes(settings.SYNC_INTERVAL_MINUTES),
    }


@app.post("/v1/settings/polling", dependencies=[Depends(verify_basic_auth)])
async def set_polling_setting(body: PollingSetting):
    interval = body.interval_minutes if body.interval_minutes is not None else db.get_polling_interval_minutes(
        settings.SYNC_INTERVAL_MINUTES
    )
    if interval < 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "interval_minutes must be at least 1")

    db.set_polling_enabled(body.enabled)
    db.set_polling_interval_minutes(interval)
    _set_polling_job(body.enabled, interval)
    return {"enabled": body.enabled, "interval_minutes": interval}


@app.get("/v1/settings/timeline", dependencies=[Depends(verify_basic_auth)])
async def get_timeline_setting():
    return {
        "working_set_seconds": db.get_working_set_seconds(settings.WORKING_SET_SECONDS),
        "warmup_set_seconds": db.get_warmup_set_seconds(settings.WARMUP_SET_SECONDS),
        "rest_between_sets_seconds": db.get_rest_between_sets_seconds(settings.REST_BETWEEN_SETS_SECONDS),
        "rest_between_exercises_seconds": db.get_rest_between_exercises_seconds(settings.REST_BETWEEN_EXERCISES_SECONDS),
    }


@app.post("/v1/settings/timeline", dependencies=[Depends(verify_basic_auth)])
async def set_timeline_setting(body: TimelineSetting):
    values = {
        "working_set_seconds": body.working_set_seconds,
        "warmup_set_seconds": body.warmup_set_seconds,
        "rest_between_sets_seconds": body.rest_between_sets_seconds,
        "rest_between_exercises_seconds": body.rest_between_exercises_seconds,
    }
    for name, value in values.items():
        if value < 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{name} must be at least 0")

    db.set_working_set_seconds(body.working_set_seconds)
    db.set_warmup_set_seconds(body.warmup_set_seconds)
    db.set_rest_between_sets_seconds(body.rest_between_sets_seconds)
    db.set_rest_between_exercises_seconds(body.rest_between_exercises_seconds)
    return values


@app.post("/v1/webhooks/hevy")
async def hevy_webhook(request: Request, background_tasks: BackgroundTasks):
    # Hevy's own secret, not dashboard Basic Auth — this endpoint is called
    # by Hevy's servers, not a browser.
    auth_header = request.headers.get("authorization", "")
    if not settings.HEVY_WEBHOOK_AUTH_TOKEN or not secrets.compare_digest(auth_header, settings.HEVY_WEBHOOK_AUTH_TOKEN):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook auth token")

    body = await request.json()
    parsed = hevy_client.parse_webhook_payload(body)
    if parsed is None or not parsed["workout_id"]:
        return {"status": "ignored"}

    # Must respond within Hevy's 5s budget — actual work happens after we return.
    background_tasks.add_task(_handle_webhook_workout, parsed["workout_id"], 0)
    return {"status": "accepted"}


@app.get("/v1/logs", dependencies=[Depends(verify_basic_auth)])
async def get_logs(source: str | None = None):
    """source: filter to "webhook", "polling", or "manual"; omit for all.
    Entries logged before this field existed have no "source" key and are
    only returned when no filter is applied."""
    with log_lock:
        logs = list(memory_logs)
        if settings.PERSIST_LOGS and os.path.exists(LOGS_FILE):
            try:
                with open(LOGS_FILE) as f:
                    logs = json.loads(f.read())
            except Exception:
                pass

    if source is not None:
        logs = [entry for entry in logs if entry.get("source") == source]
    return logs


@app.get("/v1/mappings/unmapped", dependencies=[Depends(verify_basic_auth)])
async def unmapped_exercises():
    rows = db.list_unmapped_exercises()
    return [dict(r) for r in rows]


@app.get("/v1/mappings/categories", dependencies=[Depends(verify_basic_auth)])
async def valid_categories():
    return sorted(set(CATEGORY_NAMES.values()))


@app.post("/v1/mappings", dependencies=[Depends(verify_basic_auth)])
async def save_mapping(body: MappingUpdate):
    try:
        mapper.save_override(body.template_id, body.category, body.note)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return {"status": "ok"}


@app.get("/v1/sync-history", dependencies=[Depends(verify_basic_auth)])
async def sync_history():
    return [dict(r) for r in db.recent_sync_history(limit=50)]


@app.post("/v1/mappings/learn-from-garmin/{hevy_workout_id}", dependencies=[Depends(verify_basic_auth)])
async def learn_from_garmin(hevy_workout_id: str, include_mapped: bool = False):
    """Reads back a user's manual 'Choose an Exercise' correction in Garmin
    Connect and turns it into a validated (category, name) override — see
    the 'learn from Garmin' feature plan. Unlike POST /v1/mappings, this can
    capture an exact name because it's read from Garmin's own confirmed
    state, never hand-guessed (see mapping.py's _validate_category_name_pair).

    `include_mapped` (default False, dashboard checkbox) widens the scan to
    also re-check template_ids that already have a specific mapping — the
    default excludes them since most bundled-catalog resolutions are correct,
    but a confidently-wrong one (real example: "Lateral Dumbbell Raise")
    is otherwise stuck exactly like an unmapped one, with no way back short
    of hand-editing exercise_mappings.json. Widening the scan can't bypass
    any of learn_mappings_from_garmin()'s existing safety nets — it only
    changes which template_ids are considered at all."""
    record = db.get_sync_record(hevy_workout_id)
    if record is None or record["garmin_activity_id"] is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No synced Garmin activity for this workout")
    activity_id = record["garmin_activity_id"]

    workout = hevy_client.fetch_workout(hevy_workout_id)
    if workout is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Hevy workout no longer exists")
    hevy_exercises = workout.get("exercises", [])

    try:
        client = get_garmin_client()
        garmin_exercise_sets = get_existing_exercise_sets(client, activity_id) or {}
    except GarminConnectAuthenticationError as e:
        reset_garmin_client()
        logger.error("Map from Garmin for workout %s aborted — Garmin session expired: %s", hevy_workout_id, e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Garmin session expired: {e}") from e

    garmin_sets_count = len(garmin_exercise_sets.get("exerciseSets", []))
    logger.info(
        "Map from Garmin: workout=%s activity=%s hevy_exercises=%d garmin_exercise_sets=%d include_mapped=%s",
        hevy_workout_id, activity_id, len(hevy_exercises), garmin_sets_count, include_mapped,
    )

    known_before = mapper.known_template_ids()
    already_known = set() if include_mapped else known_before

    learned = learn_mappings_from_garmin(
        hevy_exercises, garmin_exercise_sets,
        already_known_template_ids=already_known,
    )
    for lm in learned:
        if lm.template_id in known_before:
            old = mapper.resolve(lm.template_id, "")
            logger.info(
                "Map from Garmin for workout %s: correcting %s: %s/%s -> %s/%s",
                hevy_workout_id, lm.template_id, old.category, old.name, lm.category, lm.name,
            )
        mapper.save_override(lm.template_id, lm.category, note="learned from Garmin", name=lm.name)

    if learned:
        logger.info(
            "Map from Garmin for workout %s: learned %d mapping(s): %s",
            hevy_workout_id, len(learned),
            ", ".join(f"{lm.template_id}->{lm.category}/{lm.name}" for lm in learned),
        )
    else:
        logger.info(
            "Map from Garmin for workout %s: nothing learned (either every exercise's template_id is already "
            "known, the total ACTIVE set count didn't match Hevy's, or the returned name failed "
            "category/name validation — see any WARNING lines just above this one for which)",
            hevy_workout_id,
        )

    return {"learned": [lm.__dict__ for lm in learned]}
