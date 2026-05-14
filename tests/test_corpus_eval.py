import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from librarian.application import corpus_eval
from librarian.application.corpus_eval import (
    CorpusEvalCase,
    CorpusEvalSuite,
    MarkdownTableExpectation,
    TextOrderExpectation,
    TextPatternExpectation,
    corpus_eval_result_json,
    load_corpus_eval_suite,
    run_corpus_eval_suite,
)
from librarian.application.factory import build_container
from librarian.config import Settings

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"


@pytest.mark.asyncio
async def test_corpus_eval_runs_conversion_processing_and_search(tmp_path: Path) -> None:
    source = tmp_path / "horse-transcript.md"
    source.write_text(
        "# Horse Transcript\n\n"
        "Speaker: Today we practiced canter transitions, saddle fit, and groundwork.",
        encoding="utf-8",
    )
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    container = await build_container(settings)
    suite = CorpusEvalSuite(
        cases=[
            CorpusEvalCase(
                name="horse transcript",
                source_path=source,
                expected_contains=["canter transitions", "saddle fit"],
                expected_search_phrases=["canter transitions"],
                expected_classification_prefix="636",
                max_conversion_seconds=30,
                max_processing_seconds=30,
                max_peak_memory_bytes=1_000_000_000,
                require_markdown_headings=True,
            )
        ]
    )

    result = await run_corpus_eval_suite(
        container,
        suite,
        output_dir=tmp_path / "converted",
    )

    assert result.passed
    assert result.cases[0].conversion_seconds > 0
    assert result.cases[0].processing_seconds is not None
    assert result.cases[0].peak_memory_bytes > 0
    assert result.cases[0].search_recall == 1
    assert result.cases[0].search_diagnostics[0].phrase == "canter transitions"
    assert result.cases[0].search_diagnostics[0].hit is True
    assert result.cases[0].search_diagnostics[0].total_results == 1
    assert result.cases[0].classification_code == "636.1"
    rendered = json.loads(corpus_eval_result_json(result))
    assert rendered["artifact_type"] == "librarian-corpus-eval-result"
    assert rendered["evidence_tier"] == "mock-smoke"
    assert rendered["librarian_version"]
    assert rendered["llm_provider"] == "mock"
    assert rendered["llm_model"] == "mock-cleaner"
    assert rendered["cleaning_prompt_version"] == "cmos_v2"
    assert rendered["classification_prompt_version"] == "dewey_v2"
    assert rendered["generated_at"].endswith("+00:00")
    assert rendered["summary"]["case_count"] == 1
    assert rendered["summary"]["passed_count"] == 1
    assert rendered["summary"]["failed_count"] == 0
    assert rendered["summary"]["pass_rate"] == 1
    assert rendered["summary"]["failure_count"] == 0
    assert rendered["summary"]["failure_case_count"] == 0
    assert rendered["summary"]["total_input_bytes"] == source.stat().st_size
    assert rendered["summary"]["total_output_chars"] == result.cases[0].output_chars
    assert rendered["summary"]["total_page_attempts"] == 0
    assert rendered["summary"]["total_failed_pages"] == 0
    assert rendered["summary"]["max_page_duration_ms"] is None
    assert rendered["summary"]["average_search_recall"] == 1
    assert rendered["summary"]["total_search_phrases"] == 1
    assert rendered["summary"]["total_search_hits"] == 1
    assert rendered["cases"][0]["search_diagnostics"][0]["hit"] is True
    assert rendered["cases"][0]["search_diagnostics"][0]["total_results"] == 1
    assert rendered["cases"][0]["search_diagnostics"][0]["error"] is None
    assert "output_char_ratio" in rendered["cases"][0]


