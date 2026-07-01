import os
from pathlib import Path
from typing import cast

import pytest

from librarian.application.factory import build_container
from librarian.config import LlmProviderSetting, Settings
from librarian.ingest import extractors
from librarian.ingest.extractors import OcrTextResult, PdfExtractor
from librarian.llm import LazyLLMProvider
from librarian.maintainer.eval import EvalCase, EvalSuite, load_eval_suite, run_eval_suite

pytestmark = pytest.mark.skipif(
    os.environ.get("LIBRARIAN_RUN_PROVIDER_TESTS") != "1",
    reason="set LIBRARIAN_RUN_PROVIDER_TESTS=1 with provider credentials to run",
)

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"


def _provider_settings(tmp_path: Path) -> Settings:
    provider = os.environ.get("LIBRARIAN_LLM_PROVIDER", "openai-compatible")
    if provider not in {"mock", "openai-compatible"}:
        raise ValueError("LIBRARIAN_LLM_PROVIDER must be mock or openai-compatible")
    return Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        llm_provider=cast(LlmProviderSetting, provider),
        llm_model=os.environ.get("LIBRARIAN_LLM_MODEL", "gpt-4.1-mini"),
        llm_base_url=os.environ.get("LIBRARIAN_LLM_BASE_URL") or None,
        llm_api_key_env=os.environ.get("LIBRARIAN_LLM_API_KEY_ENV", "OPENAI_API_KEY"),
        llm_timeout_seconds=float(os.environ.get("LIBRARIAN_LLM_TIMEOUT_SECONDS", "120")),
        llm_max_concurrency=1,
        llm_max_retries=2,
        llm_retry_base_delay_seconds=0,
        llm_retry_max_delay_seconds=0,
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )


@pytest.mark.asyncio
async def test_real_provider_eval_and_prompt_provenance(tmp_path: Path) -> None:
    settings = _provider_settings(tmp_path)
    container = await build_container(settings)
    suite = EvalSuite(
        cases=[
            EvalCase(
                name="provider horse transcript",
                tags=["provider", "cleaning", "classification"],
                input_text=(
                    "Speaker 1: Um today we worked on canter transitions, saddle fit, "
                    "and keeping the horse relaxed through the corner."
                ),
                expected_contains=["canter transitions", "saddle fit"],
                expected_classification_prefix="636",
                min_output_chars=60,
            )
        ]
    )

    result = await run_eval_suite(container, suite)

    assert result.passed
    assert result.provider == "openai-compatible"
    assert result.model == settings.llm_model

    source = tmp_path / "provider-horse.md"
    source.write_text(suite.cases[0].input_text, encoding="utf-8")
    ingested = await container.ingest_document.execute(source)
    await container.process_document.execute(ingested.document.id)
    output = await container.repository.get_cleaned_output(ingested.document.id)

    assert output is not None
    assert output.prompt_version == settings.cleaning_prompt_version
    assert output.model_provider == "openai-compatible"
    assert output.model_name == settings.llm_model


@pytest.mark.asyncio
async def test_real_provider_shipped_v2_eval_suite(tmp_path: Path) -> None:
    settings = _provider_settings(tmp_path)
    container = await build_container(settings)
    result = await run_eval_suite(container, load_eval_suite(EXAMPLES_DIR / "eval_cases.json"))

    assert result.passed
    assert result.provider == "openai-compatible"
    assert result.cleaning_prompt_version == "cmos_v3"
    assert result.classification_prompt_version == "dewey_v5"


@pytest.mark.asyncio
async def test_real_provider_ocr_low_confidence_correction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _provider_settings(tmp_path)
    path = tmp_path / "fixture.pdf"
    path.write_bytes(b"%PDF")

    class FakePage:
        @staticmethod
        def extract_text() -> None:
            return None

    class FakePdf:
        pages = [FakePage()]

        def __enter__(self) -> "FakePdf":
            return self

        def __exit__(self, *args: object) -> None:
            del args

    class FakePdfPlumber:
        @staticmethod
        def open(path: Path) -> FakePdf:
            del path
            return FakePdf()

    def fake_import_module(name: str) -> object:
        if name == "pdfplumber":
            return FakePdfPlumber
        return __import__(name)

    def fake_ocr_pdf_page(*args: object, **kwargs: object) -> OcrTextResult:
        del args, kwargs
        return OcrTextResult(
            text="Sadd1e fit and canter transit10ns for the horse.",
            confidence=40.0,
        )

    monkeypatch.setattr(extractors.importlib, "import_module", fake_import_module)
    monkeypatch.setattr("librarian.ingest.extractors._ocr_pdf_page", fake_ocr_pdf_page)
    extractor = PdfExtractor(
        ocr_correction_provider=LazyLLMProvider(settings),
        ocr_correction_mode="low-confidence",
        ocr_low_confidence_threshold=85,
        ocr_correction_model=settings.ocr_llm_model or settings.llm_model,
    )

    text = await extractor.extract(path)

    assert "horse" in text.lower()
    assert "corrected: true" in text
    assert extractor.last_metadata is not None
    pages = extractor.last_metadata["pages"]
    assert isinstance(pages, list)
    assert pages[0]["confidence"] == 40.0
