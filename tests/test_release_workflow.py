import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType


def _load_release_notes_module() -> ModuleType:
    loader = SourceFileLoader(
        "release_notes",
        str(Path(".github/scripts/release_notes.py")),
    )
    module = ModuleType(loader.name)
    loader.exec_module(module)
    return module


def _load_check_changelog_ready_module() -> ModuleType:
    loader = SourceFileLoader(
        "check_changelog_ready",
        str(Path(".github/scripts/check_changelog_ready.py")),
    )
    module = ModuleType(loader.name)
    loader.exec_module(module)
    return module


def _load_release_evidence_module() -> ModuleType:
    loader = SourceFileLoader(
        "verify_release_evidence",
        str(Path(".github/scripts/verify_release_evidence.py")),
    )
    module = ModuleType(loader.name)
    loader.exec_module(module)
    return module


def _load_export_constraints_module() -> ModuleType:
    loader = SourceFileLoader(
        "export_constraints",
        str(Path(".github/scripts/export_constraints.py")),
    )
    module = ModuleType(loader.name)
    loader.exec_module(module)
    return module


def test_release_workflow_checks_ocr_dependencies() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "poppler-utils tesseract-ocr" in workflow
    assert "librarian doctor --strict" in workflow


def test_release_workflow_publishes_verifiable_artifacts() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "group: release-${{ github.ref }}" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "actions/attest-build-provenance" in workflow
    assert "Attest release metadata" in workflow
    assert 'python -m venv "$RUNNER_TEMP/librarian-wheel-smoke"' in workflow
    assert "export_constraints.py --output constraints.txt" in workflow
    assert workflow.index("export_constraints.py --output constraints.txt") < workflow.index(
        "Smoke install wheel"
    )
    assert (
        '"$RUNNER_TEMP/librarian-wheel-smoke/bin/python" -m pip install -c '
        'constraints.txt "$WHEEL[all]"'
    ) in workflow
    assert '"$RUNNER_TEMP/librarian-wheel-smoke/bin/librarian" version' in workflow
    assert "cyclonedx-py environment --output-format JSON --output-file sbom.json" in workflow
    assert (
        "sha256sum dist/* sbom.json constraints.txt release-evidence/*.json > SHA256SUMS.txt"
        in workflow
    )
    assert "sha256sum --check SHA256SUMS.txt" in workflow
    assert "SHA256SUMS.txt" in workflow
    assert "name: release-${{ github.ref_name }}" in workflow
    assert "retention-days: 30" in workflow
    assert "constraints.txt" in workflow
    assert "release-evidence/*.json" in workflow
    assert workflow.index("Attest release metadata") > workflow.index("sha256sum --check")
    assert workflow.index("Attest release metadata") < workflow.index("Upload distributions")
    assert "gh release create" in workflow
    assert "dist/* sbom.json constraints.txt SHA256SUMS.txt release-evidence/*.json" in workflow
    assert "release_notes.py --version" in workflow
    assert "--notes-file release-notes.md" in workflow
    assert "--verify-tag" in workflow
    assert "--notes-file CHANGELOG.md" not in workflow


def test_release_workflow_scans_secrets_before_build() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "secret-scan:" in workflow
    assert "needs: secret-scan" in workflow
    assert "timeout-minutes: 15" in workflow
    assert "timeout-minutes: 90" in workflow
    assert "permissions:\n      contents: read" in workflow
    assert "fetch-depth: 0" in workflow
    assert "zricethezav/gitleaks:v8.30.1" in workflow
    assert "detect --source . --no-banner --redact --verbose" in workflow
    assert workflow.index("zricethezav/gitleaks:v8.30.1") < workflow.index("python -m build")
    assert workflow.index("security-events: write") > workflow.index("needs: secret-scan")


def test_release_workflow_rejects_tag_package_version_mismatch() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "from librarian.version import __version__" in workflow
    assert 'tag = os.environ["GITHUB_REF_NAME"]' in workflow
    assert 'expected = f"v{__version__}"' in workflow
    assert "release tag" in workflow
    assert workflow.index("Verify release tag matches package version") < workflow.index(
        "python -m build"
    )


