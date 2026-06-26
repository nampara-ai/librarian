"""Runtime configuration."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

CoherenceModeSetting = Literal["fast", "balanced", "max-coherence"]
OcrLlmCorrectionMode = Literal["always", "never", "low-confidence"]
OcrPreprocessMode = Literal["none", "grayscale", "threshold", "deskew"]
PdfEngineSetting = Literal["auto", "liteparse", "legacy"]
LiteParseImageMode = Literal["off", "placeholder", "embed"]
LogFormatSetting = Literal["json", "text"]
LogLevelSetting = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LlmProviderSetting = Literal["mock", "openai-compatible"]
CleaningPromptVersionSetting = Literal["cmos_v1", "cmos_v2"]
ClassificationPromptVersionSetting = Literal[
    "dewey_v1", "dewey_v2", "dewey_v3", "dewey_v4", "dewey_v5"
]
CleaningModeSetting = Literal["standard"]


class Settings(BaseSettings):
    """Librarian settings loaded from env and optional .env files."""

    model_config = SettingsConfigDict(
        env_prefix="LIBRARIAN_",
        env_file=".env",
        extra="ignore",
    )

    data_dir: Path = Field(default=Path(".librarian"))
    database_path: Path = Field(default=Path(".librarian/librarian.sqlite"))

    llm_provider: LlmProviderSetting = Field(default="mock")
    llm_model: str = Field(default="mock-cleaner")
    llm_base_url: str | None = Field(default=None)
    llm_api_key_env: str = Field(default="OPENAI_API_KEY")
    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    llm_max_concurrency: int = Field(default=8, gt=0)
    llm_max_retries: int = Field(default=5, ge=0)
    llm_retry_base_delay_seconds: float = Field(default=0.5, ge=0)
    llm_retry_max_delay_seconds: float = Field(default=10.0, ge=0)
    llm_prompt_cost_per_1k_tokens_usd: float = Field(default=0.0, ge=0)
    llm_completion_cost_per_1k_tokens_usd: float = Field(default=0.0, ge=0)
    llm_max_prompt_chars: int = Field(default=2 * 1024 * 1024, gt=0)
    llm_max_response_chars: int = Field(default=2 * 1024 * 1024, gt=0)

    cleaning_prompt_version: CleaningPromptVersionSetting = Field(default="cmos_v2")
    classification_prompt_version: ClassificationPromptVersionSetting = Field(default="dewey_v5")
    cleaning_mode: CleaningModeSetting = Field(default="standard")
    coherence_mode: CoherenceModeSetting = Field(default="balanced")

    chunk_target_chars: int = Field(default=12_000, gt=0)
    chunk_overlap_chars: int = Field(default=800, ge=0)
    max_source_bytes: int = Field(default=200 * 1024 * 1024, gt=0)
    text_max_input_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    docx_max_input_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    pdf_max_input_bytes: int = Field(default=200 * 1024 * 1024, gt=0)
    pdf_max_pages: int = Field(default=1_000, gt=0)
    # PDF/image extraction engine. "auto" uses the liteparse engine (richer
    # Markdown: tables, headings, figures, selective OCR) when the optional
    # liteparse package is installed, and otherwise falls back to the built-in
    # pdfplumber + Tesseract path. "liteparse" forces it (still falls back if
    # the package is absent); "legacy" always uses the built-in path.
    pdf_engine: PdfEngineSetting = Field(default="auto")
    liteparse_ocr_server_url: str | None = Field(default=None)
    liteparse_dpi: int = Field(default=150, gt=0)
    liteparse_image_mode: LiteParseImageMode = Field(default="placeholder")
    # Directory holding Tesseract traineddata for the liteparse engine's bundled
    # OCR. Set this to ship fully offline OCR (the Mac app points it at its
    # bundled tessdata); when unset, liteparse uses its own default lookup.
    liteparse_tessdata_path: str | None = Field(default=None)
    # Extraction throughput controls. The content-hash extraction cache stores
    # extracted Markdown keyed by file digest + extraction-config signature, so
    # re-ingesting unchanged files (or the same file across documents) skips the
    # expensive parser/OCR work. The timeout bounds a single extraction so one
    # pathological file cannot hang a batch (0 disables the ceiling).
    extraction_cache_enabled: bool = Field(default=True)
    extraction_timeout_seconds: int = Field(default=0, ge=0)
    # Vision-LLM enrichment of embedded figures/charts. When enabled (and the
    # liteparse engine is active), each embedded figure image is sent to a
    # vision-capable model that returns a description and, for charts, a data
    # table; the result is injected next to the figure's markdown placeholder so
    # the otherwise-lost chart data becomes searchable text. Off by default
    # because it requires a vision model and adds per-figure cost/latency.
    figure_vision_enabled: bool = Field(default=False)
    figure_vision_model: str | None = Field(default=None)
    figure_vision_max_figures: int = Field(default=20, gt=0)
    figure_vision_min_bytes: int = Field(default=2048, ge=0)
    figure_vision_max_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    figure_vision_max_concurrency: int = Field(default=2, gt=0)
    figure_vision_max_response_chars: int = Field(default=16 * 1024, gt=0)
    # How many files a directory import converts/ingests concurrently. The
    # expensive extraction step runs in worker threads, so parallel files
    # overlap their parser/OCR work. For PROCESS/QUEUE imports this also overlaps
    # per-document LLM work, multiplying with llm_max_concurrency; keep it modest
    # on rate-limited providers. 1 restores fully sequential imports.
    import_concurrency: int = Field(default=2, gt=0)
    ocr_language: str = Field(default="eng")
    ocr_timeout_seconds: int = Field(default=120, gt=0)
    ocr_pdf_dpi: int = Field(default=200, gt=0)
    ocr_pdf_max_pages: int = Field(default=1_000, gt=0)
    ocr_preprocess_mode: OcrPreprocessMode = Field(default="none")
    ocr_threshold: int = Field(default=180, ge=0, le=255)
    ocr_preserve_page_images: bool = Field(default=False)
    ocr_rotation_retry: bool = Field(default=False)
    ocr_llm_correction: OcrLlmCorrectionMode = Field(default="always")
    ocr_llm_model: str | None = Field(default=None)
    ocr_low_confidence_threshold: float = Field(default=85.0, ge=0, le=100)
    ocr_page_concurrency: int = Field(default=2, gt=0)
    ocr_fail_on_page_error: bool = Field(default=True)
    universal_max_input_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    universal_timeout_seconds: int = Field(default=120, gt=0)

    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8080, ge=1, le=65535)
    api_key: str | None = Field(default=None)
    api_keys: str | None = Field(default=None)
    api_key_sha256: str | None = Field(default=None)
    api_key_hashes: str | None = Field(default=None)
    api_import_root: Path | None = Field(default=None)
    api_max_request_bytes: int = Field(default=1024 * 1024 * 1024, gt=0)
    api_max_upload_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    api_max_batch_files: int = Field(default=100, gt=0)
    api_max_batch_bytes: int = Field(default=1024 * 1024 * 1024, gt=0)
    api_max_import_files: int = Field(default=1_000, gt=0)
    api_max_import_bytes: int = Field(default=1024 * 1024 * 1024, gt=0)
    api_max_import_manifest_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    api_max_content_chars: int = Field(default=2 * 1024 * 1024, gt=0)
    api_rate_limit_per_minute: int = Field(default=0, ge=0)
    api_trusted_proxy_cidrs: str | None = Field(default=None)
    api_audit_retention_days: int = Field(default=90, ge=0)
    log_level: LogLevelSetting = Field(default="INFO")
    log_format: LogFormatSetting = Field(default="json")
    metrics_enabled: bool = Field(default=True)
    otel_enabled: bool = Field(default=False)
    otel_service_name: str = Field(default="librarian")
    otel_endpoint: str | None = Field(default=None)
    otel_headers: str | None = Field(default=None)
    job_backend: Literal["in-process", "sqlite"] = Field(default="in-process")
    job_max_concurrency: int = Field(default=2, gt=0)
    job_worker_id: str = Field(default="worker-local")
    job_lease_seconds: int = Field(default=300, gt=0)
    job_max_attempts: int = Field(default=3, gt=0)

    @field_validator("api_trusted_proxy_cidrs")
    @classmethod
    def validate_api_trusted_proxy_cidrs(cls, value: str | None) -> str | None:
        """Validate comma-separated trusted proxy IP networks."""
        if value is None:
            return None
        networks = [
            str(ipaddress.ip_network(entry.strip(), strict=False))
            for entry in value.split(",")
            if entry.strip()
        ]
        return ",".join(networks) if networks else None

    @model_validator(mode="after")
    def derive_database_path(self) -> Self:
        """Keep the SQLite database inside data_dir unless database_path is set explicitly."""
        if "database_path" not in self.model_fields_set:
            self.database_path = self.data_dir / "librarian.sqlite"
        return self

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
