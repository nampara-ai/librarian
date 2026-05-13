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


def verify_eval(
    path: Path,
    *,
    require_real_provider: bool,
    expected_version: str | None,
) -> list[str]:
    payload = _load_json(path)
    summary = _summary(payload, path)
    failures: list[str] = []
    provider = payload.get("provider")

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
    _expect(summary.get("case_count", 0) >= 1, f"{path}: eval has no cases", failures)
    _check_pass_counts(summary, path, label="eval", failures=failures)
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

    _expect(
        isinstance(runs, list) and len(runs) >= min_runs,
        f"{path}: too few benchmark runs",
        failures,
    )
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
        providers = {item.get("provider") for item in runs if isinstance(item, dict)}
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
