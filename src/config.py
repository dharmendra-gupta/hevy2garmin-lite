from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    HEVY_API_KEY: str = ""

    GARMIN_TOKEN_SOURCE_DIR: str = "/app/garmin_tokens_source"
    # Self-healing auth (mirrors garmin-scale-sync's own pattern): used only
    # when the shared token store is rejected by the API and needs a full
    # credential re-login. Never used for routine sync — see garmin_client.py.
    GARMIN_EMAIL: str = ""
    GARMIN_PASSWORD: str = ""

    # --- Hevy webhook (fast primary trigger for new workouts) ---
    # Hevy only fires this on workout.created, never on edits/deletes, so
    # polling stays as a slower reconciliation safety net (default OFF,
    # toggleable from the dashboard).
    HEVY_WEBHOOK_AUTH_TOKEN: str = ""  # secret we generate; sent back as Authorization header by Hevy
    PUBLIC_BASE_URL: str = ""  # e.g. https://yourdomain.example — required to register the webhook
    WEBHOOK_RETRY_DELAYS_MINUTES: str = "5,10,15"  # comma-separated; handles watch->Garmin Connect sync lag

    MATCH_TOLERANCE_MINUTES: int = 15
    SYNC_INTERVAL_MINUTES: int = 15
    # Polling is a reconciliation safety net (edits/deletes, missed webhooks),
    # not the primary trigger. Off by default; toggle from the dashboard.
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

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def db_path(self) -> Path:
        return Path(self.DATA_DIR) / "hevy2garmin_lite.db"

    @property
    def override_mappings_path(self) -> Path:
        return Path(self.DATA_DIR) / "exercise_mappings.json"

    @property
    def webhook_retry_delays_minutes(self) -> list[int]:
        return [int(x.strip()) for x in self.WEBHOOK_RETRY_DELAYS_MINUTES.split(",") if x.strip()]


settings = Settings()

if not os.path.exists(settings.DATA_DIR):
    os.makedirs(settings.DATA_DIR, exist_ok=True)
