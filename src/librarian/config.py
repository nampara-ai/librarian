"""Runtime configuration."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Librarian settings loaded from env and optional .env files."""

    model_config = SettingsConfigDict(
        env_prefix="LIBRARIAN_",
        env_file=".env",
        extra="ignore",
    )

    data_dir: Path = Field(default=Path(".librarian"))
    database_path: Path = Field(default=Path(".librarian/librarian.sqlite"))

    llm_provider: str = Field(default="mock")
    llm_model: str = Field(default="mock-cleaner")
    llm_base_url: str | None = Field(default=None)
    llm_api_key_env: str = Field(default="OPENAI_API_KEY")
    llm_timeout_seconds: float = Field(default=120.0)
    llm_max_concurrency: int = Field(default=8)
    llm_max_retries: int = Field(default=5)
    llm_retry_base_delay_seconds: float = Field(default=0.5)
    llm_retry_max_delay_seconds: float = Field(default=10.0)

    cleaning_prompt_version: str = Field(default="cmos_v1")
    classification_prompt_version: str = Field(default="dewey_v1")
    cleaning_mode: str = Field(default="standard")
    coherence_mode: str = Field(default="balanced")

    chunk_target_chars: int = Field(default=12_000)
    chunk_overlap_chars: int = Field(default=800)

    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8080)
    api_key: str | None = Field(default=None)
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="json")
    metrics_enabled: bool = Field(default=True)
    job_backend: str = Field(default="in-process")
    job_max_concurrency: int = Field(default=2)
    job_worker_id: str = Field(default="worker-local")
    job_lease_seconds: int = Field(default=300)
    job_max_attempts: int = Field(default=3)