@pytest.mark.asyncio
async def test_corpus_eval_fails_performance_budgets(tmp_path: Path) -> None:
    source = tmp_path / "horse-transcript.md"
    source.write_text("# Horse Transcript\n\nSaddle fit notes.", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)
    suite = CorpusEvalSuite(
        cases=[
            CorpusEvalCase(
                name="horse transcript",
                source_path=source,
                process=False,
                max_conversion_seconds=1e-12,
                max_processing_seconds=1,
                max_peak_memory_bytes=1,
                expected_contains=["Saddle fit"],
            )
        ]
    )

    result = await run_corpus_eval_suite(
        container,
        suite,
        output_dir=tmp_path / "converted",
    )

    assert not result.passed
    failures = "\n".join(result.cases[0].failures)
    assert "conversion_seconds" in failures
    assert "max_processing_seconds requires process=true" in failures
    assert "peak_memory_bytes" in failures


@pytest.mark.asyncio
async def test_corpus_eval_checks_expected_text_order(tmp_path: Path) -> None:
    source = tmp_path / "order.md"
    source.write_text(
        "# Order Fixture\n\nSecond topic appears first.\n\nFirst topic appears later.",
        encoding="utf-8",
    )
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)
    suite = CorpusEvalSuite(
        cases=[
            CorpusEvalCase(
                name="order fixture",
                source_path=source,
                process=False,
                expected_text_order=[
                    TextOrderExpectation(
                        before="First topic",
                        after="Second topic",
                    )
                ],
            )
        ]
    )

    result = await run_corpus_eval_suite(
        container,
        suite,
        output_dir=tmp_path / "converted",
    )

    assert not result.passed
    assert result.cases[0].failures == (
        "order check failed: 'First topic' does not appear before 'Second topic'",
    )


@pytest.mark.asyncio
async def test_corpus_eval_checks_patterns_and_table_relationships(tmp_path: Path) -> None:
    source = tmp_path / "relations.md"
    source.write_text(
        "# Relations\n\n"
        "| Finding | Evidence |\n"
        "| --- | --- |\n"
        "| Table checkpoint | canter transitions |\n"
        "| Footnote | [^1] |\n\n"
        "The formula is E = mc^2.\n\n"
        "[^1]: Preserve source footnotes.",
        encoding="utf-8",
    )
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)
    suite = CorpusEvalSuite(
        cases=[
            CorpusEvalCase(
                name="relations",
                source_path=source,
                process=False,
                expected_patterns=[
                    TextPatternExpectation(
                        pattern=r"E\s*=\s*mc\^?2",
                        description="math formula",
                    ),
                    TextPatternExpectation(
                        pattern=r"\[\^1\]: Preserve source footnotes\.",
                        description="footnote definition",
                    ),
                ],
                expected_table_rows=[
                    MarkdownTableExpectation(
                        cells=["Table checkpoint", "canter transitions"],
                    )
                ],
                expected_table_columns=[
                    MarkdownTableExpectation(
                        cells=["Table checkpoint", "Footnote"],
                    )
                ],
            )
        ]
    )

    result = await run_corpus_eval_suite(
        container,
        suite,
        output_dir=tmp_path / "converted",
    )

    assert result.passed


@pytest.mark.asyncio
async def test_corpus_eval_reports_missing_patterns_and_table_relationships(
    tmp_path: Path,
) -> None:
    source = tmp_path / "broken-relations.md"
    source.write_text(
        "# Broken Relations\n\n"
        "| Finding | Evidence |\n"
        "| --- | --- |\n"
        "| Table checkpoint | unrelated |\n",
        encoding="utf-8",
    )
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)
    suite = CorpusEvalSuite(
        cases=[
            CorpusEvalCase(
                name="broken relations",
                source_path=source,
                process=False,
                expected_patterns=[
                    TextPatternExpectation(
                        pattern=r"\[\^1\]: Preserve source footnotes\.",
                        description="footnote definition",
                    )
                ],
                expected_table_rows=[
                    MarkdownTableExpectation(
                        cells=["Table checkpoint", "canter transitions"],
                    )
                ],
                expected_table_columns=[
                    MarkdownTableExpectation(
                        cells=["Table checkpoint", "Footnote"],
                    )
                ],
            )
        ]
    )

    result = await run_corpus_eval_suite(
        container,
        suite,
        output_dir=tmp_path / "converted",
    )

    assert not result.passed
    failures = "\n".join(result.cases[0].failures)
    assert "missing expected pattern: footnote definition" in failures
    assert "table row missing related cells: 'Table checkpoint', 'canter transitions'" in failures
    assert "table column missing related cells: 'Table checkpoint', 'Footnote'" in failures


