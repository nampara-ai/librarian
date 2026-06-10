"""Corpus-level conversion and processing evaluation harness."""

from __future__ import annotations

import json
import re
import time
import tracemalloc
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, Field, model_validator

from librarian.application.convert_document import (
    ConversionFormat,
    DocumentConverter,
    classify_conversion_error,
)
from librarian.application.factory import ApplicationContainer
from librarian.observability import sanitize_error_message
from librarian.version import __version__

_MAX_CORPUS_EVAL_JSON_BYTES = 10 * 1024 * 1024


class TextOrderExpectation(BaseModel):
    """Assert that one text fragment appears before another in converted output."""

    before: str = Field(min_length=1)
    after: str = Field(min_length=1)
    case_sensitive: bool = False


class TextPatternExpectation(BaseModel):
    """Assert that converted output matches a bounded regular expression."""

    pattern: str = Field(min_length=1, max_length=512)
    description: str | None = Field(default=None, max_length=160)
    case_sensitive: bool = False


class MarkdownTableExpectation(BaseModel):
    """Assert that text fragments preserve row or column relationships in a table."""

    cells: list[str] = Field(min_length=2, max_length=16)
    case_sensitive: bool = False

    @model_validator(mode="after")
    def _validate_cells(self) -> MarkdownTableExpectation:
        if any(not cell.strip() for cell in self.cells):
            raise ValueError("table expectation cells must be non-empty")
        return self


class CorpusEvalCase(BaseModel):
    """One source-file evaluation case."""

    name: str = Field(min_length=1)
    source_path: Path
    format: ConversionFormat = ConversionFormat.MARKDOWN
    tags: list[str] = Field(default_factory=list)
    process: bool = True
    expected_contains: list[str] = Field(default_factory=list)
    forbidden_contains: list[str] = Field(default_factory=list)
    expected_text_order: list[TextOrderExpectation] = Field(
        default_factory=lambda: cast(list[TextOrderExpectation], [])
    )
    expected_patterns: list[TextPatternExpectation] = Field(
        default_factory=lambda: cast(list[TextPatternExpectation], [])
    )
    expected_table_rows: list[MarkdownTableExpectation] = Field(
        default_factory=lambda: cast(list[MarkdownTableExpectation], [])
    )
    expected_table_columns: list[MarkdownTableExpectation] = Field(
        default_factory=lambda: cast(list[MarkdownTableExpectation], [])
    )
    expected_search_phrases: list[str] = Field(default_factory=list)
    expected_classification_prefix: str | None = None
    expected_page_count: int | None = Field(default=None, ge=1)
    expected_page_source_counts: dict[str, int] = Field(default_factory=dict)
    min_ocr_pages: int | None = Field(default=None, ge=0)
    min_corrected_pages: int | None = Field(default=None, ge=0)
    min_output_char_ratio: float = Field(default=0.05, ge=0)
    max_output_char_ratio: float = Field(default=20.0, gt=0)
    max_conversion_seconds: float | None = Field(default=None, gt=0)
    max_processing_seconds: float | None = Field(default=None, gt=0)
    max_peak_memory_bytes: int | None = Field(default=None, gt=0)
    require_markdown_headings: bool = False
    require_no_context_markers: bool = True

    @model_validator(mode="after")
    def _validate_ratio_bounds(self) -> CorpusEvalCase:
        if self.max_output_char_ratio < self.min_output_char_ratio:
            raise ValueError("max_output_char_ratio must be >= min_output_char_ratio")
        for source, count in self.expected_page_source_counts.items():
            if not source.strip():
                raise ValueError("expected_page_source_counts keys must be non-empty")
            if count < 0:
                raise ValueError("expected_page_source_counts values must be >= 0")
        return self


