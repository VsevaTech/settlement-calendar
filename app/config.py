"""Application settings (environment driven, no secrets required)."""

from __future__ import annotations

import os
from datetime import date
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.services.settlement_calendar import parse_date, parse_weekend_days


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SC_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/settlement_calendar.db"
    as_of_date: str = ""
    weekend_days: str = "6,7"
    app_title: str = "Settlement Calendar"

    @property
    def resolved_as_of_date(self) -> date:
        """The "today" used for status calculation.

        Empty ``SC_AS_OF_DATE`` means "use the real current date"; setting it
        pins the clock so the bundled demo dataset reproduces exactly.
        """
        if self.as_of_date.strip():
            return parse_date(self.as_of_date.strip())
        return date.today()

    @property
    def resolved_weekend_days(self) -> frozenset[int]:
        return parse_weekend_days(self.weekend_days)

    @property
    def sqlite_path(self) -> str | None:
        if self.database_url.startswith("sqlite:///"):
            return self.database_url.removeprefix("sqlite:///")
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()


def ensure_sqlite_directory(settings: Settings) -> None:
    path = settings.sqlite_path
    if not path:
        return
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
