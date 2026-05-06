import re
from pathlib import Path

from typer.testing import CliRunner

from librarian.cli.app import app


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