class CorpusEvalSuite(BaseModel):
    """Serializable corpus evaluation suite."""

    cases: list[CorpusEvalCase] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class CorpusEvalCaseResult:
    """Result for one corpus evaluation case."""

    name: str
    passed: bool
    tags: tuple[str, ...]
    source_path: Path
    output_path: Path
    input_bytes: int
    output_chars: int
    output_char_ratio: float
    conversion_seconds: float
    processing_seconds: float | None
    peak_memory_bytes: int
    page_count: int | None
    page_status_counts: dict[str, int]
    page_source_counts: dict[str, int]
    page_warning_counts: dict[str, int]
    page_attempts: int
    max_page_duration_ms: float | None
    ocr_pages: int
    corrected_pages: int
    average_ocr_confidence: float | None
    search_recall: float | None
    search_diagnostics: tuple[SearchDiagnostic, ...]
    classification_code: str | None
    classification_label: str | None
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CorpusEvalRunResult:
    """Aggregate corpus evaluation result."""

    cases: tuple[CorpusEvalCaseResult, ...]
    total_seconds: float
    generated_at: datetime
    librarian_version: str
    llm_provider: str
    llm_model: str
    cleaning_prompt_version: str
    classification_prompt_version: str

    @property
    def passed(self) -> bool:
        """Return true when every case passed."""
        return all(item.passed for item in self.cases)


@dataclass(frozen=True, slots=True)
class PageMetrics:
    """Summary of page-level extraction metadata."""

    status_counts: dict[str, int]
    source_counts: dict[str, int]
    warning_counts: dict[str, int]
    attempts: int
    max_duration_ms: float | None
    ocr_pages: int
    corrected_pages: int
    average_ocr_confidence: float | None


@dataclass(frozen=True, slots=True)
class SearchDiagnostic:
    """Per-phrase search evidence captured by corpus evaluation."""

    phrase: str
    hit: bool
    total_results: int | None
    returned_document_ids: tuple[str, ...]
    error: str | None = None


def load_corpus_eval_suite(path: Path) -> CorpusEvalSuite:
    """Load a corpus evaluation suite from JSON."""
    suite = CorpusEvalSuite.model_validate_json(
        _read_limited_text_file(path, max_bytes=_MAX_CORPUS_EVAL_JSON_BYTES)
    )
    return CorpusEvalSuite(
        cases=[
            case.model_copy(update={"source_path": _resolve_case_path(case.source_path, path)})
            for case in suite.cases
        ]
    )


async def run_corpus_eval_suite(
    container: ApplicationContainer,
    suite: CorpusEvalSuite,
    *,
    output_dir: Path,
    overwrite: bool = False,
) -> CorpusEvalRunResult:
    """Run file-level conversion, optional processing, and quality checks."""
    start = time.perf_counter()
    await _mkdir(output_dir)
    converter = DocumentConverter(container.ingest_document.extractor)
    results: list[CorpusEvalCaseResult] = []
    for index, case in enumerate(suite.cases, start=1):
        output_path = output_dir / f"{index:03d}-{_safe_name(case.name)}.{case.format.value}"
        results.append(
            await _run_corpus_case(
                container,
                converter,
                case,
                output_path=output_path,
                overwrite=overwrite,
            )
        )
    return CorpusEvalRunResult(
        cases=tuple(results),
        total_seconds=time.perf_counter() - start,
        generated_at=datetime.now(UTC),
        librarian_version=__version__,
        llm_provider=container.settings.llm_provider,
        llm_model=container.settings.llm_model,
        cleaning_prompt_version=container.settings.cleaning_prompt_version,
        classification_prompt_version=container.settings.classification_prompt_version,
    )


