from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_user: str = "app"
    postgres_password: str = "change-me"
    postgres_db: str = "investresearch"
    postgres_host: str = "db"
    postgres_port: int = 5432

    log_level: str = "INFO"
    jquants_api_key: str | None = None
    jquants_api_key_file: Path | None = None
    qlib_data_dir: Path = Path("var/qlib-data")
    research_artifact_dir: Path = Path("var/research-artifacts")
    qlib_disk_budget_bytes: int = 20 * 1024 * 1024 * 1024
    qlib_threads: int = 2

    @model_validator(mode="after")
    def validate_jquants_key_sources(self) -> "Settings":
        if self.jquants_api_key and self.jquants_api_key_file:
            raise ValueError("JQUANTS_API_KEY and JQUANTS_API_KEY_FILE are mutually exclusive")
        return self

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def resolve_jquants_api_key(settings: Settings | None = None) -> str | None:
    settings = settings or get_settings()
    if settings.jquants_api_key:
        return settings.jquants_api_key.strip() or None
    if settings.jquants_api_key_file:
        try:
            value = settings.jquants_api_key_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError("J-Quants API key file cannot be read") from exc
        if not value:
            raise ValueError("J-Quants API key file is empty")
        return value
    return None