def test_release_workflow_rejects_unprepared_changelog_before_build() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "check_changelog_ready.py --version \"$GITHUB_REF_NAME\"" in workflow
    assert workflow.index("check_changelog_ready.py") < workflow.index("python -m build")


def test_release_workflow_runs_dependency_audit_before_build() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert 'python -m pip install --upgrade "pip>=26.1"' in workflow
    assert workflow.index('python -m pip install --upgrade "pip>=26.1"') < workflow.index(
        "pip-audit --progress-spinner off --skip-editable"
    )
    assert "pip-audit --progress-spinner off --skip-editable" in workflow
    assert workflow.index("pip-audit --progress-spinner off --skip-editable") < workflow.index(
        "python -m build"
    )


def test_release_workflow_uploads_trivy_sarif_only_when_present() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "hashFiles('trivy-image.sarif') != ''" in workflow


def test_release_workflow_runs_synthetic_corpus_eval_before_build() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "rm -rf dist" in workflow
    corpus_eval_index = workflow.index(
        "librarian corpus-eval examples/synthetic-corpus/corpus_eval_cases.json"
    )
    prompt_eval_index = workflow.index("librarian eval examples/eval_cases.json")
    benchmark_index = workflow.index("librarian benchmark --input-path examples/benchmark_text.txt")
    verifier_index = workflow.index(".github/scripts/verify_release_evidence.py")
    build_index = workflow.index("python -m build")
    assert corpus_eval_index < build_index
    assert prompt_eval_index < build_index
    assert benchmark_index < build_index
    assert verifier_index < build_index
    assert "--output-dir" in workflow
    assert "mkdir -p release-evidence" in workflow
    assert "--output release-evidence/corpus-eval-mock.json" in workflow
    assert "--overwrite" in workflow
    assert "--output release-evidence/eval-mock.json" in workflow
    assert "--output release-evidence/benchmark-mock.json" in workflow
    assert "--eval release-evidence/eval-mock.json" in workflow
    assert "--corpus-eval release-evidence/corpus-eval-mock.json" in workflow
    assert "--benchmark release-evidence/benchmark-mock.json" in workflow
    assert "--version \"$GITHUB_REF_NAME\"" in workflow
    assert "--min-corpus-cases 11" in workflow
    assert "--min-corpus-search-recall 1.0" in workflow
    assert "--min-corpus-output-ratio 0.05" in workflow
    assert "--min-benchmark-cps 1000" in workflow
    assert "--min-benchmark-runs 1" in workflow


def test_release_docs_use_one_release_version_variable() -> None:
    release_doc = Path("docs/RELEASE.md").read_text(encoding="utf-8")

    assert "from librarian.version import __version__; print(__version__)" in release_doc
    assert '--version "v${RELEASE_VERSION}"' in release_doc
    assert "--min-corpus-output-ratio 0.05" in release_doc
    assert 'git tag "v${RELEASE_VERSION}"' in release_doc
    assert 'git push origin "v${RELEASE_VERSION}"' in release_doc
    assert "git tag v0.1.0a4" not in release_doc
    assert "git push origin v0.1.0a4" not in release_doc


def test_release_docs_include_manual_dependency_audit_gate() -> None:
    release_doc = Path("docs/RELEASE.md").read_text(encoding="utf-8")

    assert 'python -m pip install --upgrade "pip>=26.1"' in release_doc
    assert release_doc.index('python -m pip install --upgrade "pip>=26.1"') < (
        release_doc.index("pip-audit --progress-spinner off --skip-editable")
    )
    assert "pip-audit --progress-spinner off --skip-editable" in release_doc
    assert release_doc.index("pip-audit --progress-spinner off --skip-editable") < (
        release_doc.index("python -m build")
    )


def test_release_docs_install_with_exported_constraints() -> None:
    release_doc = Path("docs/RELEASE.md").read_text(encoding="utf-8")

    assert "constraints.txt" in release_doc
    install_command = (
        'pip install -c constraints.txt '
        '"nampara_librarian-${RELEASE_VERSION}-py3-none-any.whl[all]"'
    )
    assert install_command in release_doc


