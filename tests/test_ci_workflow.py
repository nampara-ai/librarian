from pathlib import Path
from typing import cast

import yaml


def test_github_workflows_are_valid_yaml() -> None:
    for path in Path(".github/workflows").glob("*.yml"):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))

        assert isinstance(payload, dict), path
        workflow = cast(dict[str, object], payload)
        assert isinstance(workflow.get("jobs"), dict), path


def test_ci_uses_read_only_token_permissions() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "permissions:\n  contents: read" in workflow
    assert "timeout-minutes: 45" in workflow


def test_workflows_do_not_persist_checkout_credentials() -> None:
    for path in Path(".github/workflows").glob("*.yml"):
        workflow = path.read_text(encoding="utf-8")
        if "actions/checkout" in workflow:
            assert "persist-credentials: false" in workflow, path


def test_secret_scan_workflow_runs_gitleaks_without_write_permissions() -> None:
    workflow = Path(".github/workflows/secrets.yml").read_text(encoding="utf-8")

    assert "zricethezav/gitleaks:v8.30.1" in workflow
    assert "detect --source . --no-banner --redact --verbose" in workflow
    assert "timeout-minutes: 15" in workflow
    assert "pull_request:" in workflow
    assert "schedule:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "contents: read" in workflow
    assert "pull-requests: read" in workflow
    assert "persist-credentials: false" in workflow


def test_dependency_review_workflow_blocks_high_severity_dependency_changes() -> None:
    workflow = Path(".github/workflows/dependency-review.yml").read_text(encoding="utf-8")

    assert "pull_request:" in workflow
    assert "timeout-minutes: 10" in workflow
    assert "permissions:\n  contents: read\n  pull-requests: read" in workflow
    assert "actions/dependency-review-action@v4" in workflow
    assert "fail-on-severity: high" in workflow
    assert "persist-credentials: false" in workflow


def test_dependabot_monitors_python_and_github_actions() -> None:
    payload = yaml.safe_load(Path(".github/dependabot.yml").read_text(encoding="utf-8"))

    assert isinstance(payload, dict)
    updates = cast(list[dict[str, object]], payload["updates"])
    assert isinstance(updates, list)
    ecosystems: dict[tuple[str, str], str] = {
        (
            str(item["package-ecosystem"]),
            str(item["directory"]),
        ): str(cast(dict[str, object], item["schedule"])["interval"])
        for item in updates
    }
    assert ecosystems[("pip", "/")] == "weekly"
    assert ecosystems[("github-actions", "/")] == "weekly"


def test_codeql_workflow_has_bounded_runtime_and_read_permissions() -> None:
    workflow = Path(".github/workflows/codeql.yml").read_text(encoding="utf-8")

    assert "timeout-minutes: 30" in workflow
    assert "contents: read" in workflow
    assert "security-events: write" in workflow


def test_ci_installs_and_checks_ocr_dependencies() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "poppler-utils tesseract-ocr" in workflow
    assert "librarian doctor --strict" in workflow


def test_ci_runs_example_corpus_eval() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "librarian corpus-eval examples/corpus_eval_cases.json" in workflow
    assert "librarian corpus-eval examples/synthetic-corpus/corpus_eval_cases.json" in workflow
    assert "--output-dir" in workflow
    assert '--output "$RUNNER_TEMP/ci-corpus-eval.json"' in workflow


def test_ci_runs_and_verifies_example_evidence() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "librarian eval examples/eval_cases.json" in workflow
    assert "librarian benchmark --paragraphs 40 --paragraph-chars 1000" in workflow
    assert "--repeats 1" in workflow
    assert '--output "$RUNNER_TEMP/ci-eval.json"' in workflow
    assert '--output "$RUNNER_TEMP/ci-benchmark.json"' in workflow
    assert ".github/scripts/verify_release_evidence.py" in workflow
    assert "--eval \"$RUNNER_TEMP/ci-eval.json\"" in workflow
    assert "--corpus-eval \"$RUNNER_TEMP/ci-corpus-eval.json\"" in workflow
    assert "--benchmark \"$RUNNER_TEMP/ci-benchmark.json\"" in workflow
    assert "--min-eval-cases 6" in workflow
    for tag in (
        "classification",
        "transcript",
        "legal",
        "technical",
        "no-summarization",
        "markdown",
        "ocr-correction",
    ):
        assert f"--require-eval-tag {tag}" in workflow
    assert "--min-corpus-cases 13" in workflow
    for tag in (
        "docx",
        "tables",
        "headers-footers",
        "pdf",
        "embedded-text",
        "scanned",
        "ocr",
        "noisy-ocr",
        "mixed-embedded-scanned",
        "transcript-caption",
        "srt",
        "vtt",
    ):
        assert f"--require-corpus-tag {tag}" in workflow
    assert "--min-corpus-search-recall 1.0" in workflow
    assert "--min-corpus-output-ratio 0.05" in workflow
    assert "--min-benchmark-cps 1000" in workflow
    assert "--min-benchmark-runs 1" in workflow
    assert "--min-benchmark-input-chars 40000" in workflow
    assert "--min-benchmark-chunks 4" in workflow
    verifier_index = workflow.index(".github/scripts/verify_release_evidence.py")
    build_index = workflow.index("python -m build")
    assert verifier_index < build_index


def test_ci_runs_dependency_audit_before_tests_and_build() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    audit_index = workflow.index("pip-audit --progress-spinner off --skip-editable")
    pytest_index = workflow.index("pytest")
    build_index = workflow.index("python -m build")
    assert audit_index < pytest_index
    assert audit_index < build_index


def test_ci_builds_python_distributions_before_docker_image() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "rm -rf dist" in workflow
    build_index = workflow.index("python -m build")
    smoke_index = workflow.index('"$RUNNER_TEMP/librarian-wheel-smoke/bin/librarian" version')
    docker_index = workflow.index("docker build -t librarian-ci .")
    assert build_index < docker_index
    assert build_index < smoke_index < docker_index
    assert '"$RUNNER_TEMP/librarian-wheel-smoke/bin/python" -m pip install dist/*.whl' in workflow
