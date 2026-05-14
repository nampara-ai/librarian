"""Validate release-candidate eval, corpus-eval, and benchmark evidence."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return payload


def _expect(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def _float_matches(actual: Any, expected: float, *, tolerance: float = 1e-9) -> bool:
    return isinstance(actual, int | float) and abs(float(actual) - expected) <= tolerance


def _summary(payload: dict[str, Any], path: Path) -> dict[str, Any]:
    value = payload.get("summary")
    if not isinstance(value, dict):
        raise ValueError(f"{path}: missing summary object")
    return value


def _normalize_version(value: str) -> str:
    return value.removeprefix("v")


def _check_version(
    payload: dict[str, Any],
    path: Path,
    *,
    expected_version: str | None,
    label: str,
    failures: list[str],
) -> None:
    if expected_version is None:
        return
    actual = payload.get("librarian_version")
    if not isinstance(actual, str) or _normalize_version(actual) != _normalize_version(
        expected_version
    ):
        failures.append(f"{path}: {label} version {actual!r} does not match {expected_version!r}")


def _check_generated_at(
    payload: dict[str, Any],
    path: Path,
    *,
    label: str,
    failures: list[str],
) -> None:
    value = payload.get("generated_at")
    if not isinstance(value, str) or not value:
        failures.append(f"{path}: {label} missing generated_at")
        return
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        failures.append(f"{path}: {label} generated_at is not ISO-8601")
        return
    if parsed.tzinfo is None:
        failures.append(f"{path}: {label} generated_at must include a timezone")


def _check_pass_counts(
    summary: dict[str, Any],
    path: Path,
    *,
    label: str,
    failures: list[str],
) -> None:
    case_count = summary.get("case_count")
    passed_count = summary.get("passed_count")
    failed_count = summary.get("failed_count")
    if not all(isinstance(value, int) for value in (case_count, passed_count, failed_count)):
        failures.append(f"{path}: {label} summary missing integer pass counts")
        return
    if passed_count + failed_count != case_count:
        failures.append(f"{path}: {label} pass counts do not add up to case_count")


def _check_artifact_type(
    payload: dict[str, Any],
    path: Path,
    *,
    expected_type: str,
    label: str,
    failures: list[str],
) -> None:
    actual = payload.get("artifact_type")
    if actual != expected_type:
        failures.append(f"{path}: {label} artifact_type {actual!r} != {expected_type!r}")


def _check_evidence_tier(
    payload: dict[str, Any],
    path: Path,
    *,
    providers: set[Any],
    require_real_provider: bool,
    label: str,
    failures: list[str],
) -> None:
    tier = payload.get("evidence_tier")
    if tier not in {"mock-smoke", "real-provider"}:
        failures.append(f"{path}: {label} evidence_tier is missing or invalid")
        return
    expected = (
        "mock-smoke"
        if not providers or "mock" in providers or None in providers
        else "real-provider"
    )
    if tier != expected:
        failures.append(f"{path}: {label} evidence_tier {tier!r} does not match providers")
    if require_real_provider and tier != "real-provider":
        failures.append(f"{path}: {label} evidence_tier is not real-provider")


def _check_case_results(
    payload: dict[str, Any],
    summary: dict[str, Any],
    path: Path,
    *,
    label: str,
    failures: list[str],
) -> None:
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        failures.append(f"{path}: {label} missing case details")
        return
    if not all(isinstance(item, dict) for item in cases):
        failures.append(f"{path}: {label} case details must be objects")
        return
    case_count = summary.get("case_count")
    passed_count = summary.get("passed_count")
    failed_count = summary.get("failed_count")
    if isinstance(case_count, int) and len(cases) != case_count:
        failures.append(f"{path}: {label} case detail count does not match summary")
    detailed_passed = sum(1 for item in cases if item.get("passed") is True)
    detailed_failed = sum(1 for item in cases if item.get("passed") is False)
    if isinstance(passed_count, int) and detailed_passed != passed_count:
        failures.append(f"{path}: {label} detailed passed count does not match summary")
    if isinstance(failed_count, int) and detailed_failed != failed_count:
        failures.append(f"{path}: {label} detailed failed count does not match summary")
    for index, item in enumerate(cases, start=1):
        if item.get("passed") is not True:
            failures.append(f"{path}: {label} case {index} did not pass")
        case_failures = item.get("failures")
        if not isinstance(case_failures, list):
            failures.append(f"{path}: {label} case {index} missing failures list")
        elif case_failures:
            failures.append(f"{path}: {label} case {index} has failure details")


def _check_eval_case_metrics(
    payload: dict[str, Any],
    path: Path,
    *,
    failures: list[str],
) -> None:
    cases = payload.get("cases")
    if not isinstance(cases, list):
        return
    for index, item in enumerate(cases, start=1):
        if not isinstance(item, dict):
            continue
        if not isinstance(item.get("name"), str) or not item.get("name"):
            failures.append(f"{path}: eval case {index} missing name")
        if not isinstance(item.get("tags"), list):
            failures.append(f"{path}: eval case {index} missing tags list")
        if not isinstance(item.get("warnings"), list):
            failures.append(f"{path}: eval case {index} missing warnings list")
        if not isinstance(item.get("input_chars"), int | float) or item.get("input_chars", 0) <= 0:
            failures.append(f"{path}: eval case {index} missing positive input_chars")
        if (
            not isinstance(item.get("output_chars"), int | float)
            or item.get("output_chars", 0) <= 0
        ):
            failures.append(f"{path}: eval case {index} missing positive output_chars")
        ratio = item.get("output_char_ratio")
        if not isinstance(ratio, int | float) or float(ratio) <= 0:
            failures.append(f"{path}: eval case {index} missing positive output_char_ratio")
        duration = item.get("duration_seconds")
        if not isinstance(duration, int | float) or float(duration) <= 0:
            failures.append(f"{path}: eval case {index} missing positive duration_seconds")
        cps = item.get("chars_per_second")
        if not isinstance(cps, int | float) or float(cps) <= 0:
            failures.append(f"{path}: eval case {index} missing positive chars_per_second")
        input_chars = item.get("input_chars")
        output_chars = item.get("output_chars")
        if (
            isinstance(input_chars, int | float)
            and float(input_chars) > 0
            and isinstance(output_chars, int | float)
            and float(output_chars) >= 0
        ):
            expected_ratio = float(output_chars) / float(input_chars)
            if not _float_matches(ratio, expected_ratio):
                failures.append(f"{path}: eval case {index} output_char_ratio is inconsistent")
            if isinstance(duration, int | float) and float(duration) > 0:
                expected_cps = float(input_chars) / float(duration)
                if not _float_matches(cps, expected_cps):
                    failures.append(f"{path}: eval case {index} chars_per_second is inconsistent")
        if not isinstance(item.get("classification_code"), str) or not item.get(
            "classification_code"
        ):
            failures.append(f"{path}: eval case {index} missing classification_code")
        if not isinstance(item.get("classification_label"), str) or not item.get(
            "classification_label"
        ):
            failures.append(f"{path}: eval case {index} missing classification_label")


def _check_eval_aggregate_summary(
    payload: dict[str, Any],
    summary: dict[str, Any],
    path: Path,
    *,
    failures: list[str],
) -> None:
    cases = payload.get("cases")
    if not isinstance(cases, list) or not all(isinstance(item, dict) for item in cases):
        return
    total_input_chars = 0
    total_output_chars = 0
    chars_per_second: list[float] = []
    warning_count = 0
    failure_count = 0
    warning_case_count = 0
    failure_case_count = 0
    for item in cases:
        input_chars = item.get("input_chars")
        if isinstance(input_chars, int | float) and input_chars >= 0:
            total_input_chars += int(input_chars)
        output_chars = item.get("output_chars")
        if isinstance(output_chars, int | float) and output_chars >= 0:
            total_output_chars += int(output_chars)
        cps = item.get("chars_per_second")
        if isinstance(cps, int | float) and float(cps) > 0:
            chars_per_second.append(float(cps))
        warnings_obj = item.get("warnings")
        if isinstance(warnings_obj, list):
            warning_count += len(warnings_obj)
            if warnings_obj:
                warning_case_count += 1
        failures_obj = item.get("failures")
        if isinstance(failures_obj, list):
            failure_count += len(failures_obj)
            if failures_obj:
                failure_case_count += 1

    if summary.get("total_input_chars") != total_input_chars:
        failures.append(f"{path}: eval total_input_chars does not match cases")
    if summary.get("total_output_chars") != total_output_chars:
        failures.append(f"{path}: eval total_output_chars does not match cases")
    expected_average_cps = (
        sum(chars_per_second) / len(chars_per_second) if chars_per_second else 0.0
    )
    actual_average_cps = summary.get("average_chars_per_second")
    if (
        not isinstance(actual_average_cps, int | float)
        or abs(float(actual_average_cps) - expected_average_cps) > 1e-9
    ):
        failures.append(f"{path}: eval average_chars_per_second does not match cases")
    if summary.get("warning_count") != warning_count:
        failures.append(f"{path}: eval warning_count does not match cases")
    if summary.get("failure_count") != failure_count:
        failures.append(f"{path}: eval failure_count does not match cases")
    if summary.get("warning_case_count") != warning_case_count:
        failures.append(f"{path}: eval warning_case_count does not match cases")
    if summary.get("failure_case_count") != failure_case_count:
        failures.append(f"{path}: eval failure_case_count does not match cases")


def _check_corpus_case_metrics(
    payload: dict[str, Any],
    path: Path,
    *,
    failures: list[str],
) -> None:
    cases = payload.get("cases")
    if not isinstance(cases, list):
        return
    for index, item in enumerate(cases, start=1):
        if not isinstance(item, dict):
            continue
        if not isinstance(item.get("name"), str) or not item.get("name"):
            failures.append(f"{path}: corpus eval case {index} missing name")
        if not isinstance(item.get("tags"), list):
            failures.append(f"{path}: corpus eval case {index} missing tags list")
        for key in ("source_path", "output_path"):
            if not isinstance(item.get(key), str) or not item.get(key):
                failures.append(f"{path}: corpus eval case {index} missing {key}")
        if not isinstance(item.get("input_bytes"), int | float) or item.get("input_bytes", 0) <= 0:
            failures.append(f"{path}: corpus eval case {index} missing positive input_bytes")
        if (
            not isinstance(item.get("output_chars"), int | float)
            or item.get("output_chars", 0) <= 0
        ):
            failures.append(f"{path}: corpus eval case {index} missing positive output_chars")
        ratio = item.get("output_char_ratio")
        if not isinstance(ratio, int | float) or float(ratio) <= 0:
            failures.append(f"{path}: corpus eval case {index} missing positive output_char_ratio")
        input_bytes = item.get("input_bytes")
        output_chars = item.get("output_chars")
        if (
            isinstance(input_bytes, int | float)
            and float(input_bytes) > 0
            and isinstance(output_chars, int | float)
            and float(output_chars) >= 0
        ):
            expected_ratio = float(output_chars) / float(input_bytes)
            if not _float_matches(ratio, expected_ratio):
                failures.append(
                    f"{path}: corpus eval case {index} output_char_ratio is inconsistent"
                )
        for key in ("conversion_seconds", "peak_memory_bytes"):
            if not isinstance(item.get(key), int | float) or item.get(key, 0) < 0:
                failures.append(f"{path}: corpus eval case {index} missing nonnegative {key}")
        status_counts = item.get("page_status_counts")
        if not isinstance(status_counts, dict):
            failures.append(f"{path}: corpus eval case {index} missing page_status_counts")
        source_counts = item.get("page_source_counts")
        if not isinstance(source_counts, dict):
            failures.append(f"{path}: corpus eval case {index} missing page_source_counts")
        warning_counts = item.get("page_warning_counts")
        if not isinstance(warning_counts, dict):
            failures.append(f"{path}: corpus eval case {index} missing page_warning_counts")
        for key in ("page_attempts", "ocr_pages", "corrected_pages"):
            if not isinstance(item.get(key), int) or item.get(key, -1) < 0:
                failures.append(f"{path}: corpus eval case {index} missing nonnegative {key}")
        max_page_duration = item.get("max_page_duration_ms")
        if max_page_duration is not None and (
            not isinstance(max_page_duration, int | float)
            or float(max_page_duration) < 0
        ):
            failures.append(f"{path}: corpus eval case {index} invalid max_page_duration_ms")
        search_recall = item.get("search_recall")
        if search_recall is not None and (
            not isinstance(search_recall, int | float)
            or float(search_recall) < 0
            or float(search_recall) > 1
        ):
            failures.append(f"{path}: corpus eval case {index} invalid search_recall")
        diagnostics = item.get("search_diagnostics")
        if not isinstance(diagnostics, list):
            failures.append(f"{path}: corpus eval case {index} missing search_diagnostics list")
        elif search_recall is not None and not diagnostics:
            failures.append(f"{path}: corpus eval case {index} missing search diagnostics")
        elif isinstance(search_recall, int | float) and diagnostics:
            hits = sum(
                1
                for diagnostic in diagnostics
                if isinstance(diagnostic, dict) and diagnostic.get("hit") is True
            )
            expected_recall = hits / len(diagnostics)
            if not _float_matches(search_recall, expected_recall):
                failures.append(
                    f"{path}: corpus eval case {index} search_recall is inconsistent"
                )


def _check_corpus_aggregate_summary(
    payload: dict[str, Any],
    summary: dict[str, Any],
    path: Path,
    *,
    failures: list[str],
) -> None:
    cases = payload.get("cases")
    if not isinstance(cases, list) or not all(isinstance(item, dict) for item in cases):
        return
    total_input_bytes = 0
    total_output_chars = 0
    total_ocr_pages = 0
    total_corrected_pages = 0
    peak_memory_values: list[float] = []
    search_recalls: list[float] = []
    total_search_phrases = 0
    total_search_hits = 0
    failure_count = 0
    failure_case_count = 0
    total_attempts = 0
    total_failed_pages = 0
    durations: list[float] = []
    for item in cases:
        input_bytes = item.get("input_bytes")
        if isinstance(input_bytes, int | float) and input_bytes >= 0:
            total_input_bytes += int(input_bytes)
        output_chars = item.get("output_chars")
        if isinstance(output_chars, int | float) and output_chars >= 0:
            total_output_chars += int(output_chars)
        ocr_pages = item.get("ocr_pages")
        if isinstance(ocr_pages, int) and ocr_pages >= 0:
            total_ocr_pages += ocr_pages
        corrected_pages = item.get("corrected_pages")
        if isinstance(corrected_pages, int) and corrected_pages >= 0:
            total_corrected_pages += corrected_pages
        peak_memory = item.get("peak_memory_bytes")
        if isinstance(peak_memory, int | float) and peak_memory >= 0:
            peak_memory_values.append(float(peak_memory))
        search_recall = item.get("search_recall")
        if isinstance(search_recall, int | float) and 0 <= float(search_recall) <= 1:
            search_recalls.append(float(search_recall))
        diagnostics = item.get("search_diagnostics")
        if isinstance(diagnostics, list):
            total_search_phrases += len(diagnostics)
            total_search_hits += sum(
                1
                for diagnostic in diagnostics
                if isinstance(diagnostic, dict) and diagnostic.get("hit") is True
            )
        failures_obj = item.get("failures")
        if isinstance(failures_obj, list):
            failure_count += len(failures_obj)
            if failures_obj:
                failure_case_count += 1
        attempts = item.get("page_attempts")
        if isinstance(attempts, int) and attempts >= 0:
            total_attempts += attempts
        statuses = item.get("page_status_counts")
        if isinstance(statuses, dict):
            failed_pages = statuses.get("failed", 0)
            if isinstance(failed_pages, int) and failed_pages >= 0:
                total_failed_pages += failed_pages
        duration = item.get("max_page_duration_ms")
        if isinstance(duration, int | float) and float(duration) >= 0:
            durations.append(float(duration))

    if summary.get("total_input_bytes") != total_input_bytes:
        failures.append(f"{path}: corpus eval total_input_bytes does not match cases")
    if summary.get("total_output_chars") != total_output_chars:
        failures.append(f"{path}: corpus eval total_output_chars does not match cases")
    if summary.get("total_ocr_pages") != total_ocr_pages:
        failures.append(f"{path}: corpus eval total_ocr_pages does not match cases")
    if summary.get("total_corrected_pages") != total_corrected_pages:
        failures.append(f"{path}: corpus eval total_corrected_pages does not match cases")
    expected_peak_memory = max(peak_memory_values, default=0.0)
    if summary.get("max_peak_memory_bytes") != expected_peak_memory:
        failures.append(f"{path}: corpus eval max_peak_memory_bytes does not match cases")
    expected_search_recall = (
        sum(search_recalls) / len(search_recalls) if search_recalls else None
    )
    actual_search_recall = summary.get("average_search_recall")
    if expected_search_recall is None:
        if actual_search_recall is not None:
            failures.append(f"{path}: corpus eval average_search_recall does not match cases")
    elif (
        not isinstance(actual_search_recall, int | float)
        or abs(float(actual_search_recall) - expected_search_recall) > 1e-9
    ):
        failures.append(f"{path}: corpus eval average_search_recall does not match cases")
    if summary.get("total_search_phrases") != total_search_phrases:
        failures.append(f"{path}: corpus eval total_search_phrases does not match cases")
    if summary.get("total_search_hits") != total_search_hits:
        failures.append(f"{path}: corpus eval total_search_hits does not match cases")
    if summary.get("failure_count") != failure_count:
        failures.append(f"{path}: corpus eval failure_count does not match cases")
    if summary.get("failure_case_count") != failure_case_count:
        failures.append(f"{path}: corpus eval failure_case_count does not match cases")
    if summary.get("total_page_attempts") != total_attempts:
        failures.append(f"{path}: corpus eval total_page_attempts does not match cases")
    if summary.get("total_failed_pages") != total_failed_pages:
        failures.append(f"{path}: corpus eval total_failed_pages does not match cases")
    expected_max_duration = max(durations) if durations else None
    actual_max_duration = summary.get("max_page_duration_ms")
    if expected_max_duration is None:
        if actual_max_duration is not None:
            failures.append(f"{path}: corpus eval max_page_duration_ms does not match cases")
    elif (
        not isinstance(actual_max_duration, int | float)
        or abs(float(actual_max_duration) - expected_max_duration) > 1e-9
    ):
        failures.append(f"{path}: corpus eval max_page_duration_ms does not match cases")


def _check_benchmark_runs(
    runs: Any,
    summary: dict[str, Any],
    path: Path,
    *,
    failures: list[str],
) -> None:
    if not isinstance(runs, list) or not runs:
        failures.append(f"{path}: benchmark missing run details")
        return
    if not all(isinstance(item, dict) for item in runs):
        failures.append(f"{path}: benchmark run details must be objects")
        return
    run_count = summary.get("run_count")
    if isinstance(run_count, int) and len(runs) != run_count:
        failures.append(f"{path}: benchmark run count does not match summary")
    for index, item in enumerate(runs, start=1):
        if not isinstance(item.get("provider"), str) or not item.get("provider"):
            failures.append(f"{path}: benchmark run {index} missing provider")
        if not isinstance(item.get("model"), str) or not item.get("model"):
            failures.append(f"{path}: benchmark run {index} missing model")
        if not isinstance(item.get("input_chars"), int | float) or item.get("input_chars", 0) <= 0:
            failures.append(f"{path}: benchmark run {index} missing positive input_chars")
        if not isinstance(item.get("chunks"), int) or item.get("chunks", 0) <= 0:
            failures.append(f"{path}: benchmark run {index} missing positive chunks")
        for key in ("chunking_seconds", "cleaning_seconds", "total_seconds"):
            if not isinstance(item.get(key), int | float) or item.get(key, 0) < 0:
                failures.append(f"{path}: benchmark run {index} missing nonnegative {key}")
        cps = item.get("chars_per_second")
        if not isinstance(cps, int | float) or float(cps) <= 0:
            failures.append(f"{path}: benchmark run {index} missing positive chars_per_second")
        input_chars = item.get("input_chars")
        total_seconds = item.get("total_seconds")
        if (
            isinstance(input_chars, int | float)
            and float(input_chars) > 0
            and isinstance(total_seconds, int | float)
            and float(total_seconds) > 0
        ):
            expected_cps = float(input_chars) / float(total_seconds)
            if not _float_matches(cps, expected_cps):
                failures.append(f"{path}: benchmark run {index} chars_per_second is inconsistent")
        if not isinstance(item.get("chunk_target_chars"), int) or item.get(
            "chunk_target_chars", 0
        ) <= 0:
            failures.append(f"{path}: benchmark run {index} missing positive chunk_target_chars")
        overlap = item.get("chunk_overlap_chars")
        if not isinstance(overlap, int) or overlap < 0:
            failures.append(
                f"{path}: benchmark run {index} missing nonnegative chunk_overlap_chars"
            )


def _check_benchmark_aggregate_summary(
    runs: Any,
    summary: dict[str, Any],
    path: Path,
    *,
    failures: list[str],
) -> None:
    if not isinstance(runs, list) or not all(isinstance(item, dict) for item in runs):
        return
    input_chars = 0
    chunks = 0
    total_seconds = 0.0
    chars_per_second: list[float] = []
    for item in runs:
        run_input_chars = item.get("input_chars")
        if isinstance(run_input_chars, int | float) and run_input_chars >= 0:
            input_chars += int(run_input_chars)
        run_chunks = item.get("chunks")
        if isinstance(run_chunks, int) and run_chunks >= 0:
            chunks += run_chunks
        run_total_seconds = item.get("total_seconds")
        if isinstance(run_total_seconds, int | float) and float(run_total_seconds) >= 0:
            total_seconds += float(run_total_seconds)
        cps = item.get("chars_per_second")
        if isinstance(cps, int | float) and float(cps) > 0:
            chars_per_second.append(float(cps))

    if summary.get("total_input_chars") != input_chars:
        failures.append(f"{path}: benchmark total_input_chars does not match runs")
    if summary.get("total_chunks") != chunks:
        failures.append(f"{path}: benchmark total_chunks does not match runs")
    actual_total_seconds = summary.get("total_seconds")
    if (
        not isinstance(actual_total_seconds, int | float)
        or abs(float(actual_total_seconds) - total_seconds) > 1e-9
    ):
        failures.append(f"{path}: benchmark total_seconds does not match runs")
    expected_average_cps = (
        sum(chars_per_second) / len(chars_per_second) if chars_per_second else 0.0
    )
    actual_average_cps = summary.get("average_chars_per_second")
    if (
        not isinstance(actual_average_cps, int | float)
        or abs(float(actual_average_cps) - expected_average_cps) > 1e-9
    ):
        failures.append(f"{path}: benchmark average_chars_per_second does not match runs")
    expected_fastest_cps = max(chars_per_second, default=0.0)
    actual_fastest_cps = summary.get("fastest_chars_per_second")
    if (
        not isinstance(actual_fastest_cps, int | float)
        or abs(float(actual_fastest_cps) - expected_fastest_cps) > 1e-9
    ):
        failures.append(f"{path}: benchmark fastest_chars_per_second does not match runs")


def verify_eval(
    path: Path,
    *,
    require_real_provider: bool,
    min_cases: int,
    expected_version: str | None,
) -> list[str]:
    payload = _load_json(path)
    summary = _summary(payload, path)
    failures: list[str] = []
    provider = payload.get("provider")

    _check_artifact_type(
        payload,
        path,
        expected_type="librarian-eval-result",
        label="eval",
        failures=failures,
    )
    _check_evidence_tier(
        payload,
        path,
        providers={provider},
        require_real_provider=require_real_provider,
        label="eval",
        failures=failures,
    )
    _expect(payload.get("passed") is True, f"{path}: eval did not pass", failures)
    _check_generated_at(payload, path, label="eval", failures=failures)
    _expect(
        bool(payload.get("librarian_version")),
        f"{path}: eval missing librarian_version",
        failures,
    )
    _check_version(
        payload,
        path,
        expected_version=expected_version,
        label="eval",
        failures=failures,
    )
    _expect(bool(payload.get("model")), f"{path}: eval missing model", failures)
    _expect(
        summary.get("case_count", 0) >= min_cases,
        f"{path}: eval has too few cases",
        failures,
    )
    _check_pass_counts(summary, path, label="eval", failures=failures)
    _check_case_results(payload, summary, path, label="eval", failures=failures)
    _check_eval_case_metrics(payload, path, failures=failures)
    _check_eval_aggregate_summary(payload, summary, path, failures=failures)
    _expect(summary.get("failure_count") == 0, f"{path}: eval failures recorded", failures)
    _expect(
        payload.get("cleaning_prompt_version") == "cmos_v2",
        f"{path}: expected cleaning prompt cmos_v2",
        failures,
    )
    _expect(
        payload.get("classification_prompt_version") == "dewey_v2",
        f"{path}: expected classification prompt dewey_v2",
        failures,
    )
    if require_real_provider:
        _expect(provider not in {None, "mock"}, f"{path}: eval provider is not real", failures)
    return failures


def verify_corpus_eval(
    path: Path,
    *,
    require_real_provider: bool,
    min_search_recall: float,
    min_output_ratio: float,
    min_cases: int,
    expected_version: str | None,
) -> list[str]:
    payload = _load_json(path)
    summary = _summary(payload, path)
    failures: list[str] = []
    provider = payload.get("llm_provider")
    average_search_recall = summary.get("average_search_recall")
    total_input_bytes = summary.get("total_input_bytes")
    total_output_chars = summary.get("total_output_chars")

    _check_artifact_type(
        payload,
        path,
        expected_type="librarian-corpus-eval-result",
        label="corpus eval",
        failures=failures,
    )
    _check_evidence_tier(
        payload,
        path,
        providers={provider},
        require_real_provider=require_real_provider,
        label="corpus eval",
        failures=failures,
    )
    _expect(payload.get("passed") is True, f"{path}: corpus eval did not pass", failures)
    _check_generated_at(payload, path, label="corpus eval", failures=failures)
    _expect(
        bool(payload.get("librarian_version")),
        f"{path}: corpus eval missing librarian_version",
        failures,
    )
    _check_version(
        payload,
        path,
        expected_version=expected_version,
        label="corpus eval",
        failures=failures,
    )
    _expect(bool(payload.get("llm_model")), f"{path}: corpus eval missing llm_model", failures)
    _expect(
        summary.get("case_count", 0) >= min_cases,
        f"{path}: corpus eval has too few cases",
        failures,
    )
    _check_pass_counts(summary, path, label="corpus eval", failures=failures)
    _check_case_results(payload, summary, path, label="corpus eval", failures=failures)
    _check_corpus_case_metrics(payload, path, failures=failures)
    _check_corpus_aggregate_summary(payload, summary, path, failures=failures)
    _expect(summary.get("failure_count") == 0, f"{path}: corpus eval failures recorded", failures)
    _expect(
        isinstance(average_search_recall, int | float)
        and float(average_search_recall) >= min_search_recall,
        f"{path}: search recall below {min_search_recall}",
        failures,
    )
    if min_output_ratio > 0:
        if not isinstance(total_input_bytes, int | float) or float(total_input_bytes) <= 0:
            failures.append(f"{path}: corpus eval missing positive total_input_bytes")
        elif not isinstance(total_output_chars, int | float):
            failures.append(f"{path}: corpus eval missing total_output_chars")
        else:
            output_ratio = float(total_output_chars) / float(total_input_bytes)
            _expect(
                output_ratio >= min_output_ratio,
                f"{path}: corpus output ratio below {min_output_ratio}",
                failures,
            )
    _expect(
        payload.get("cleaning_prompt_version") == "cmos_v2",
        f"{path}: expected cleaning prompt cmos_v2",
        failures,
    )
    _expect(
        payload.get("classification_prompt_version") == "dewey_v2",
        f"{path}: expected classification prompt dewey_v2",
        failures,
    )
    if require_real_provider:
        _expect(
            provider not in {None, "mock"},
            f"{path}: corpus eval provider is not real",
            failures,
        )
    return failures


def verify_benchmark(
    path: Path,
    *,
    require_real_provider: bool,
    min_chars_per_second: float,
    min_runs: int,
    expected_version: str | None,
) -> list[str]:
    payload = _load_json(path)
    summary = _summary(payload, path)
    failures: list[str] = []
    runs = payload.get("runs")
    average_cps = summary.get("average_chars_per_second")
    providers = (
        {item.get("provider") for item in runs if isinstance(item, dict)}
        if isinstance(runs, list)
        else set()
    )

    _check_artifact_type(
        payload,
        path,
        expected_type="librarian-benchmark-result",
        label="benchmark",
        failures=failures,
    )
    _check_evidence_tier(
        payload,
        path,
        providers=providers,
        require_real_provider=require_real_provider,
        label="benchmark",
        failures=failures,
    )
    _expect(
        isinstance(runs, list) and len(runs) >= min_runs,
        f"{path}: too few benchmark runs",
        failures,
    )
    _check_benchmark_runs(runs, summary, path, failures=failures)
    _check_benchmark_aggregate_summary(runs, summary, path, failures=failures)
    _check_generated_at(payload, path, label="benchmark", failures=failures)
    _expect(
        bool(payload.get("librarian_version")),
        f"{path}: benchmark missing librarian_version",
        failures,
    )
    _check_version(
        payload,
        path,
        expected_version=expected_version,
        label="benchmark",
        failures=failures,
    )
    _expect(
        isinstance(average_cps, int | float) and float(average_cps) >= min_chars_per_second,
        f"{path}: benchmark throughput below {min_chars_per_second} chars/sec",
        failures,
    )
    _expect(
        payload.get("cleaning_prompt_version") == "cmos_v2",
        f"{path}: expected cleaning prompt cmos_v2",
        failures,
    )
    if require_real_provider and isinstance(runs, list):
        _expect(
            bool(providers) and "mock" not in providers and None not in providers,
            f"{path}: benchmark provider is not real",
            failures,
        )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval", dest="eval_paths", action="append", default=[], type=Path)
    parser.add_argument("--corpus-eval", action="append", default=[], type=Path)
    parser.add_argument("--benchmark", action="append", default=[], type=Path)
    parser.add_argument("--require-real-provider", action="store_true")
    parser.add_argument("--min-corpus-search-recall", type=float, default=1.0)
    parser.add_argument("--min-corpus-output-ratio", type=float, default=0.0)
    parser.add_argument("--min-corpus-cases", type=int, default=1)
    parser.add_argument("--min-eval-cases", type=int, default=1)
    parser.add_argument("--min-benchmark-cps", type=float, default=1.0)
    parser.add_argument("--min-benchmark-runs", type=int, default=1)
    parser.add_argument("--version", help="Expected Librarian release version or tag.")
    args = parser.parse_args(argv)

    failures: list[str] = []
    for path in args.eval_paths:
        try:
            failures.extend(
                verify_eval(
                    path,
                    require_real_provider=args.require_real_provider,
                    min_cases=args.min_eval_cases,
                    expected_version=args.version,
                )
            )
        except ValueError as exc:
            failures.append(str(exc))
    for path in args.corpus_eval:
        try:
            failures.extend(
                verify_corpus_eval(
                    path,
                    require_real_provider=args.require_real_provider,
                    min_search_recall=args.min_corpus_search_recall,
                    min_output_ratio=args.min_corpus_output_ratio,
                    min_cases=args.min_corpus_cases,
                    expected_version=args.version,
                )
            )
        except ValueError as exc:
            failures.append(str(exc))
    for path in args.benchmark:
        try:
            failures.extend(
                verify_benchmark(
                    path,
                    require_real_provider=args.require_real_provider,
                    min_chars_per_second=args.min_benchmark_cps,
                    min_runs=args.min_benchmark_runs,
                    expected_version=args.version,
                )
            )
        except ValueError as exc:
            failures.append(str(exc))

    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
