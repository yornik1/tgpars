"""Application configuration, loaded from environment / .env."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Values come from the environment or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Telegram API (https://my.telegram.org)
    tg_api_id: int = Field(..., alias="TG_API_ID")
    tg_api_hash: str = Field(..., alias="TG_API_HASH")
    tg_phone: str = Field("", alias="TG_PHONE")
    tg_session_path: str = Field("sessions/tgpars", alias="TG_SESSION_PATH")

    # One-time login (non-interactive). Filled in only while authorising:
    # the code Telegram sends to your app, and your 2FA password if enabled.
    tg_login_code: str = Field("", alias="TG_LOGIN_CODE")
    tg_2fa_password: str = Field("", alias="TG_2FA_PASSWORD")

    # Monitoring targets (raw comma-separated string; see target_chats for parsed form).
    tg_target_chats_raw: str = Field("", alias="TG_TARGET_CHATS")

    # Storage
    database_url: str = Field("sqlite:///data/tgpars.db", alias="DATABASE_URL")

    # Politeness
    flood_sleep_threshold: int = Field(60, alias="FLOOD_SLEEP_THRESHOLD")

    @property
    def target_chats(self) -> list[str]:
        """Target chats as a cleaned list of identifiers (usernames / links / ids)."""
        return [c.strip() for c in self.tg_target_chats_raw.split(",") if c.strip()]


def load_settings() -> Settings:
    """Load settings, raising a clear error if required values are missing."""
    return Settings()  # type: ignore[call-arg]