async def _run_corpus_case(
    container: ApplicationContainer,
    converter: DocumentConverter,
    case: CorpusEvalCase,
    *,
    output_path: Path,
    overwrite: bool,
) -> CorpusEvalCaseResult:
    tracemalloc.start()
    conversion_start = time.perf_counter()
    failures: list[str] = []
    processing_seconds: float | None = None
    converted_text = ""
    sidecar = None
    try:
        converted = await converter.convert_file(
            case.source_path,
            output_path,
            format=case.format,
            overwrite=overwrite,
            write_sidecar=True,
        )
        converted_text = converted.text
    except Exception as exc:
        _, peak_memory = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        failure_type = classify_conversion_error(exc)
        error = sanitize_error_message(exc)
        return CorpusEvalCaseResult(
            name=case.name,
            passed=False,
            tags=tuple(case.tags),
            source_path=case.source_path,
            output_path=output_path,
            input_bytes=_safe_stat_size(case.source_path),
            output_chars=0,
            output_char_ratio=0,
            conversion_seconds=max(time.perf_counter() - conversion_start, 1e-9),
            processing_seconds=None,
            peak_memory_bytes=peak_memory,
            page_count=None,
            page_status_counts={},
            page_source_counts={},
            page_warning_counts={},
            page_attempts=0,
            max_page_duration_ms=None,
            ocr_pages=0,
            corrected_pages=0,
            average_ocr_confidence=None,
            search_recall=None,
            search_diagnostics=(),
            classification_code=None,
            classification_label=None,
            failures=(f"conversion failed ({failure_type.value}): {error}",),
        )
    conversion_seconds = max(time.perf_counter() - conversion_start, 1e-9)
    sidecar = _load_conversion_sidecar(output_path)
    input_bytes = _safe_stat_size(case.source_path)
    output_ratio = len(converted_text) / max(input_bytes, 1)

    _check_converted_text(case, converted_text, output_ratio, failures)
    _check_conversion_budget(case, conversion_seconds, failures)
    extraction_obj = sidecar.get("extraction")
    extraction_data = (
        cast(dict[str, object], extraction_obj) if isinstance(extraction_obj, dict) else {}
    )
    page_count = _int_or_none(extraction_data.get("page_count"))
    page_metrics = _page_metrics(extraction_data)
    if case.expected_page_count is not None and page_count != case.expected_page_count:
        failures.append(f"page_count {page_count} != expected {case.expected_page_count}")
    _check_page_summary(extraction_data, page_metrics, failures)
    _check_page_metrics(case, page_metrics, failures)

    classification_code = None
    classification_label = None
    search_recall = None
    search_diagnostics: tuple[SearchDiagnostic, ...] = ()
    if case.process:
        processing_start = time.perf_counter()
        ingested = await container.ingest_document.execute(output_path)
        run = await container.process_document.execute(ingested.document.id)
        processing_seconds = max(time.perf_counter() - processing_start, 1e-9)
        if run.error:
            failures.append(f"processing run failed: {run.error}")
        classification = await container.repository.get_classification(ingested.document.id)
        if classification is not None:
            classification_code = classification.code
            classification_label = classification.label
        if (
            case.expected_classification_prefix
            and (
                classification is None
                or not classification.code.startswith(case.expected_classification_prefix)
            )
        ):
            failures.append(
                "classification "
                f"{classification_code or '<missing>'} does not match "
                f"{case.expected_classification_prefix}"
            )
        search_recall, search_diagnostics = await _search_recall(
            container,
            case.expected_search_phrases,
            str(ingested.document.id),
            failures,
        )
    elif case.expected_search_phrases or case.expected_classification_prefix:
        failures.append("search/classification expectations require process=true")

    _, peak_memory = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    _check_processing_budget(case, processing_seconds, failures)
    _check_memory_budget(case, peak_memory, failures)
    return CorpusEvalCaseResult(
        name=case.name,
        passed=not failures,
        tags=tuple(case.tags),
        source_path=case.source_path,
        output_path=output_path,
        input_bytes=input_bytes,
        output_chars=len(converted_text),
        output_char_ratio=output_ratio,
        conversion_seconds=conversion_seconds,
        processing_seconds=processing_seconds,
        peak_memory_bytes=peak_memory,
        page_count=page_count,
        page_status_counts=page_metrics.status_counts,
        page_source_counts=page_metrics.source_counts,
        page_warning_counts=page_metrics.warning_counts,
        page_attempts=page_metrics.attempts,
        max_page_duration_ms=page_metrics.max_duration_ms,
        ocr_pages=page_metrics.ocr_pages,
        corrected_pages=page_metrics.corrected_pages,
        average_ocr_confidence=page_metrics.average_ocr_confidence,
        search_recall=search_recall,
        search_diagnostics=search_diagnostics,
        classification_code=classification_code,
        classification_label=classification_label,
        failures=tuple(failures),
    )


