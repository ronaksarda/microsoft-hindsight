"""Centralised settings loaded from environment / .env file."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # --- LLM (Groq) ---
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_timeout_seconds: float = 20.0
    groq_max_retries: int = 3
    groq_backoff_base_seconds: float = 0.5
    groq_backoff_max_seconds: float = 8.0

    # --- Vectorize Hindsight ---
    hindsight_api_key: str = ""
    # Root of the API. Paths such as /v1/default/banks/{bank}/memories are appended.
    hindsight_base_url: str = "https://api.hindsight.vectorize.io"
    hindsight_bank_id: str = "grantanchor-ops-bank"
    hindsight_timeout_seconds: float = 5.0
    hindsight_recall_max_tokens: int = 2048
    hindsight_recall_budget: Literal["low", "mid", "high"] = "low"

    # --- Outbox (Hindsight retry queue) ---
    outbox_enabled: bool = True
    outbox_interval_seconds: float = 15.0
    outbox_max_attempts: int = 8
    outbox_backoff_base_seconds: float = 5.0
    outbox_backoff_max_seconds: float = 900.0

    # --- Storage ---
    storage_backend: Literal["json", "sqlite"] = "json"
    data_dir: Path = BASE_DIR / "data"
    json_store_filename: str = "grant_memory_store.json"
    sqlite_filename: str = "grantanchor.sqlite3"
    seed_path: Path = BASE_DIR / "data" / "seed_state.json"

    # --- Security ---
    # Comma-separated key lists. When both are empty, auth is disabled (local development).
    admin_api_keys: str = ""
    auditor_api_keys: str = ""
    api_key_header: str = "X-API-Key"

    # --- HTTP hardening ---
    cors_origins: str = "*"
    rate_limit_per_minute: int = 120
    rate_limit_burst: int = 30
    max_request_bytes: int = 64 * 1024
    log_level: str = "INFO"
    log_json: bool = True

    port: int = 8000

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("hindsight_base_url", "groq_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("hindsight_base_url")
    @classmethod
    def _strip_version_suffix(cls, v: str) -> str:
        # Earlier configs used ".../v1" as the base; the client appends /v1 itself.
        return v[: -len("/v1")] if v.endswith("/v1") else v

    @property
    def admin_keys(self) -> set[str]:
        return {k.strip() for k in self.admin_api_keys.split(",") if k.strip()}

    @property
    def auditor_keys(self) -> set[str]:
        return {k.strip() for k in self.auditor_api_keys.split(",") if k.strip()}

    @property
    def auth_enabled(self) -> bool:
        return bool(self.admin_keys or self.auditor_keys)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def llm_enabled(self) -> bool:
        return bool(self.groq_api_key)

    @property
    def hindsight_enabled(self) -> bool:
        return bool(self.hindsight_api_key)


settings = Settings()
