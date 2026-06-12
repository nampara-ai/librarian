from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

import pytest


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


def _load_export_constraints_module() -> ModuleType:
    loader = SourceFileLoader(
        "export_constraints",
        str(Path(".github/scripts/export_constraints.py")),
    )
    module = ModuleType(loader.name)
    loader.exec_module(module)
    return module


def test_release_workflow_keeps_production_supply_chain_gates() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "secret-scan:" in workflow
    assert "zricethezav/gitleaks:v8.30.1" in workflow
    assert "Verify release tag matches package version" in workflow
    assert 'check_changelog_ready.py --version "$GITHUB_REF_NAME"' in workflow
    assert "librarian doctor --strict" in workflow
    assert "ruff check ." in workflow
    assert "pyright" in workflow
    assert "pytest" in workflow
    assert "pip-audit --progress-spinner off --skip-editable" in workflow
    assert "python -m build" in workflow
    assert "export_constraints.py --output constraints.txt" in workflow
    assert "cyclonedx-py environment --output-format JSON --output-file sbom.json" in workflow
    assert "sha256sum dist/* sbom.json constraints.txt > SHA256SUMS.txt" in workflow
    assert "actions/attest-build-provenance" in workflow
    assert "aquasecurity/trivy-action" in workflow
    assert "gh release create" in workflow
    assert "release-evidence" not in workflow
    assert "verify_release_evidence.py" not in workflow


def test_release_workflow_smoke_installs_with_exported_constraints() -> None:
    workflow = Path(".github/workflows/release.yml").read_text(encoding="utf-8")

    assert 'python -m venv "$RUNNER_TEMP/librarian-wheel-smoke"' in workflow
    assert (
        '"$RUNNER_TEMP/librarian-wheel-smoke/bin/python" -m pip install -c '
        'constraints.txt "$WHEEL[all]"'
    ) in workflow
    assert '"$RUNNER_TEMP/librarian-wheel-smoke/bin/librarian" version' in workflow


def test_release_docs_describe_stable_surface() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    operations = Path("docs/OPERATIONS.md").read_text(encoding="utf-8")
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")

    assert "Version `1.2.0` is the stable production release." in readme
    assert "librarian admin page-manifest" in operations
    assert "librarian maintainer eval" in operations
    assert "## 1.0.0 - 2026-05-22" in changelog
    assert "0.1.0a" not in readme
    assert "docs/SUPPLY_CHAIN.md" not in readme


def test_release_notes_extracts_only_requested_changelog_section() -> None:
    module = _load_release_notes_module()
    changelog = """# Changelog

## 1.1.0 - 2026-06-01

- Future entry.

## 1.0.0 - 2026-05-22

Stable release text.

## 0.1.0a72 - 2026-05-14

- Old alpha.
"""

    notes = module.extract_release_notes(changelog, "v1.0.0")

    assert "Stable release text." in notes
    assert "Future entry" not in notes
    assert "Old alpha" not in notes


def test_changelog_release_guard_accepts_single_stable_section() -> None:
    module = _load_check_changelog_ready_module()
    changelog = """# Changelog

## 1.0.0 - 2026-05-22

Stable release text.
"""

    module.validate_changelog_ready(changelog, "v1.0.0")


def test_changelog_release_guard_rejects_pending_unreleased_entries() -> None:
    module = _load_check_changelog_ready_module()
    changelog = """# Changelog

## Unreleased

- Pending entry.

## 1.0.0 - 2026-05-22

Stable release text.
"""

    with pytest.raises(ValueError, match="Unreleased"):
        module.validate_changelog_ready(changelog, "v1.0.0")


def test_export_constraints_exports_registry_pins_only() -> None:
    module = _load_export_constraints_module()
    lock_text = """
[[package]]
name = "fastapi"
version = "0.115.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "nampara-librarian"
version = "1.0.0"
source = { editable = "." }
"""

    constraints = module.export_constraints(lock_text)

    assert "fastapi==0.115.0" in constraints
    assert "nampara-librarian" not in constraints
