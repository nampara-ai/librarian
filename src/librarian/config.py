"""Runtime configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

CoherenceModeSetting = Literal["fast", "balanced", "max-coherence"]
OcrLlmCorrectionMode = Literal["always", "never", "low-confidence"]


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
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    llm_max_concurrency: int = Field(default=8, gt=0)
    llm_max_retries: int = Field(default=5, ge=0)
    llm_retry_base_delay_seconds: float = Field(default=0.5, ge=0)
    llm_retry_max_delay_seconds: float = Field(default=10.0, ge=0)

    cleaning_prompt_version: str = Field(default="cmos_v1")
    classification_prompt_version: str = Field(default="dewey_v1")
    cleaning_mode: str = Field(default="standard")
    coherence_mode: CoherenceModeSetting = Field(default="balanced")

    chunk_target_chars: int = Field(default=12_000, gt=0)
    chunk_overlap_chars: int = Field(default=800, ge=0)
    max_source_bytes: int = Field(default=200 * 1024 * 1024, gt=0)
    text_max_input_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    docx_max_input_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    pdf_max_input_bytes: int = Field(default=200 * 1024 * 1024, gt=0)
    pdf_max_pages: int = Field(default=1_000, gt=0)
    ocr_language: str = Field(default="eng")
    ocr_timeout_seconds: int = Field(default=120, gt=0)
    ocr_pdf_dpi: int = Field(default=200, gt=0)
    ocr_pdf_max_pages: int = Field(default=1_000, gt=0)
    ocr_llm_correction: OcrLlmCorrectionMode = Field(default="always")
    ocr_llm_model: str | None = Field(default=None)
    ocr_page_concurrency: int = Field(default=2, gt=0)
    ocr_fail_on_page_error: bool = Field(default=True)
    universal_max_input_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    universal_timeout_seconds: int = Field(default=120, gt=0)

    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8080, ge=1, le=65535)
    api_key: str | None = Field(default=None)
    api_import_root: Path | None = Field(default=None)
    api_max_upload_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="json")
    metrics_enabled: bool = Field(default=True)
    job_backend: Literal["in-process", "sqlite"] = Field(default="in-process")
    job_max_concurrency: int = Field(default=2, gt=0)
    job_worker_id: str = Field(default="worker-local")
    job_lease_seconds: int = Field(default=300, gt=0)
    job_max_attempts: int = Field(default=3, gt=0)

    @model_validator(mode="after")
    def validate_cross_field_settings(self) -> Self:
        """Validate settings that depend on each other."""
        if self.chunk_overlap_chars >= self.chunk_target_chars:
            raise ValueError("chunk_overlap_chars must be smaller than chunk_target_chars")
        if self.llm_retry_max_delay_seconds < self.llm_retry_base_delay_seconds:
            raise ValueError(
                "llm_retry_max_delay_seconds must be greater than or equal to "
                "llm_retry_base_delay_seconds"
            )
        return self