def test_supply_chain_docs_include_reproducibility_notes() -> None:
    supply_chain_doc = Path("docs/SUPPLY_CHAIN.md").read_text(encoding="utf-8")
    release_doc = Path("docs/RELEASE.md").read_text(encoding="utf-8")

    assert "## Reproducibility Notes" in supply_chain_doc
    assert "not byte-for-byte reproducible" in supply_chain_doc
    assert "constraints.txt" in supply_chain_doc
    assert "SHA256SUMS.txt" in supply_chain_doc
    assert "GitHub artifact attestations" in supply_chain_doc
    assert "python .github/scripts/export_constraints.py --output constraints.txt" in (
        supply_chain_doc
    )
    assert "sha256sum dist/* constraints.txt" in supply_chain_doc
    assert "docs/SUPPLY_CHAIN.md#reproducibility-notes" in release_doc


def test_secret_scan_docs_use_pinned_container_command() -> None:
    release_doc = Path("docs/RELEASE.md").read_text(encoding="utf-8")
    supply_chain_doc = Path("docs/SUPPLY_CHAIN.md").read_text(encoding="utf-8")
    threat_model_doc = Path("docs/THREAT_MODEL.md").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")
    stale_binary_command = "gitleaks detect --source . --redact --verbose"

    assert "zricethezav/gitleaks:v8.30.1" in release_doc
    assert "zricethezav/gitleaks:v8.30.1" in supply_chain_doc
    assert "docs/SUPPLY_CHAIN.md" in threat_model_doc
    assert "docs/SUPPLY_CHAIN.md" in readme
    assert stale_binary_command not in release_doc
    assert stale_binary_command not in supply_chain_doc
    assert stale_binary_command not in threat_model_doc
    assert stale_binary_command not in readme


def test_changelog_unreleased_section_is_ready_for_development_or_release() -> None:
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("## Unreleased", maxsplit=1)[1].split("## ", maxsplit=1)[0]

    assert "No unreleased changes." not in unreleased


def test_mock_eval_baseline_is_marked_as_manifest_not_release_evidence() -> None:
    payload = json.loads(Path("examples/baselines/mock-eval.json").read_text(encoding="utf-8"))

    assert payload["artifact_type"] == "baseline-manifest"
    assert "not a release evidence artifact" in payload["notes"]
    assert "generated_at" not in payload


def test_release_notes_extracts_only_requested_changelog_section() -> None:
    release_notes = _load_release_notes_module()

    notes = release_notes.extract_release_notes(
        """# Changelog

## 0.1.0a4 - 2026-05-09

- current release

## 0.1.0a2 - 2026-05-06

- older release
""",
        "v0.1.0a4",
    )

    assert "0.1.0a4 - 2026-05-09" in notes
    assert "current release" in notes
    assert "older release" not in notes


def test_changelog_release_guard_accepts_drained_unreleased_section() -> None:
    checker = _load_check_changelog_ready_module()

    checker.validate_changelog_ready(
        """# Changelog

## Unreleased

## 0.1.0a4 - 2026-05-13

- release entry
""",
        "v0.1.0a4",
    )


def test_changelog_release_guard_rejects_unreleased_entries() -> None:
    checker = _load_check_changelog_ready_module()

    try:
        checker.validate_changelog_ready(
            """# Changelog

## Unreleased

- not moved yet

## 0.1.0a4 - 2026-05-13

- release entry
""",
            "v0.1.0a4",
        )
    except ValueError as exc:
        assert "Unreleased entries" in str(exc)
    else:
        raise AssertionError("expected changelog guard to reject unreleased entries")


def test_export_constraints_exports_registry_pins_only() -> None:
    exporter = _load_export_constraints_module()

    constraints = exporter.export_constraints(
        """
        version = 1

        [[package]]
        name = "nampara-librarian"
        version = "0.1.0a4"
        source = { editable = "." }

        [[package]]
        name = "urllib3"
        version = "2.7.0"
        source = { registry = "https://pypi.org/simple" }

        [[package]]
        name = "typing_extensions"
        version = "4.15.0"
        source = { registry = "https://pypi.org/simple" }
        """
    )

    assert "nampara-librarian" not in constraints
    assert "typing-extensions==4.15.0" in constraints
    assert "urllib3==2.7.0" in constraints