def corpus_eval_result_json(result: CorpusEvalRunResult) -> str:
    """Render corpus evaluation results as JSON."""
    return json.dumps(
        {
            "artifact_type": "librarian-corpus-eval-result",
            "evidence_tier": _evidence_tier(result.llm_provider),
            "passed": result.passed,
            "total_seconds": result.total_seconds,
            "generated_at": result.generated_at.isoformat(),
            "librarian_version": result.librarian_version,
            "llm_provider": result.llm_provider,
            "llm_model": result.llm_model,
            "cleaning_prompt_version": result.cleaning_prompt_version,
            "classification_prompt_version": result.classification_prompt_version,
            "summary": _corpus_eval_summary(result),
            "cases": [
                {
                    "name": item.name,
                    "passed": item.passed,
                    "tags": list(item.tags),
                    "source_path": str(item.source_path),
                    "output_path": str(item.output_path),
                    "input_bytes": item.input_bytes,
                    "output_chars": item.output_chars,
                    "output_char_ratio": item.output_char_ratio,
                    "conversion_seconds": item.conversion_seconds,
                    "processing_seconds": item.processing_seconds,
                    "peak_memory_bytes": item.peak_memory_bytes,
                    "page_count": item.page_count,
                    "page_status_counts": item.page_status_counts,
                    "page_source_counts": item.page_source_counts,
                    "page_warning_counts": item.page_warning_counts,
                    "page_attempts": item.page_attempts,
                    "max_page_duration_ms": item.max_page_duration_ms,
                    "ocr_pages": item.ocr_pages,
                    "corrected_pages": item.corrected_pages,
                    "average_ocr_confidence": item.average_ocr_confidence,
                    "search_recall": item.search_recall,
                    "search_diagnostics": [
                        {
                            "phrase": diagnostic.phrase,
                            "hit": diagnostic.hit,
                            "total_results": diagnostic.total_results,
                            "returned_document_ids": list(diagnostic.returned_document_ids),
                            "error": diagnostic.error,
                        }
                        for diagnostic in item.search_diagnostics
                    ],
                    "classification_code": item.classification_code,
                    "classification_label": item.classification_label,
                    "failures": list(item.failures),
                }
                for item in result.cases
            ],
        },
        indent=2,
    )


def _evidence_tier(provider: str) -> str:
    return "mock-smoke" if provider == "mock" else "real-provider"


def _corpus_eval_summary(result: CorpusEvalRunResult) -> dict[str, object]:
    cases = result.cases
    search_recalls = [item.search_recall for item in cases if item.search_recall is not None]
    search_diagnostics = [
        diagnostic for item in cases for diagnostic in item.search_diagnostics
    ]
    return {
        "case_count": len(cases),
        "passed_count": sum(1 for item in cases if item.passed),
        "failed_count": sum(1 for item in cases if not item.passed),
        "pass_rate": (sum(1 for item in cases if item.passed) / len(cases) if cases else 0.0),
        "total_input_bytes": sum(item.input_bytes for item in cases),
        "total_output_chars": sum(item.output_chars for item in cases),
        "total_page_attempts": sum(item.page_attempts for item in cases),
        "total_failed_pages": sum(item.page_status_counts.get("failed", 0) for item in cases),
        "total_ocr_pages": sum(item.ocr_pages for item in cases),
        "total_corrected_pages": sum(item.corrected_pages for item in cases),
        "max_peak_memory_bytes": max((item.peak_memory_bytes for item in cases), default=0),
        "max_page_duration_ms": max(
            (
                item.max_page_duration_ms
                for item in cases
                if item.max_page_duration_ms is not None
            ),
            default=None,
        ),
        "average_search_recall": (
            sum(search_recalls) / len(search_recalls) if search_recalls else None
        ),
        "total_search_phrases": len(search_diagnostics),
        "total_search_hits": sum(1 for item in search_diagnostics if item.hit),
        "failure_count": sum(len(item.failures) for item in cases),
        "failure_case_count": sum(1 for item in cases if item.failures),
    }


def _check_converted_text(
    case: CorpusEvalCase,
    converted_text: str,
    output_ratio: float,
    failures: list[str],
) -> None:
    lower_output = converted_text.lower()
    if output_ratio < case.min_output_char_ratio:
        failures.append(
            f"output_char_ratio {output_ratio:.3f} < minimum {case.min_output_char_ratio:.3f}"
        )
    if output_ratio > case.max_output_char_ratio:
        failures.append(
            f"output_char_ratio {output_ratio:.3f} > maximum {case.max_output_char_ratio:.3f}"
        )
    for expected in case.expected_contains:
        if expected.lower() not in lower_output:
            failures.append(f"missing expected text: {expected}")
    for forbidden in case.forbidden_contains:
        if forbidden.lower() in lower_output:
            failures.append(f"found forbidden text: {forbidden}")
    _check_expected_text_order(case.expected_text_order, converted_text, failures)
    _check_expected_patterns(case.expected_patterns, converted_text, failures)
    _check_expected_markdown_tables(case, converted_text, failures)
    if case.require_markdown_headings and "\n# " not in f"\n{converted_text}":
        failures.append("missing Markdown heading")
    if case.require_no_context_markers:
        for marker in ("[previous context]", "[next context]", "as an ai language model"):
            if marker in lower_output:
                failures.append(f"found context/assistant artifact: {marker}")