@pytest.mark.asyncio
async def test_corpus_eval_fails_page_source_and_ocr_expectations(tmp_path: Path) -> None:
    class MetadataExtractor:
        supported_extensions = frozenset({".pdf"})
        last_metadata: dict[str, object] | None = None

        async def extract(self, path: Path) -> str:
            del path
            self.last_metadata = {
                "artifact_type": "pdf-page-extraction",
                "page_count": 2,
                "pages": [
                    {
                        "page_number": 1,
                        "status": "succeeded",
                        "source": "embedded",
                        "chars": 12,
                        "corrected": False,
                        "warnings": [],
                        "attempts": 0,
                        "duration_ms": None,
                    },
                    {
                        "page_number": 2,
                        "status": "pending",
                        "source": "empty",
                        "chars": 0,
                        "corrected": False,
                        "warnings": ["missing-ocr-confidence"],
                        "attempts": 2,
                        "duration_ms": 15.0,
                    },
                ],
            }
            return "# PDF\n\nExtracted text"

    source = tmp_path / "mixed.pdf"
    source.write_bytes(b"%PDF")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)
    object.__setattr__(container.ingest_document, "extractor", MetadataExtractor())
    suite = CorpusEvalSuite(
        cases=[
            CorpusEvalCase(
                name="mixed pdf",
                source_path=source,
                process=False,
                expected_contains=["Extracted text"],
                expected_page_source_counts={"embedded": 1, "ocr": 1},
                min_ocr_pages=1,
                min_corrected_pages=1,
            )
        ]
    )

    result = await run_corpus_eval_suite(
        container,
        suite,
        output_dir=tmp_path / "converted",
    )

    assert not result.passed
    failures = "\n".join(result.cases[0].failures)
    assert "page source 'ocr' count 0 != expected 1" in failures
    assert "ocr_pages 0 < minimum 1" in failures
    assert "corrected_pages 0 < minimum 1" in failures
    rendered = json.loads(corpus_eval_result_json(result))
    assert rendered["cases"][0]["page_status_counts"] == {
        "pending": 1,
        "succeeded": 1,
    }
    assert rendered["cases"][0]["page_warning_counts"] == {
        "missing-ocr-confidence": 1,
    }
    assert rendered["cases"][0]["page_attempts"] == 2
    assert rendered["cases"][0]["max_page_duration_ms"] == 15.0
    assert rendered["summary"]["total_page_attempts"] == 2


@pytest.mark.asyncio
async def test_corpus_eval_rejects_mismatched_page_summary(tmp_path: Path) -> None:
    class MetadataExtractor:
        supported_extensions = frozenset({".pdf"})
        last_metadata: dict[str, object] | None = None

        async def extract(self, path: Path) -> str:
            del path
            self.last_metadata = {
                "artifact_type": "pdf-page-extraction",
                "page_count": 1,
                "summary": {
                    "status": "succeeded",
                    "status_counts": {"failed": 0, "pending": 0, "succeeded": 1},
                    "source_counts": {"ocr": 1},
                    "warning_counts": {},
                    "attempts": 99,
                    "ocr_pages": 1,
                    "corrected_pages": 0,
                    "average_ocr_confidence": 88.0,
                    "max_page_duration_ms": 10.0,
                },
                "pages": [
                    {
                        "page_number": 1,
                        "status": "succeeded",
                        "source": "ocr",
                        "chars": 16,
                        "confidence": 88.0,
                        "corrected": False,
                        "warnings": [],
                        "attempts": 1,
                        "duration_ms": 10.0,
                    }
                ],
            }
            return "# PDF\n\nExtracted text"

    source = tmp_path / "summary.pdf"
    source.write_bytes(b"%PDF")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)
    object.__setattr__(container.ingest_document, "extractor", MetadataExtractor())
    suite = CorpusEvalSuite(
        cases=[
            CorpusEvalCase(
                name="summary mismatch",
                source_path=source,
                process=False,
                expected_contains=["Extracted text"],
            )
        ]
    )

    result = await run_corpus_eval_suite(
        container,
        suite,
        output_dir=tmp_path / "converted",
    )

    assert not result.passed
    assert result.cases[0].failures == ("page summary attempts 99 != expected 1",)