def test_release_evidence_verifier_accepts_passing_artifacts(tmp_path: Path) -> None:
    verifier = _load_release_evidence_module()
    eval_path = tmp_path / "eval.json"
    corpus_path = tmp_path / "corpus.json"
    benchmark_path = tmp_path / "benchmark.json"
    eval_path.write_text(
        """
        {
          "artifact_type": "librarian-eval-result",
          "evidence_tier": "real-provider",
          "passed": true,
          "provider": "openai-compatible",
          "model": "gpt-4.1-mini",
          "generated_at": "2026-05-13T00:00:00+00:00",
          "librarian_version": "0.1.0a4",
          "cleaning_prompt_version": "cmos_v2",
          "classification_prompt_version": "dewey_v2",
          "summary": {"case_count": 2, "passed_count": 2, "failed_count": 0, "failure_count": 0},
          "cases": [
            {
              "name": "case one",
              "passed": true,
              "tags": ["provider"],
              "input_chars": 100,
              "output_chars": 95,
              "output_char_ratio": 0.95,
              "duration_seconds": 1.0,
              "chars_per_second": 100,
              "classification_code": "636.1",
              "classification_label": "Horses",
              "warnings": [],
              "failures": []
            },
            {
              "name": "case two",
              "passed": true,
              "tags": ["provider"],
              "input_chars": 120,
              "output_chars": 110,
              "output_char_ratio": 0.92,
              "duration_seconds": 1.2,
              "chars_per_second": 100,
              "classification_code": "020",
              "classification_label": "Library science",
              "warnings": [],
              "failures": []
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    corpus_path.write_text(
        """
        {
          "artifact_type": "librarian-corpus-eval-result",
          "evidence_tier": "real-provider",
          "passed": true,
          "llm_provider": "openai-compatible",
          "llm_model": "gpt-4.1-mini",
          "generated_at": "2026-05-13T00:00:00+00:00",
          "librarian_version": "0.1.0a4",
          "cleaning_prompt_version": "cmos_v2",
          "classification_prompt_version": "dewey_v2",
          "summary": {
            "case_count": 8,
            "passed_count": 8,
            "failed_count": 0,
            "failure_count": 0,
            "average_search_recall": 1.0,
            "total_input_bytes": 1000,
            "total_output_chars": 750,
            "total_page_attempts": 2,
            "total_failed_pages": 0,
            "max_page_duration_ms": 10.0
          },
          "cases": [
            {
              "name": "case one",
              "passed": true,
              "tags": ["markdown"],
              "source_path": "corpus/one.md",
              "output_path": "converted/one.md",
              "input_bytes": 125,
              "output_chars": 100,
              "output_char_ratio": 0.8,
              "conversion_seconds": 0.1,
              "processing_seconds": 0.2,
              "peak_memory_bytes": 1000,
              "page_status_counts": {},
              "page_source_counts": {},
              "page_warning_counts": {},
              "page_attempts": 0,
              "max_page_duration_ms": null,
              "ocr_pages": 0,
              "corrected_pages": 0,
              "average_ocr_confidence": null,
              "search_recall": 1.0,
              "search_diagnostics": [
                {
                  "phrase": "anchor",
                  "hit": true,
                  "total_results": 1,
                  "returned_document_ids": ["doc_1"],
                  "error": null
                }
              ],
              "classification_code": "636.1",
              "classification_label": "Horses",
              "failures": []
            },
            {
              "name": "case two",
              "passed": true,
              "tags": ["markdown"],
              "source_path": "corpus/two.md",
              "output_path": "converted/two.md",
              "input_bytes": 125,
              "output_chars": 100,
              "output_char_ratio": 0.8,
              "conversion_seconds": 0.1,
              "processing_seconds": 0.2,
              "peak_memory_bytes": 1000,
              "page_status_counts": {},
              "page_source_counts": {},
              "page_warning_counts": {},
              "page_attempts": 0,
              "max_page_duration_ms": null,
              "ocr_pages": 0,
              "corrected_pages": 0,
              "average_ocr_confidence": null,
              "search_recall": 1.0,
              "search_diagnostics": [
                {
                  "phrase": "anchor",
                  "hit": true,
                  "total_results": 1,
                  "returned_document_ids": ["doc_2"],
                  "error": null
                }
              ],
              "classification_code": "020",
              "classification_label": "Library science",
              "failures": []
            },
            {
              "name": "case three",
              "passed": true,
              "tags": ["markdown"],
              "source_path": "corpus/three.md",
              "output_path": "converted/three.md",
              "input_bytes": 125,
              "output_chars": 100,
              "output_char_ratio": 0.8,
              "conversion_seconds": 0.1,
              "processing_seconds": 0.2,
              "peak_memory_bytes": 1000,
              "page_status_counts": {},
              "page_source_counts": {},
              "page_warning_counts": {},
              "page_attempts": 0,
              "max_page_duration_ms": null,
              "ocr_pages": 0,
              "corrected_pages": 0,
              "average_ocr_confidence": null,
              "search_recall": 1.0,
              "search_diagnostics": [
                {
                  "phrase": "anchor",
                  "hit": true,
                  "total_results": 1,
                  "returned_document_ids": ["doc_3"],
                  "error": null
                }
              ],
              "classification_code": "610",
              "classification_label": "Medicine",
              "failures": []
            },
            {
              "name": "case four",
              "passed": true,
              "tags": ["markdown"],
              "source_path": "corpus/four.md",
              "output_path": "converted/four.md",
              "input_bytes": 125,
              "output_chars": 100,
              "output_char_ratio": 0.8,
              "conversion_seconds": 0.1,
              "processing_seconds": 0.2,
              "peak_memory_bytes": 1000,
              "page_status_counts": {},
              "page_source_counts": {},
              "page_warning_counts": {},
              "page_attempts": 0,
              "max_page_duration_ms": null,
              "ocr_pages": 0,
              "corrected_pages": 0,
              "average_ocr_confidence": null,
              "search_recall": 1.0,
              "search_diagnostics": [
                {
                  "phrase": "anchor",
                  "hit": true,
                  "total_results": 1,
                  "returned_document_ids": ["doc_4"],
                  "error": null
                }
              ],
              "classification_code": "800",
              "classification_label": "Literature",
              "failures": []
            },
            {
              "name": "case five",
              "passed": true,
              "tags": ["pdf"],
              "source_path": "corpus/five.pdf",
              "output_path": "converted/five.md",
              "input_bytes": 125,
              "output_chars": 100,
              "output_char_ratio": 0.8,
              "conversion_seconds": 0.1,
              "processing_seconds": 0.2,
              "peak_memory_bytes": 1000,
              "page_status_counts": {"succeeded": 1},
              "page_source_counts": {"embedded": 1},
              "page_warning_counts": {},
              "page_attempts": 0,
              "max_page_duration_ms": null,
              "ocr_pages": 0,
              "corrected_pages": 0,
              "average_ocr_confidence": null,
              "search_recall": 1.0,
              "search_diagnostics": [
                {
                  "phrase": "anchor",
                  "hit": true,
                  "total_results": 1,
                  "returned_document_ids": ["doc_5"],
                  "error": null
                }
              ],
              "classification_code": "636.1",
              "classification_label": "Horses",
              "failures": []
            },
            {
              "name": "case six",
              "passed": true,
              "tags": ["pdf", "ocr"],
              "source_path": "corpus/six.pdf",
              "output_path": "converted/six.md",
              "input_bytes": 125,
              "output_chars": 100,
              "output_char_ratio": 0.8,
              "conversion_seconds": 0.1,
              "processing_seconds": 0.2,
              "peak_memory_bytes": 1000,
              "page_status_counts": {"succeeded": 1},
              "page_source_counts": {"ocr": 1},
              "page_warning_counts": {},
              "page_attempts": 1,
              "max_page_duration_ms": 10.0,
              "ocr_pages": 1,
              "corrected_pages": 0,
              "average_ocr_confidence": 90.0,
              "search_recall": 1.0,
              "search_diagnostics": [
                {
                  "phrase": "anchor",
                  "hit": true,
                  "total_results": 1,
                  "returned_document_ids": ["doc_6"],
                  "error": null
                }
              ],
              "classification_code": "610",
              "classification_label": "Medicine",
              "failures": []
            },
            {
              "name": "case seven",
              "passed": true,
              "tags": ["docx"],
              "source_path": "corpus/seven.docx",
              "output_path": "converted/seven.md",
              "input_bytes": 125,
              "output_chars": 100,
              "output_char_ratio": 0.8,
              "conversion_seconds": 0.1,
              "processing_seconds": 0.2,
              "peak_memory_bytes": 1000,
              "page_status_counts": {},
              "page_source_counts": {},
              "page_warning_counts": {},
              "page_attempts": 0,
              "max_page_duration_ms": null,
              "ocr_pages": 0,
              "corrected_pages": 0,
              "average_ocr_confidence": null,
              "search_recall": 1.0,
              "search_diagnostics": [
                {
                  "phrase": "anchor",
                  "hit": true,
                  "total_results": 1,
                  "returned_document_ids": ["doc_7"],
                  "error": null
                }
              ],
              "classification_code": "020",
              "classification_label": "Library science",
              "failures": []
            },
            {
              "name": "case eight",
              "passed": true,
              "tags": ["pdf", "mixed-embedded-scanned"],
              "source_path": "corpus/eight.pdf",
              "output_path": "converted/eight.md",
              "input_bytes": 125,
              "output_chars": 100,
              "output_char_ratio": 0.8,
              "conversion_seconds": 0.1,
              "processing_seconds": 0.2,
              "peak_memory_bytes": 1000,
              "page_status_counts": {"succeeded": 2},
              "page_source_counts": {"embedded": 1, "ocr": 1},
              "page_warning_counts": {},
              "page_attempts": 1,
              "max_page_duration_ms": 10.0,
              "ocr_pages": 1,
              "corrected_pages": 0,
              "average_ocr_confidence": 90.0,
              "search_recall": 1.0,
              "search_diagnostics": [
                {
                  "phrase": "anchor",
                  "hit": true,
                  "total_results": 1,
                  "returned_document_ids": ["doc_8"],
                  "error": null
                }
              ],
              "classification_code": "636.1",
              "classification_label": "Horses",
              "failures": []
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    benchmark_path.write_text(
        """
        {
          "artifact_type": "librarian-benchmark-result",
          "evidence_tier": "real-provider",
          "generated_at": "2026-05-13T00:00:00+00:00",
          "librarian_version": "0.1.0a4",
          "cleaning_prompt_version": "cmos_v2",
          "summary": {"run_count": 1, "average_chars_per_second": 1000},
          "runs": [
            {
              "provider": "openai-compatible",
              "model": "gpt-4.1-mini",
              "input_chars": 100,
              "chunks": 1,
              "chunking_seconds": 0.01,
              "cleaning_seconds": 0.09,
              "total_seconds": 0.1,
              "chars_per_second": 1000,
              "chunk_target_chars": 8000,
              "chunk_overlap_chars": 400
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    assert (
        verifier.main(
            [
                "--eval",
                str(eval_path),
                "--corpus-eval",
                str(corpus_path),
                "--benchmark",
                str(benchmark_path),
                "--require-real-provider",
                "--min-corpus-cases",
                "8",
                "--min-benchmark-cps",
                "10",
                "--min-corpus-output-ratio",
                "0.5",
                "--version",
                "v0.1.0a4",
            ]
        )
        == 0
    )


def test_release_evidence_verifier_rejects_mock_or_failed_artifacts(tmp_path: Path) -> None:
    verifier = _load_release_evidence_module()
    eval_path = tmp_path / "eval.json"
    eval_path.write_text(
        """
        {
          "artifact_type": "librarian-eval-result",
          "evidence_tier": "mock-smoke",
          "passed": false,
          "provider": "mock",
          "model": "mock-cleaner",
          "generated_at": "2026-05-13T00:00:00+00:00",
          "librarian_version": "0.1.0a4",
          "cleaning_prompt_version": "cmos_v2",
          "classification_prompt_version": "dewey_v2",
          "summary": {"case_count": 1, "passed_count": 0, "failed_count": 1, "failure_count": 1}
        }
        """,
        encoding="utf-8",
    )

    assert verifier.main(["--eval", str(eval_path), "--require-real-provider"]) == 1


def test_release_evidence_verifier_rejects_mismatched_evidence_tier(tmp_path: Path) -> None:
    verifier = _load_release_evidence_module()
    eval_path = tmp_path / "eval.json"
    eval_path.write_text(
        """
        {
          "artifact_type": "librarian-eval-result",
          "evidence_tier": "real-provider",
          "passed": true,
          "provider": "mock",
          "model": "mock-cleaner",
          "generated_at": "2026-05-13T00:00:00+00:00",
          "librarian_version": "0.1.0a4",
          "cleaning_prompt_version": "cmos_v2",
          "classification_prompt_version": "dewey_v2",
          "summary": {"case_count": 1, "passed_count": 1, "failed_count": 0, "failure_count": 0}
        }
        """,
        encoding="utf-8",
    )

    assert verifier.main(["--eval", str(eval_path)]) == 1


def test_release_evidence_verifier_rejects_hidden_case_failures(tmp_path: Path) -> None:
    verifier = _load_release_evidence_module()
    eval_path = tmp_path / "eval.json"
    eval_path.write_text(
        """
        {
          "artifact_type": "librarian-eval-result",
          "evidence_tier": "real-provider",
          "passed": true,
          "provider": "openai-compatible",
          "model": "gpt-4.1-mini",
          "generated_at": "2026-05-13T00:00:00+00:00",
          "librarian_version": "0.1.0a4",
          "cleaning_prompt_version": "cmos_v2",
          "classification_prompt_version": "dewey_v2",
          "summary": {"case_count": 1, "passed_count": 1, "failed_count": 0, "failure_count": 0},
          "cases": [{"passed": false, "failures": ["missing expected text"]}]
        }
        """,
        encoding="utf-8",
    )

    assert verifier.main(["--eval", str(eval_path)]) == 1


def test_release_evidence_verifier_rejects_incomplete_case_metrics(tmp_path: Path) -> None:
    verifier = _load_release_evidence_module()
    eval_path = tmp_path / "eval.json"
    benchmark_path = tmp_path / "benchmark.json"
    eval_path.write_text(
        """
        {
          "artifact_type": "librarian-eval-result",
          "evidence_tier": "real-provider",
          "passed": true,
          "provider": "openai-compatible",
          "model": "gpt-4.1-mini",
          "generated_at": "2026-05-13T00:00:00+00:00",
          "librarian_version": "0.1.0a4",
          "cleaning_prompt_version": "cmos_v2",
          "classification_prompt_version": "dewey_v2",
          "summary": {"case_count": 1, "passed_count": 1, "failed_count": 0, "failure_count": 0},
          "cases": [{"passed": true, "failures": []}]
        }
        """,
        encoding="utf-8",
    )
    benchmark_path.write_text(
        """
        {
          "artifact_type": "librarian-benchmark-result",
          "evidence_tier": "real-provider",
          "generated_at": "2026-05-13T00:00:00+00:00",
          "librarian_version": "0.1.0a4",
          "cleaning_prompt_version": "cmos_v2",
          "summary": {"run_count": 1, "average_chars_per_second": 1000},
          "runs": [{"provider": "openai-compatible", "input_chars": 100, "chars_per_second": 1000}]
        }
        """,
        encoding="utf-8",
    )

    assert verifier.main(["--eval", str(eval_path), "--benchmark", str(benchmark_path)]) == 1


def test_release_evidence_verifier_rejects_invalid_result_shape(tmp_path: Path) -> None:
    verifier = _load_release_evidence_module()
    eval_path = tmp_path / "eval.json"
    eval_path.write_text('{"passed": true}', encoding="utf-8")

    assert verifier.main(["--eval", str(eval_path)]) == 1


def test_release_evidence_verifier_rejects_version_mismatch(tmp_path: Path) -> None:
    verifier = _load_release_evidence_module()
    eval_path = tmp_path / "eval.json"
    eval_path.write_text(
        """
        {
          "artifact_type": "librarian-eval-result",
          "evidence_tier": "real-provider",
          "passed": true,
          "provider": "openai-compatible",
          "model": "gpt-4.1-mini",
          "generated_at": "2026-05-13T00:00:00+00:00",
          "librarian_version": "0.1.0a3",
          "cleaning_prompt_version": "cmos_v2",
          "classification_prompt_version": "dewey_v2",
          "summary": {"case_count": 1, "passed_count": 1, "failed_count": 0, "failure_count": 0}
        }
        """,
        encoding="utf-8",
    )

    assert verifier.main(["--eval", str(eval_path), "--version", "v0.1.0a4"]) == 1


def test_release_evidence_verifier_rejects_bad_timestamps_and_counts(
    tmp_path: Path,
) -> None:
    verifier = _load_release_evidence_module()
    eval_path = tmp_path / "eval.json"
    eval_path.write_text(
        """
        {
          "artifact_type": "librarian-eval-result",
          "evidence_tier": "real-provider",
          "passed": true,
          "provider": "openai-compatible",
          "model": "gpt-4.1-mini",
          "generated_at": "2026-05-13",
          "librarian_version": "0.1.0a4",
          "cleaning_prompt_version": "cmos_v2",
          "classification_prompt_version": "dewey_v2",
          "summary": {"case_count": 2, "passed_count": 2, "failed_count": 1, "failure_count": 0}
        }
        """,
        encoding="utf-8",
    )

    assert verifier.main(["--eval", str(eval_path), "--version", "v0.1.0a4"]) == 1


def test_release_evidence_verifier_rejects_low_corpus_output_ratio(tmp_path: Path) -> None:
    verifier = _load_release_evidence_module()
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(
        """
        {
          "artifact_type": "librarian-corpus-eval-result",
          "evidence_tier": "real-provider",
          "passed": true,
          "llm_provider": "openai-compatible",
          "llm_model": "gpt-4.1-mini",
          "generated_at": "2026-05-13T00:00:00+00:00",
          "librarian_version": "0.1.0a4",
          "cleaning_prompt_version": "cmos_v2",
          "classification_prompt_version": "dewey_v2",
          "summary": {
            "case_count": 8,
            "passed_count": 8,
            "failed_count": 0,
            "failure_count": 0,
            "average_search_recall": 1.0,
            "total_input_bytes": 1000,
            "total_output_chars": 10
          }
        }
        """,
        encoding="utf-8",
    )

    assert (
        verifier.main(
            [
                "--corpus-eval",
                str(corpus_path),
                "--min-corpus-cases",
                "8",
                "--min-corpus-output-ratio",
                "0.05",
                "--version",
                "v0.1.0a4",
            ]
        )
        == 1
    )


def test_release_evidence_verifier_rejects_mismatched_corpus_page_summary(
    tmp_path: Path,
) -> None:
    verifier = _load_release_evidence_module()
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(
        """
        {
          "artifact_type": "librarian-corpus-eval-result",
          "evidence_tier": "real-provider",
          "passed": true,
          "llm_provider": "openai-compatible",
          "llm_model": "gpt-4.1-mini",
          "generated_at": "2026-05-13T00:00:00+00:00",
          "librarian_version": "0.1.0a4",
          "cleaning_prompt_version": "cmos_v2",
          "classification_prompt_version": "dewey_v2",
          "summary": {
            "case_count": 1,
            "passed_count": 1,
            "failed_count": 0,
            "failure_count": 0,
            "average_search_recall": 1.0,
            "total_input_bytes": 1000,
            "total_output_chars": 800,
            "total_page_attempts": 0,
            "total_failed_pages": 0,
            "max_page_duration_ms": null
          },
          "cases": [
            {
              "name": "ocr case",
              "passed": true,
              "tags": ["pdf", "ocr"],
              "source_path": "corpus/one.pdf",
              "output_path": "converted/one.md",
              "input_bytes": 1000,
              "output_chars": 800,
              "output_char_ratio": 0.8,
              "conversion_seconds": 0.1,
              "processing_seconds": 0.2,
              "peak_memory_bytes": 1000,
              "page_status_counts": {"succeeded": 1},
              "page_source_counts": {"ocr": 1},
              "page_warning_counts": {},
              "page_attempts": 1,
              "max_page_duration_ms": 10.0,
              "ocr_pages": 1,
              "corrected_pages": 0,
              "average_ocr_confidence": 90.0,
              "search_recall": 1.0,
              "search_diagnostics": [
                {
                  "phrase": "anchor",
                  "hit": true,
                  "total_results": 1,
                  "returned_document_ids": ["doc_1"],
                  "error": null
                }
              ],
              "classification_code": "636.1",
              "classification_label": "Horses",
              "failures": []
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    assert verifier.main(["--corpus-eval", str(corpus_path), "--version", "v0.1.0a4"]) == 1
