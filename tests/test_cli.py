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
