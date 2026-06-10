import tomllib
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from shutil import which
from subprocess import run

import pytest


def test_wheel_includes_runtime_package_data() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]

    assert wheel["packages"] == ["src/librarian"]
    assert "src/librarian/prompts/**/*.md" in wheel["artifacts"]
    assert "src/librarian/storage/migrations/*.sql" in wheel["artifacts"]


def test_wheel_excludes_maintainer_harness() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]

    assert "src/librarian/maintainer" in wheel["exclude"]


def test_package_version_metadata_is_consistent() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    spec = spec_from_file_location("librarian_version", "src/librarian/version.py")
    assert spec is not None
    assert spec.loader is not None
    version_module = module_from_spec(spec)
    spec.loader.exec_module(version_module)

    assert pyproject["project"]["version"] == version_module.__version__


def test_dependencies_include_security_audit_tool_and_vulnerability_floors() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    runtime_dependencies = pyproject["project"]["dependencies"]
    dev_dependencies = pyproject["project"]["optional-dependencies"]["dev"]

    assert "idna>=3.15" in runtime_dependencies
    assert "starlette>=1.0.1" in runtime_dependencies
    assert "pip-audit>=2.9.0" in dev_dependencies
    assert "urllib3>=2.7.0" in dev_dependencies


def test_dev_dependencies_keep_typed_starlette_test_client() -> None:
    """starlette>=1.2 TestClient imports httpx2 when installed; without it the
    deprecated httpx 1.x shim is used, which is untyped and fails pyright on
    fresh installs. Runtime code stays on httpx 1.x for the openai SDK."""
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    runtime_dependencies = pyproject["project"]["dependencies"]
    dev_dependencies = pyproject["project"]["optional-dependencies"]["dev"]

    assert "httpx2>=2.3.0" in dev_dependencies
    assert "httpx>=0.27.0" in runtime_dependencies
    assert not any(dependency.startswith("httpx2") for dependency in runtime_dependencies)


def test_gitignore_excludes_private_runtime_artifacts() -> None:
    gitignore = Path(".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in gitignore
    assert ".env.*" in gitignore
    assert ".librarian/" in gitignore
    assert "docs/results/" in gitignore
    assert "*.sqlite" in gitignore
    assert "*.sqlite-wal" in gitignore


def test_dockerignore_excludes_private_runtime_artifacts() -> None:
    dockerignore = Path(".dockerignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in dockerignore
    assert ".env.*" in dockerignore
    assert ".librarian" in dockerignore
    assert "docs/results" in dockerignore
    assert "*.sqlite" in dockerignore
    assert "*.sqlite-wal" in dockerignore


def test_sensitive_local_artifacts_are_not_tracked() -> None:
    git = which("git")
    if git is None:
        pytest.skip("git is not installed")
    completed = run([git, "ls-files"], capture_output=True, text=True)  # noqa: S603
    if completed.returncode != 0:
        pytest.skip("not running inside a git checkout")
    tracked = set(completed.stdout.splitlines())
    if not tracked:
        pytest.skip("no tracked files visible (not a git checkout)")
    forbidden_exact = {".env", ".librarian/librarian.sqlite"}
    forbidden_prefixes = (
        ".librarian/",
        "docs/results/",
    )
    forbidden_substrings = (
        "eval-provider",
        "benchmark-provider",
        "corpus-eval-provider",
    )

    assert forbidden_exact.isdisjoint(tracked)
    assert not any(path.startswith(forbidden_prefixes) for path in tracked)
    assert not any(substring in path for path in tracked for substring in forbidden_substrings)