@pytest.mark.asyncio
async def test_corpus_eval_redacts_conversion_failures(tmp_path: Path) -> None:
    class FailingExtractor:
        supported_extensions = frozenset({".txt"})

        async def extract(self, path: Path) -> str:
            del path
            raise RuntimeError("extract failed api_key=abc123 sk-testSECRET123")

    source = tmp_path / "secret-failure.txt"
    source.write_text("Saddle fit notes.", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)
    object.__setattr__(container.ingest_document, "extractor", FailingExtractor())
    suite = CorpusEvalSuite(
        cases=[
            CorpusEvalCase(
                name="secret failure",
                source_path=source,
                process=False,
            )
        ]
    )

    result = await run_corpus_eval_suite(
        container,
        suite,
        output_dir=tmp_path / "converted",
    )

    assert not result.passed
    failure = result.cases[0].failures[0]
    assert failure == (
        "conversion failed (extraction_failed): "
        "extract failed api_key=[REDACTED] [REDACTED]"
    )
    assert "abc123" not in failure
    assert "sk-testSECRET123" not in failure
    rendered = json.loads(corpus_eval_result_json(result))
    assert "abc123" not in json.dumps(rendered)
    assert "sk-testSECRET123" not in json.dumps(rendered)


@pytest.mark.asyncio
async def test_corpus_eval_rejects_symlink_output_directory(tmp_path: Path) -> None:
    source = tmp_path / "horse-transcript.md"
    source.write_text("# Horse Transcript\n\nSaddle fit notes.", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    output_dir = tmp_path / "converted"
    output_dir.symlink_to(outside, target_is_directory=True)
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)
    suite = CorpusEvalSuite(
        cases=[
            CorpusEvalCase(
                name="horse transcript",
                source_path=source,
                process=False,
                expected_contains=["Saddle fit"],
            )
        ]
    )

    with pytest.raises(ValueError, match="output directory crosses symlink"):
        await run_corpus_eval_suite(container, suite, output_dir=output_dir)

    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_corpus_eval_rejects_symlinked_output_directory_parent(tmp_path: Path) -> None:
    source = tmp_path / "horse-transcript.md"
    source.write_text("# Horse Transcript\n\nSaddle fit notes.", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)
    suite = CorpusEvalSuite(
        cases=[
            CorpusEvalCase(
                name="horse transcript",
                source_path=source,
                process=False,
                expected_contains=["Saddle fit"],
            )
        ]
    )

    with pytest.raises(ValueError, match="output directory crosses symlink"):
        await run_corpus_eval_suite(container, suite, output_dir=linked_parent / "converted")

    assert list(outside.iterdir()) == []


