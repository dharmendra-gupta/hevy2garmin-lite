"""SQLite state — idempotency (Phase A) and unmapped-exercise tracking (Phase D)."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS synced_workouts (
    hevy_workout_id   TEXT PRIMARY KEY,
    garmin_activity_id INTEGER UNIQUE,
    sync_status        TEXT NOT NULL,  -- synced | synced_partial | no_watch_match | failed | source_deleted
    content_hash        TEXT,
    synced_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unmapped_exercises (
    template_id   TEXT PRIMARY KEY,
    exercise_name TEXT NOT NULL,
    first_seen_at  TEXT NOT NULL,
    occurrences     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sync_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- idempotency / sync state -----------------------------------------

    def get_sync_record(self, hevy_workout_id: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM synced_workouts WHERE hevy_workout_id = ?",
                (hevy_workout_id,),
            ).fetchone()

    def is_activity_claimed(self, garmin_activity_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM synced_workouts WHERE garmin_activity_id = ?",
                (garmin_activity_id,),
            ).fetchone()
            return row is not None

    def record_sync(
        self,
        hevy_workout_id: str,
        garmin_activity_id: int | None,
        sync_status: str,
        content_hash: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO synced_workouts
                    (hevy_workout_id, garmin_activity_id, sync_status, content_hash, synced_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(hevy_workout_id) DO UPDATE SET
                    garmin_activity_id = excluded.garmin_activity_id,
                    sync_status = excluded.sync_status,
                    content_hash = excluded.content_hash,
                    synced_at = excluded.synced_at
                """,
                (hevy_workout_id, garmin_activity_id, sync_status, content_hash, _now_iso()),
            )

    def mark_source_deleted(self, hevy_workout_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE synced_workouts SET sync_status = 'source_deleted', synced_at = ? "
                "WHERE hevy_workout_id = ?",
                (_now_iso(), hevy_workout_id),
            )

    def recent_sync_history(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM synced_workouts ORDER BY synced_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

    # --- unmapped exercises (Phase D) ---------------------------------------

    def record_unmapped_exercise(self, template_id: str | None, exercise_name: str) -> None:
        key = template_id or f"__no_template_id__:{exercise_name}"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO unmapped_exercises (template_id, exercise_name, first_seen_at, occurrences)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(template_id) DO UPDATE SET
                    occurrences = occurrences + 1
                """,
                (key, exercise_name, _now_iso()),
            )

    def clear_unmapped_exercise(self, template_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM unmapped_exercises WHERE template_id = ?", (template_id,))

    def list_unmapped_exercises(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM unmapped_exercises ORDER BY occurrences DESC"
            ).fetchall()

    # --- polling cursor (optimization only, not idempotency source) --------

    def get_last_poll_timestamp(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM sync_meta WHERE key = 'last_poll_timestamp'"
            ).fetchone()
            return row["value"] if row else None

    def set_last_poll_timestamp(self, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sync_meta (key, value) VALUES ('last_poll_timestamp', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (value,),
            )

    # --- polling toggle (default off; dashboard-controlled) -----------------

    def get_polling_enabled(self, default: bool) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM sync_meta WHERE key = 'polling_enabled'"
            ).fetchone()
            if row is None:
                return default
            return row["value"] == "true"

    def set_polling_enabled(self, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sync_meta (key, value) VALUES ('polling_enabled', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("true" if enabled else "false",),
            )

    # --- polling interval (default from SYNC_INTERVAL_MINUTES; dashboard-controlled) ---

    def get_polling_interval_minutes(self, default: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM sync_meta WHERE key = 'polling_interval_minutes'"
            ).fetchone()
            if row is None:
                return default
            return int(row["value"])

    def set_polling_interval_minutes(self, minutes: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sync_meta (key, value) VALUES ('polling_interval_minutes', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(minutes),),
            )

    # --- timeline synthesis tuning (defaults from Settings; dashboard-controlled) ---

    def _get_meta_int(self, key: str, default: int) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM sync_meta WHERE key = ?", (key,)).fetchone()
            return int(row["value"]) if row else default

    def _set_meta_int(self, key: str, value: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sync_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def get_working_set_seconds(self, default: int) -> int:
        return self._get_meta_int("working_set_seconds", default)

    def set_working_set_seconds(self, value: int) -> None:
        self._set_meta_int("working_set_seconds", value)

    def get_warmup_set_seconds(self, default: int) -> int:
        return self._get_meta_int("warmup_set_seconds", default)

    def set_warmup_set_seconds(self, value: int) -> None:
        self._set_meta_int("warmup_set_seconds", value)

    def get_rest_between_sets_seconds(self, default: int) -> int:
        return self._get_meta_int("rest_between_sets_seconds", default)

    def set_rest_between_sets_seconds(self, value: int) -> None:
        self._set_meta_int("rest_between_sets_seconds", value)

    def get_rest_between_exercises_seconds(self, default: int) -> int:
        return self._get_meta_int("rest_between_exercises_seconds", default)

    def set_rest_between_exercises_seconds(self, value: int) -> None:
        self._set_meta_int("rest_between_exercises_seconds", value)