def _check_expected_text_order(
    expectations: list[TextOrderExpectation],
    converted_text: str,
    failures: list[str],
) -> None:
    for expectation in expectations:
        haystack = _normalize_order_text(
            converted_text,
            case_sensitive=expectation.case_sensitive,
        )
        before = _normalize_order_text(
            expectation.before,
            case_sensitive=expectation.case_sensitive,
        )
        after = _normalize_order_text(
            expectation.after,
            case_sensitive=expectation.case_sensitive,
        )
        before_index = haystack.find(before)
        after_index = haystack.find(after)
        if before_index < 0:
            failures.append(f"order check missing before text: {expectation.before}")
            continue
        if after_index < 0:
            failures.append(f"order check missing after text: {expectation.after}")
            continue
        if before_index >= after_index:
            failures.append(
                "order check failed: "
                f"{expectation.before!r} does not appear before {expectation.after!r}"
            )


def _normalize_order_text(value: str, *, case_sensitive: bool) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    return normalized if case_sensitive else normalized.lower()


def _check_expected_patterns(
    expectations: list[TextPatternExpectation],
    converted_text: str,
    failures: list[str],
) -> None:
    for expectation in expectations:
        flags = 0 if expectation.case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(expectation.pattern, flags)
        except re.error as exc:
            failures.append(f"invalid expected pattern {expectation.pattern!r}: {exc}")
            continue
        if regex.search(converted_text) is None:
            label = expectation.description or expectation.pattern
            failures.append(f"missing expected pattern: {label}")


def _check_expected_markdown_tables(
    case: CorpusEvalCase,
    converted_text: str,
    failures: list[str],
) -> None:
    if not case.expected_table_rows and not case.expected_table_columns:
        return
    tables = _parse_markdown_tables(converted_text)
    if not tables:
        failures.append("missing Markdown table")
        return
    for expectation in case.expected_table_rows:
        if not _table_relation_matches(
            tables,
            expectation,
            relation="row",
        ):
            failures.append(
                "table row missing related cells: "
                + ", ".join(repr(cell) for cell in expectation.cells)
            )
    for expectation in case.expected_table_columns:
        if not _table_relation_matches(
            tables,
            expectation,
            relation="column",
        ):
            failures.append(
                "table column missing related cells: "
                + ", ".join(repr(cell) for cell in expectation.cells)
            )


def _parse_markdown_tables(text: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in text.splitlines():
        if "|" not in line:
            if current:
                tables.append(current)
                current = []
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            if current:
                tables.append(current)
                current = []
            continue
        if all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells):
            continue
        current.append(cells)
    if current:
        tables.append(current)
    return tables


def _table_relation_matches(
    tables: list[list[list[str]]],
    expectation: MarkdownTableExpectation,
    *,
    relation: Literal["row", "column"],
) -> bool:
    normalized_needles = [
        _normalize_order_text(cell, case_sensitive=expectation.case_sensitive)
        for cell in expectation.cells
    ]
    for table in tables:
        groups = table if relation == "row" else _table_columns(table)
        for group in groups:
            normalized_cells = [
                _normalize_order_text(cell, case_sensitive=expectation.case_sensitive)
                for cell in group
            ]
            if all(
                any(needle in cell for cell in normalized_cells)
                for needle in normalized_needles
            ):
                return True
    return False


def _table_columns(table: list[list[str]]) -> list[list[str]]:
    width = max((len(row) for row in table), default=0)
    return [[row[index] for row in table if index < len(row)] for index in range(width)]


def _check_conversion_budget(
    case: CorpusEvalCase,
    conversion_seconds: float,
    failures: list[str],
) -> None:
    if (
        case.max_conversion_seconds is not None
        and conversion_seconds > case.max_conversion_seconds
    ):
        failures.append(
            "conversion_seconds "
            f"{conversion_seconds:.3f} > maximum {case.max_conversion_seconds:.3f}"
        )