def test_load_corpus_eval_suite_resolves_relative_paths(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("Library science notes", encoding="utf-8")
    suite_path = tmp_path / "corpus.json"
    suite_path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "name": "notes",
                        "source_path": "notes.txt",
                        "process": False,
                        "expected_contains": ["Library"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    suite = load_corpus_eval_suite(suite_path)

    assert suite.cases[0].source_path == source.resolve()


def test_load_corpus_eval_suite_rejects_empty_cases(tmp_path: Path) -> None:
    suite_path = tmp_path / "corpus.json"
    suite_path.write_text('{"cases": []}', encoding="utf-8")

    with pytest.raises(ValueError):
        load_corpus_eval_suite(suite_path)


def test_shipped_synthetic_corpus_suite_covers_conversion_formats() -> None:
    suite = load_corpus_eval_suite(EXAMPLES_DIR / "synthetic-corpus" / "corpus_eval_cases.json")
    tags = {tag for case in suite.cases for tag in case.tags}

    assert len(suite.cases) >= 10
    assert {
        "long-document",
        "docx",
        "pdf",
        "embedded-text",
        "scanned",
        "mixed-embedded-scanned",
        "noisy-ocr",
        "ocr",
        "transcript-caption",
        "srt",
        "vtt",
        "tables",
        "headers-footers",
    } <= tags
    assert any(case.expected_page_count is not None for case in suite.cases)
    assert all(case.expected_search_phrases for case in suite.cases)
    assert all(case.expected_classification_prefix for case in suite.cases)
    pdf_cases = [case for case in suite.cases if "pdf" in case.tags]
    assert all(case.expected_page_source_counts for case in pdf_cases)
    assert all(
        case.min_ocr_pages is not None
        for case in pdf_cases
        if "ocr" in case.tags
    )
    table_cases = [case for case in suite.cases if "tables" in case.tags]
    assert table_cases
    assert all(case.expected_table_rows for case in table_cases)
    assert all(case.expected_table_columns for case in table_cases)


def test_corpus_eval_case_rejects_invalid_invariants(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    with pytest.raises(ValueError):
        CorpusEvalCase(name="", source_path=source)
    with pytest.raises(ValueError):
        CorpusEvalCase(name="notes", source_path=source, expected_page_count=0)
    with pytest.raises(ValueError):
        CorpusEvalCase(
            name="notes",
            source_path=source,
            expected_page_source_counts={"": 1},
        )
    with pytest.raises(ValueError):
        CorpusEvalCase(
            name="notes",
            source_path=source,
            expected_page_source_counts={"ocr": -1},
        )
    with pytest.raises(ValueError):
        CorpusEvalCase(name="notes", source_path=source, min_ocr_pages=-1)
    with pytest.raises(ValueError):
        CorpusEvalCase(name="notes", source_path=source, min_corrected_pages=-1)
    with pytest.raises(ValueError):
        CorpusEvalCase(name="notes", source_path=source, min_output_char_ratio=-1)
    with pytest.raises(ValueError):
        CorpusEvalCase(name="notes", source_path=source, max_conversion_seconds=0)
    with pytest.raises(ValueError):
        CorpusEvalCase(name="notes", source_path=source, max_processing_seconds=0)
    with pytest.raises(ValueError):
        CorpusEvalCase(name="notes", source_path=source, max_peak_memory_bytes=0)
    with pytest.raises(ValueError):
        CorpusEvalCase(
            name="notes",
            source_path=source,
            min_output_char_ratio=2.0,
            max_output_char_ratio=1.0,
        )
    with pytest.raises(ValueError):
        TextPatternExpectation(pattern="")
    with pytest.raises(ValueError):
        TextPatternExpectation(pattern="x" * 513)
    with pytest.raises(ValueError):
        MarkdownTableExpectation(cells=["one"])
    with pytest.raises(ValueError):
        MarkdownTableExpectation(cells=["one", " "])


def test_load_corpus_eval_suite_rejects_oversized_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite_path = tmp_path / "corpus.json"
    suite_path.write_text(" " * 8, encoding="utf-8")
    monkeypatch.setattr(corpus_eval, "_MAX_CORPUS_EVAL_JSON_BYTES", 4)

    with pytest.raises(ValueError, match="exceeds configured limit"):
        load_corpus_eval_suite(suite_path)


def test_load_conversion_sidecar_ignores_oversized_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_path = tmp_path / "converted.md"
    sidecar_path = output_path.with_suffix(".md.json")
    sidecar_path.write_text(" " * 8, encoding="utf-8")
    monkeypatch.setattr(corpus_eval, "_MAX_CORPUS_EVAL_JSON_BYTES", 4)
    load_conversion_sidecar = cast(
        Callable[[Path], dict[str, object]],
        getattr(corpus_eval, "_load" + "_conversion_sidecar"),
    )

    assert load_conversion_sidecar(output_path) == {}
