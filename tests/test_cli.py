import re
from pathlib import Path

from typer.testing import CliRunner

from librarian.cli.app import app


def test_cli_read_only_commands_do_not_require_llm_credentials(tmp_path: Path) -> None:
    runner = CliRunner()
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
        "LIBRARIAN_LLM_PROVIDER": "openai-compatible",
        "LIBRARIAN_LLM_API_KEY_ENV": "LIBRARIAN_TEST_MISSING_API_KEY",
    }

    runs = runner.invoke(app, ["runs"], env=env)
    queue = runner.invoke(app, ["queue"], env=env)
    search = runner.invoke(app, ["search", "horse"], env=env)
    show = runner.invoke(app, ["show", "doc_missing"], env=env)
    status = runner.invoke(app, ["status", "run_missing"], env=env)
    export = runner.invoke(app, ["export", "doc_missing"], env=env)

    assert runs.exit_code == 0
    assert queue.exit_code == 0
    assert search.exit_code == 0
    assert "Missing API key" not in runs.output + queue.output + search.output
    assert "Document not found" in show.output
    assert "Run not found" in status.output
    assert "Document not found" in export.output
    assert "Missing API key" not in show.output + status.output + export.output


def test_cli_rejects_unbounded_limits() -> None:
    runner = CliRunner()

    search = runner.invoke(app, ["search", "horse", "--limit=-1"])
    runs = runner.invoke(app, ["runs", "--limit=0"])
    queue = runner.invoke(app, ["queue", "--limit=10000"])

    assert search.exit_code != 0
    assert runs.exit_code != 0
    assert queue.exit_code != 0


def test_cli_convert_dir_rejects_new_directory_without_output_dir(tmp_path: Path) -> None:
    runner = CliRunner()
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "a.txt").write_text("Alpha", encoding="utf-8")

    result = runner.invoke(app, ["convert-dir", str(source_dir), "--output-mode", "new-directory"])

    assert result.exit_code != 0
    assert "--output-dir is required" in _strip_ansi(result.output)
    assert "Traceback" not in result.output


def test_cli_import_rejects_new_directory_without_output_dir(tmp_path: Path) -> None:
    runner = CliRunner()
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "a.txt").write_text("Alpha", encoding="utf-8")

    result = runner.invoke(app, ["import", str(source_dir), "--output-mode", "new-directory"])

    assert result.exit_code != 0
    assert "--output-dir is required" in _strip_ansi(result.output)
    assert "Traceback" not in result.output


def _strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", value)