def _check_processing_budget(
    case: CorpusEvalCase,
    processing_seconds: float | None,
    failures: list[str],
) -> None:
    if case.max_processing_seconds is None:
        return
    if processing_seconds is None:
        failures.append("max_processing_seconds requires process=true")
        return
    if processing_seconds > case.max_processing_seconds:
        failures.append(
            "processing_seconds "
            f"{processing_seconds:.3f} > maximum {case.max_processing_seconds:.3f}"
        )


def _check_memory_budget(
    case: CorpusEvalCase,
    peak_memory_bytes: int,
    failures: list[str],
) -> None:
    if case.max_peak_memory_bytes is None:
        return
    if peak_memory_bytes > case.max_peak_memory_bytes:
        failures.append(
            f"peak_memory_bytes {peak_memory_bytes} > maximum {case.max_peak_memory_bytes}"
        )


def _check_page_metrics(
    case: CorpusEvalCase,
    page_metrics: PageMetrics,
    failures: list[str],
) -> None:
    for source, expected_count in case.expected_page_source_counts.items():
        actual_count = page_metrics.source_counts.get(source, 0)
        if actual_count != expected_count:
            failures.append(
                f"page source {source!r} count {actual_count} != expected {expected_count}"
            )
    if case.min_ocr_pages is not None and page_metrics.ocr_pages < case.min_ocr_pages:
        failures.append(
            f"ocr_pages {page_metrics.ocr_pages} < minimum {case.min_ocr_pages}"
        )
    if (
        case.min_corrected_pages is not None
        and page_metrics.corrected_pages < case.min_corrected_pages
    ):
        failures.append(
            "corrected_pages "
            f"{page_metrics.corrected_pages} < minimum {case.min_corrected_pages}"
        )


def _check_page_summary(
    extraction: dict[str, object],
    page_metrics: PageMetrics,
    failures: list[str],
) -> None:
    summary_obj = extraction.get("summary")
    if summary_obj is None:
        return
    if not isinstance(summary_obj, dict):
        failures.append("page summary must be an object")
        return
    summary = cast(dict[str, object], summary_obj)
    expected_status = "succeeded"
    if page_metrics.status_counts.get("failed", 0) > 0:
        expected_status = "failed"
    elif page_metrics.status_counts.get("pending", 0) > 0:
        expected_status = "pending"
    _check_summary_value(summary, "status", expected_status, failures)
    _check_summary_value(
        summary,
        "status_counts",
        {
            "failed": page_metrics.status_counts.get("failed", 0),
            "pending": page_metrics.status_counts.get("pending", 0),
            "succeeded": page_metrics.status_counts.get("succeeded", 0),
        },
        failures,
    )
    _check_summary_value(summary, "source_counts", page_metrics.source_counts, failures)
    _check_summary_value(summary, "warning_counts", page_metrics.warning_counts, failures)
    _check_summary_value(summary, "attempts", page_metrics.attempts, failures)
    _check_summary_value(summary, "ocr_pages", page_metrics.ocr_pages, failures)
    _check_summary_value(summary, "corrected_pages", page_metrics.corrected_pages, failures)
    _check_summary_float(
        summary,
        "average_ocr_confidence",
        page_metrics.average_ocr_confidence,
        failures,
    )
    _check_summary_float(
        summary,
        "max_page_duration_ms",
        page_metrics.max_duration_ms,
        failures,
    )


def _check_summary_value(
    summary: dict[str, object],
    key: str,
    expected: object,
    failures: list[str],
) -> None:
    actual = summary.get(key)
    if actual != expected:
        failures.append(f"page summary {key} {actual!r} != expected {expected!r}")


def _check_summary_float(
    summary: dict[str, object],
    key: str,
    expected: float | None,
    failures: list[str],
) -> None:
    actual = summary.get(key)
    if expected is None:
        if actual is not None:
            failures.append(f"page summary {key} {actual!r} != expected None")
        return
    if not isinstance(actual, int | float) or abs(float(actual) - expected) > 1e-6:
        failures.append(f"page summary {key} {actual!r} != expected {expected!r}")


async def _search_recall(
    container: ApplicationContainer,
    phrases: list[str],
    expected_document_id: str,
    failures: list[str],
) -> tuple[float | None, tuple[SearchDiagnostic, ...]]:
    if not phrases:
        return None, ()
    hits = 0
    diagnostics: list[SearchDiagnostic] = []
    for phrase in phrases:
        try:
            results = await container.repository.search(phrase, limit=20)
            total_results = await container.repository.search_count(phrase)
        except ValueError as exc:
            error = sanitize_error_message(exc)
            failures.append(f"search failed for {phrase!r}: {error}")
            diagnostics.append(
                SearchDiagnostic(
                    phrase=phrase,
                    hit=False,
                    total_results=None,
                    returned_document_ids=(),
                    error=error,
                )
            )
            continue
        returned_ids = tuple(str(document_id) for document_id in results)
        hit = expected_document_id in set(returned_ids)
        diagnostics.append(
            SearchDiagnostic(
                phrase=phrase,
                hit=hit,
                total_results=total_results,
                returned_document_ids=returned_ids,
            )
        )
        if hit:
            hits += 1
        else:
            failures.append(f"search did not find document for phrase: {phrase}")
    return hits / len(phrases), tuple(diagnostics)


def _load_conversion_sidecar(output_path: Path) -> dict[str, object]:
    sidecar_path = output_path.with_suffix(f"{output_path.suffix}.json")
    try:
        payload = json.loads(
            _read_limited_text_file(sidecar_path, max_bytes=_MAX_CORPUS_EVAL_JSON_BYTES)
        )
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return {}
    return cast(dict[str, object], payload) if isinstance(payload, dict) else {}


def _read_limited_text_file(path: Path, *, max_bytes: int) -> str:
    with path.open("rb") as handle:
        payload = handle.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError(f"JSON file exceeds configured limit of {max_bytes} bytes: {path}")
    return payload.decode("utf-8")


def _page_metrics(extraction: dict[str, object]) -> PageMetrics:
    pages_obj = extraction.get("pages")
    if not isinstance(pages_obj, list):
        return PageMetrics(
            status_counts={},
            source_counts={},
            warning_counts={},
            attempts=0,
            max_duration_ms=None,
            ocr_pages=0,
            corrected_pages=0,
            average_ocr_confidence=None,
        )
    status_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    warning_counts: dict[str, int] = {}
    attempts = 0
    durations: list[float] = []
    corrected_pages = 0
    ocr_pages = 0
    confidences: list[float] = []
    for page_obj in cast(list[object], pages_obj):
        if not isinstance(page_obj, dict):
            continue
        page = cast(dict[str, object], page_obj)
        status = str(page.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        source = str(page.get("source") or "unknown")
        source_counts[source] = source_counts.get(source, 0) + 1
        page_attempts = page.get("attempts")
        if isinstance(page_attempts, int) and page_attempts >= 0:
            attempts += page_attempts
        duration_ms = page.get("duration_ms")
        if isinstance(duration_ms, int | float) and duration_ms >= 0:
            durations.append(float(duration_ms))
        warnings = page.get("warnings")
        if isinstance(warnings, list):
            for warning in cast(list[object], warnings):
                if isinstance(warning, str) and warning:
                    warning_counts[warning] = warning_counts.get(warning, 0) + 1
        if source == "ocr":
            ocr_pages += 1
        if page.get("corrected") is True:
            corrected_pages += 1
        confidence = page.get("confidence")
        if isinstance(confidence, int | float):
            confidences.append(float(confidence))
    return PageMetrics(
        status_counts=status_counts,
        source_counts=source_counts,
        warning_counts=warning_counts,
        attempts=attempts,
        max_duration_ms=max(durations) if durations else None,
        ocr_pages=ocr_pages,
        corrected_pages=corrected_pages,
        average_ocr_confidence=(
            sum(confidences) / len(confidences) if confidences else None
        ),
    )


def _resolve_case_path(source_path: Path, suite_path: Path) -> Path:
    if source_path.is_absolute():
        return source_path
    return (suite_path.parent / source_path).resolve()


def _safe_name(name: str) -> str:
    safe = "".join(char.lower() if char.isalnum() else "-" for char in name).strip("-")
    return safe or "case"


async def _mkdir(path: Path) -> None:
    import asyncio

    await asyncio.to_thread(_reject_symlinked_output_directory, path)
    await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)


def _reject_symlinked_output_directory(path: Path) -> None:
    for current in (*reversed(path.parents), path):
        if current.exists() and current.is_symlink():
            raise ValueError(f"Corpus eval output directory crosses symlink: {path}")


def _safe_stat_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None
