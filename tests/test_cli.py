import hashlib
import json
import re
import sqlite3
import warnings
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from librarian.application.factory import build_container
from librarian.cli.app import app
from librarian.config import Settings
from librarian.domain.models import RunStage, RunStatus
from librarian.storage.sqlite import SQLiteRunQueue


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
    search_details = runner.invoke(app, ["search", "horse", "--details"], env=env)
    show = runner.invoke(app, ["show", "doc_missing"], env=env)
    status = runner.invoke(app, ["status", "run_missing"], env=env)
    export = runner.invoke(app, ["export", "doc_missing"], env=env)

    assert runs.exit_code == 0
    assert queue.exit_code == 0
    assert search.exit_code == 0
    assert search_details.exit_code == 0
    assert "Missing API key" not in runs.output + queue.output + search.output
    assert "Missing API key" not in search_details.output
    assert "Document not found" in show.output
    assert "Run not found" in status.output
    assert "Document not found" in export.output
    assert "Missing API key" not in show.output + status.output + export.output


def test_cli_doctor_reports_optional_dependency_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_module(name: str) -> bool:
        return False

    def missing_tool(name: str) -> str | None:
        return None

    monkeypatch.setattr("librarian.cli.app._module_available", missing_module)
    monkeypatch.setattr("librarian.cli.app.shutil.which", missing_tool)
    runner = CliRunner()
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }

    result = runner.invoke(app, ["doctor"], env=env)

    output = _strip_ansi(result.output)
    assert result.exit_code == 0
    assert "pdfplumber" in output
    assert "tesseract" in output
    assert "missing" in output


def test_cli_doctor_strict_fails_when_optional_dependencies_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_module(name: str) -> bool:
        return False

    def missing_tool(name: str) -> str | None:
        return None

    monkeypatch.setattr("librarian.cli.app._module_available", missing_module)
    monkeypatch.setattr("librarian.cli.app.shutil.which", missing_tool)
    runner = CliRunner()
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }

    result = runner.invoke(app, ["doctor", "--strict"], env=env)

    assert result.exit_code == 1


def test_cli_rejects_unbounded_limits() -> None:
    runner = CliRunner()

    search = runner.invoke(app, ["search", "horse", "--limit=-1"])
    search_status = runner.invoke(app, ["search", "horse", "--document-status", "unknown"])
    search_scope = runner.invoke(app, ["search", "horse", "--scope", "all"])
    search_date = runner.invoke(app, ["search", "horse", "--created-after", "not-a-date"])
    runs = runner.invoke(app, ["runs", "--limit=0"])
    queue = runner.invoke(app, ["queue", "--limit=10000"])
    queue_offset = runner.invoke(app, ["queue", "--offset=-1"])
    status = runner.invoke(app, ["status", "run_missing", "--event-limit=0"])
    benchmark_paragraphs = runner.invoke(app, ["benchmark", "--paragraphs=0"])
    benchmark_chars = runner.invoke(app, ["benchmark", "--paragraph-chars=0"])
    benchmark_repeats = runner.invoke(app, ["benchmark", "--repeats=0"])

    assert search.exit_code != 0
    assert search_status.exit_code != 0
    assert search_scope.exit_code != 0
    assert search_date.exit_code != 0
    assert runs.exit_code != 0
    assert queue.exit_code != 0
    assert queue_offset.exit_code != 0
    assert status.exit_code != 0
    assert benchmark_paragraphs.exit_code != 0
    assert benchmark_chars.exit_code != 0
    assert benchmark_repeats.exit_code != 0


def test_cli_status_pages_run_events(tmp_path: Path) -> None:
    async def setup() -> str:
        settings = Settings(
            data_dir=tmp_path / ".librarian",
            database_path=tmp_path / ".librarian" / "librarian.sqlite",
        )
        container = await build_container(settings)
        source = tmp_path / "notes.txt"
        source.write_text("Horse transcript.", encoding="utf-8")
        ingested = await container.ingest_document.execute(source)
        run = await container.process_document.start(ingested.document.id)
        await container.repository.emit(run.id, RunStage.INGEST, "first event")
        await container.repository.emit(run.id, RunStage.CLEAN, "second event")
        return str(run.id)

    import asyncio

    run_id = asyncio.run(setup())
    runner = CliRunner()
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }

    result = runner.invoke(
        app,
        ["status", run_id, "--event-limit=1", "--event-offset=2"],
        env=env,
    )

    assert result.exit_code == 0
    output = _strip_ansi(result.output)
    assert "second event" in output
    assert "first event" not in output


def test_cli_search_details_reports_total_without_changing_id_output(tmp_path: Path) -> None:
    async def setup() -> None:
        settings = Settings(
            data_dir=tmp_path / ".librarian",
            database_path=tmp_path / ".librarian" / "librarian.sqlite",
        )
        container = await build_container(settings)
        first = tmp_path / "first.txt"
        second = tmp_path / "second.txt"
        first.write_text("Horse CLI details pagination fixture one.", encoding="utf-8")
        second.write_text(
            "CLI details pagination fixture two mentions Horse later.",
            encoding="utf-8",
        )
        await container.ingest_document.execute(first)
        await container.ingest_document.execute(second)

    import asyncio

    asyncio.run(setup())
    runner = CliRunner()
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }

    ids = runner.invoke(app, ["search", "Horse CLI", "--scope", "raw", "--limit", "1"], env=env)
    details = runner.invoke(
        app,
        ["search", "Horse CLI", "--scope", "raw", "--limit", "1", "--details"],
        env=env,
    )
    phrase = runner.invoke(
        app,
        ["search", "Horse CLI", "--scope", "raw", "--phrase", "--details"],
        env=env,
    )

    assert ids.exit_code == 0
    assert "Showing" not in ids.output
    assert details.exit_code == 0
    assert "Showing 1 of 2 results (offset=0, limit=1)" in _strip_ansi(details.output)
    assert phrase.exit_code == 0
    assert "Showing 1 of 1 results (offset=0, limit=20)" in _strip_ansi(phrase.output)


def test_cli_init_rejects_symlinked_config_path(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    data_dir = workspace / ".librarian"
    data_dir.mkdir(parents=True)
    outside = tmp_path / "outside-config.json"
    outside.write_text("keep", encoding="utf-8")
    (data_dir / "config.json").symlink_to(outside)

    result = runner.invoke(app, ["init", str(workspace)])

    assert result.exit_code != 0
    assert outside.read_text(encoding="utf-8") == "keep"


def test_cli_db_maintain_runs_sqlite_maintenance(tmp_path: Path) -> None:
    runner = CliRunner()
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }

    result = runner.invoke(app, ["db-maintain", "--vacuum"], env=env)

    assert result.exit_code == 0
    assert "SQLite maintenance complete" in result.output
    assert "vacuumed=True" in result.output


def test_cli_db_check_verifies_sqlite_database(tmp_path: Path) -> None:
    runner = CliRunner()
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }
    assert runner.invoke(app, ["migrate"], env=env).exit_code == 0

    result = runner.invoke(app, ["db-check"], env=env)

    assert result.exit_code == 0
    assert "SQLite verification complete" in result.output
    assert "integrity_ok=True" in result.output
    assert "foreign_key_violations=0" in result.output


def test_cli_db_stats_reports_machine_readable_storage_sizing(tmp_path: Path) -> None:
    runner = CliRunner()
    database_path = tmp_path / ".librarian" / "librarian.sqlite"
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(database_path),
    }
    assert runner.invoke(app, ["migrate"], env=env).exit_code == 0
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO documents (
              id, source_path, filename, media_type, byte_size, sha256,
              status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            (
                "doc_cli_stats",
                str(tmp_path / "stats.txt"),
                "stats.txt",
                "text/plain",
                17,
                "cli-stats-sha",
                "ingested",
            ),
        )
        connection.execute(
            "INSERT INTO content_blobs (key, text, created_at) VALUES (?, ?, datetime('now'))",
            ("raw:doc_cli_stats", "cli raw text"),
        )

    result = runner.invoke(app, ["db-stats", "--json"], env=env)

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["database_path"] == str(database_path)
    assert payload["database_file_bytes"] > 0
    assert payload["total_sqlite_bytes"] >= payload["database_file_bytes"]
    assert payload["table_counts"]["documents"] == 1
    assert payload["table_counts"]["content_blobs"] == 1
    assert payload["source_file_bytes"] == 17
    assert payload["stored_text_bytes"]["content_blobs"] == len("cli raw text")


def test_cli_api_audit_lists_redacted_security_events(tmp_path: Path) -> None:
    runner = CliRunner()
    database_path = tmp_path / ".librarian" / "librarian.sqlite"
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(database_path),
    }
    assert runner.invoke(app, ["migrate"], env=env).exit_code == 0
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO api_audit_events (
              event, method, path, client_host, credential_present,
              credential_scope, retry_after_seconds, created_at
            )
            VALUES
              (?, ?, ?, ?, ?, ?, ?, ?),
              (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "api_auth_failed",
                "GET",
                "/documents",
                "127.0.0.1",
                1,
                None,
                None,
                "2026-05-13T12:00:00+00:00",
                "api_rate_limited",
                "POST",
                "/search",
                "127.0.0.2",
                0,
                None,
                59,
                "2026-05-13T12:01:00+00:00",
            ),
        )

    result = runner.invoke(app, ["api-audit", "--json", "--event", "api_auth_failed"], env=env)

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["limit"] == 100
    assert payload["offset"] == 0
    assert payload["events"] == [
        {
            "id": 1,
            "event": "api_auth_failed",
            "method": "GET",
            "path": "/documents",
            "client_host": "127.0.0.1",
            "credential_present": True,
            "credential_scope": None,
            "retry_after_seconds": None,
            "created_at": "2026-05-13T12:00:00+00:00",
        }
    ]
    assert "secret" not in result.output.lower()


def test_cli_db_backup_creates_sqlite_backup(tmp_path: Path) -> None:
    runner = CliRunner()
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }
    backup_path = tmp_path / "backup.sqlite"
    migrate = runner.invoke(app, ["migrate"], env=env)
    assert migrate.exit_code == 0

    result = runner.invoke(app, ["db-backup", str(backup_path)], env=env)
    rejected = runner.invoke(app, ["db-backup", str(backup_path)], env=env)
    overwritten = runner.invoke(app, ["db-backup", str(backup_path), "--overwrite"], env=env)

    assert result.exit_code == 0
    assert "SQLite backup complete" in result.output
    assert backup_path.exists()
    assert rejected.exit_code != 0
    assert "already exists" in rejected.output
    assert overwritten.exit_code == 0


def test_cli_db_restore_requires_confirmation_and_restores_backup(tmp_path: Path) -> None:
    runner = CliRunner()
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }
    backup_path = tmp_path / "backup.sqlite"
    assert runner.invoke(app, ["migrate"], env=env).exit_code == 0
    assert runner.invoke(app, ["db-backup", str(backup_path)], env=env).exit_code == 0

    refused = runner.invoke(app, ["db-restore", str(backup_path)], env=env)
    restored = runner.invoke(app, ["db-restore", str(backup_path), "--yes"], env=env)

    assert refused.exit_code != 0
    assert "Refusing to restore without --yes" in refused.output
    assert restored.exit_code == 0
    assert "SQLite restore complete" in restored.output


def test_cli_workspace_backup_includes_database_and_uploads(tmp_path: Path) -> None:
    runner = CliRunner()
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }
    uploads = tmp_path / ".librarian" / "uploads" / "manual"
    uploads.mkdir(parents=True)
    (uploads / "source.txt").write_text("uploaded source", encoding="utf-8")
    backup_path = tmp_path / "workspace.zip"
    assert runner.invoke(app, ["migrate"], env=env).exit_code == 0

    result = runner.invoke(app, ["workspace-backup", str(backup_path)], env=env)
    rejected = runner.invoke(app, ["workspace-backup", str(backup_path)], env=env)
    overwritten = runner.invoke(app, ["workspace-backup", str(backup_path), "--overwrite"], env=env)

    assert result.exit_code == 0
    assert "Workspace backup complete" in result.output
    assert rejected.exit_code != 0
    assert "already exists" in rejected.output
    assert overwritten.exit_code == 0
    with zipfile.ZipFile(backup_path) as archive:
        names = set(archive.namelist())
        assert "workspace-backup.json" in names
        assert "data/librarian.sqlite" in names
        assert "data/uploads/manual/source.txt" in names
        assert "data/librarian.sqlite-wal" not in names


def test_cli_workspace_backup_skips_symlinked_files(tmp_path: Path) -> None:
    runner = CliRunner()
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }
    uploads = tmp_path / ".librarian" / "uploads" / "manual"
    uploads.mkdir(parents=True)
    (uploads / "source.txt").write_text("uploaded source", encoding="utf-8")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside secret", encoding="utf-8")
    (uploads / "linked-secret.txt").symlink_to(outside)
    backup_path = tmp_path / "workspace.zip"
    assert runner.invoke(app, ["migrate"], env=env).exit_code == 0

    result = runner.invoke(app, ["workspace-backup", str(backup_path)], env=env)

    assert result.exit_code == 0
    with zipfile.ZipFile(backup_path) as archive:
        names = set(archive.namelist())
        assert "data/uploads/manual/source.txt" in names
        assert "data/uploads/manual/linked-secret.txt" not in names
        assert "outside secret" not in {
            archive.read(name).decode("utf-8", errors="ignore") for name in names
        }


def test_cli_workspace_backup_rejects_symlink_parent(tmp_path: Path) -> None:
    runner = CliRunner()
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)
    assert runner.invoke(app, ["migrate"], env=env).exit_code == 0

    result = runner.invoke(app, ["workspace-backup", str(linked_parent / "workspace.zip")], env=env)

    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)
    assert "crosses symlinked parent" in str(result.exception)
    assert list(outside.iterdir()) == []


def test_cli_workspace_restore_requires_confirmation_and_restores_uploads(tmp_path: Path) -> None:
    runner = CliRunner()
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }
    uploads = tmp_path / ".librarian" / "uploads" / "manual"
    uploads.mkdir(parents=True)
    source_file = uploads / "source.txt"
    source_file.write_text("uploaded source", encoding="utf-8")
    backup_path = tmp_path / "workspace.zip"
    assert runner.invoke(app, ["migrate"], env=env).exit_code == 0
    assert runner.invoke(app, ["workspace-backup", str(backup_path)], env=env).exit_code == 0
    source_file.unlink()

    refused = runner.invoke(app, ["workspace-restore", str(backup_path)], env=env)
    restored = runner.invoke(app, ["workspace-restore", str(backup_path), "--yes"], env=env)

    assert refused.exit_code != 0
    assert "Refusing to restore workspace without --yes" in refused.output
    assert restored.exit_code == 0
    assert "Workspace restore complete" in restored.output
    assert source_file.read_text(encoding="utf-8") == "uploaded source"


def test_cli_workspace_restore_rejects_unsafe_archive_path(tmp_path: Path) -> None:
    runner = CliRunner()
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }
    archive_path = tmp_path / "unsafe.zip"
    assert runner.invoke(app, ["migrate"], env=env).exit_code == 0
    db_backup = tmp_path / "db.sqlite"
    assert runner.invoke(app, ["db-backup", str(db_backup)], env=env).exit_code == 0
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "workspace-backup.json",
            json.dumps(
                {
                    "artifact_type": "librarian-workspace-backup",
                    "database_archive_path": "data/librarian.sqlite",
                }
            ),
        )
        archive.write(db_backup, "data/librarian.sqlite")
        archive.writestr("../escape.txt", "bad")

    result = runner.invoke(app, ["workspace-restore", str(archive_path), "--yes"], env=env)

    assert result.exit_code != 0
    assert "unsafe path" in result.output


def test_cli_workspace_restore_rejects_symlink_archive_member(tmp_path: Path) -> None:
    runner = CliRunner()
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }
    archive_path = tmp_path / "symlink-member.zip"
    assert runner.invoke(app, ["migrate"], env=env).exit_code == 0
    db_backup = tmp_path / "db.sqlite"
    assert runner.invoke(app, ["db-backup", str(db_backup)], env=env).exit_code == 0
    link_info = zipfile.ZipInfo("data/uploads/link")
    link_info.create_system = 3
    link_info.external_attr = 0o120777 << 16
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "workspace-backup.json",
            json.dumps(
                {
                    "artifact_type": "librarian-workspace-backup",
                    "database_archive_path": "data/librarian.sqlite",
                }
            ),
        )
        archive.write(db_backup, "data/librarian.sqlite")
        archive.writestr(link_info, "../outside")

    result = runner.invoke(app, ["workspace-restore", str(archive_path), "--yes"], env=env)

    assert result.exit_code != 0
    assert "symlink member" in result.output


def test_cli_workspace_restore_does_not_apply_data_when_database_invalid(tmp_path: Path) -> None:
    runner = CliRunner()
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }
    archive_path = tmp_path / "invalid-db.zip"
    upload_path = tmp_path / ".librarian" / "uploads" / "manual" / "source.txt"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "workspace-backup.json",
            json.dumps(
                {
                    "artifact_type": "librarian-workspace-backup",
                    "database_archive_path": "data/librarian.sqlite",
                }
            ),
        )
        archive.writestr("data/librarian.sqlite", "not sqlite")
        archive.writestr("data/uploads/manual/source.txt", "should not restore")

    result = runner.invoke(app, ["workspace-restore", str(archive_path), "--yes"], env=env)

    assert result.exit_code != 0
    assert "failed integrity check" in result.output
    assert not upload_path.exists()


def test_cli_workspace_restore_rejects_existing_symlink_path(tmp_path: Path) -> None:
    runner = CliRunner()
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }
    uploads = tmp_path / ".librarian" / "uploads" / "manual"
    uploads.mkdir(parents=True)
    source_file = uploads / "source.txt"
    source_file.write_text("uploaded source", encoding="utf-8")
    backup_path = tmp_path / "workspace.zip"
    assert runner.invoke(app, ["migrate"], env=env).exit_code == 0
    assert runner.invoke(app, ["workspace-backup", str(backup_path)], env=env).exit_code == 0
    source_file.unlink()
    uploads.rmdir()
    (tmp_path / ".librarian" / "uploads").rmdir()
    (tmp_path / ".librarian" / "actual").mkdir()
    (tmp_path / ".librarian" / "uploads").symlink_to(tmp_path / ".librarian" / "actual")

    result = runner.invoke(app, ["workspace-restore", str(backup_path), "--yes"], env=env)

    assert result.exit_code != 0
    assert "crosses symlink" in result.output
    assert not (tmp_path / ".librarian" / "actual" / "manual" / "source.txt").exists()


def test_cli_workspace_restore_rejects_symlinked_data_dir(tmp_path: Path) -> None:
    runner = CliRunner()
    source_env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / "source" / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / "source" / ".librarian" / "librarian.sqlite"),
    }
    uploads = tmp_path / "source" / ".librarian" / "uploads" / "manual"
    uploads.mkdir(parents=True)
    (uploads / "source.txt").write_text("uploaded source", encoding="utf-8")
    backup_path = tmp_path / "workspace.zip"
    assert runner.invoke(app, ["migrate"], env=source_env).exit_code == 0
    assert runner.invoke(app, ["workspace-backup", str(backup_path)], env=source_env).exit_code == 0
    outside = tmp_path / "outside"
    outside.mkdir()
    data_dir = tmp_path / "restore" / ".librarian"
    data_dir.parent.mkdir()
    data_dir.symlink_to(outside, target_is_directory=True)
    restore_env = {
        "LIBRARIAN_DATA_DIR": str(data_dir),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / "restore.sqlite"),
    }

    result = runner.invoke(app, ["workspace-restore", str(backup_path), "--yes"], env=restore_env)

    assert result.exit_code != 0
    assert "data_dir crosses symlink" in result.output
    assert not (outside / "uploads" / "manual" / "source.txt").exists()


def test_cli_workspace_restore_rejects_archive_expansion_over_budget(tmp_path: Path) -> None:
    runner = CliRunner()
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }
    uploads = tmp_path / ".librarian" / "uploads" / "manual"
    uploads.mkdir(parents=True)
    (uploads / "source.txt").write_text("uploaded source", encoding="utf-8")
    backup_path = tmp_path / "workspace.zip"
    assert runner.invoke(app, ["migrate"], env=env).exit_code == 0
    assert runner.invoke(app, ["workspace-backup", str(backup_path)], env=env).exit_code == 0

    result = runner.invoke(
        app,
        [
            "workspace-restore",
            str(backup_path),
            "--yes",
            "--max-expanded-bytes",
            "1",
        ],
        env=env,
    )

    assert result.exit_code != 0
    assert "expands to more than 1 bytes" in result.output


def test_cli_workspace_restore_rejects_oversized_manifest(tmp_path: Path) -> None:
    runner = CliRunner()
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }
    archive_path = tmp_path / "oversized-manifest.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("workspace-backup.json", " " * (64 * 1024 + 1))
        archive.writestr("data/librarian.sqlite", "not reached")

    result = runner.invoke(app, ["workspace-restore", str(archive_path), "--yes"], env=env)

    assert result.exit_code != 0
    assert "manifest expands to more than 65536 bytes" in result.output


def test_cli_workspace_restore_rejects_duplicate_archive_path(tmp_path: Path) -> None:
    runner = CliRunner()
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }
    archive_path = tmp_path / "duplicate-path.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(
            "workspace-backup.json",
            json.dumps(
                {
                    "artifact_type": "librarian-workspace-backup",
                    "database_archive_path": "data/librarian.sqlite",
                }
            ),
        )
        archive.writestr("data/librarian.sqlite", "first")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            archive.writestr("data/librarian.sqlite", "second")

    result = runner.invoke(app, ["workspace-restore", str(archive_path), "--yes"], env=env)

    assert result.exit_code != 0
    assert "duplicate path: data/librarian.sqlite" in result.output


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


def test_cli_import_accepts_single_file(tmp_path: Path) -> None:
    runner = CliRunner()
    source = tmp_path / "large.md"
    source.write_text("# Transcript\n\nHorse import transcript", encoding="utf-8")
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }

    result = runner.invoke(app, ["import", str(source), "--format", "md"], env=env)

    assert result.exit_code == 0
    assert "ingested 1" in _strip_ansi(result.output)
    assert (tmp_path / "librarian-converted" / "large.md").exists()


def test_cli_convert_dir_rejects_symlink_output_dir(tmp_path: Path) -> None:
    runner = CliRunner()
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "a.txt").write_text("Alpha", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    output_dir = tmp_path / "linked-output"
    output_dir.symlink_to(outside, target_is_directory=True)

    result = runner.invoke(
        app,
        [
            "convert-dir",
            str(source_dir),
            "--output-mode",
            "new-directory",
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code != 0
    assert "crosses symlinked parent" in _strip_ansi(result.output)
    assert list(outside.iterdir()) == []


def test_cli_import_rejects_symlink_manifest_path(tmp_path: Path) -> None:
    runner = CliRunner()
    source = tmp_path / "large.md"
    source.write_text("# Transcript\n\nHorse import transcript", encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.symlink_to(outside)
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }

    result = runner.invoke(app, ["import", str(source), "--manifest", str(manifest)], env=env)

    assert result.exit_code != 0
    assert "manifest_path must not be a symlink" in _strip_ansi(result.output)
    assert outside.read_text(encoding="utf-8") == "{}"


def test_cli_import_rejects_symlink_report_path(tmp_path: Path) -> None:
    runner = CliRunner()
    source = tmp_path / "large.md"
    source.write_text("# Transcript\n\nHorse import transcript", encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text("keep", encoding="utf-8")
    report = tmp_path / "report.json"
    report.symlink_to(outside)
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }

    result = runner.invoke(app, ["import", str(source), "--report", str(report)], env=env)

    assert result.exit_code != 0
    assert "must not be a symlink" in _strip_ansi(result.output)
    assert outside.read_text(encoding="utf-8") == "keep"


def test_cli_import_single_file_processes_immediately(tmp_path: Path) -> None:
    runner = CliRunner()
    source = tmp_path / "large.md"
    source.write_text("# Transcript\n\nHorse import process transcript", encoding="utf-8")
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
        "LIBRARIAN_CHUNK_TARGET_CHARS": "200",
        "LIBRARIAN_CHUNK_OVERLAP_CHARS": "20",
    }

    result = runner.invoke(app, ["import", str(source), "--format", "md", "--process"], env=env)
    search = runner.invoke(app, ["search", "Horse import process", "--details"], env=env)

    assert result.exit_code == 0
    assert "processed 1" in _strip_ansi(result.output)
    assert search.exit_code == 0
    assert "Showing 1 of 1 results" in _strip_ansi(search.output)


def test_cli_corpus_eval_writes_results(tmp_path: Path) -> None:
    runner = CliRunner()
    source = tmp_path / "large.md"
    source.write_text(
        "# Transcript\n\nHorse import transcript about saddle fit.",
        encoding="utf-8",
    )
    suite = tmp_path / "corpus.json"
    suite.write_text(
        """
        {
          "cases": [
            {
              "name": "single file",
              "source_path": "large.md",
              "process": false,
              "expected_contains": ["saddle fit"],
              "require_markdown_headings": true
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    output = tmp_path / "results.json"
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }

    result = runner.invoke(
        app,
        [
            "corpus-eval",
            str(suite),
            "--output-dir",
            str(tmp_path / "converted"),
            "--output",
            str(output),
        ],
        env=env,
    )

    assert result.exit_code == 0
    assert "Wrote corpus eval results" in result.output
    assert '"passed": true' in output.read_text(encoding="utf-8")


def test_cli_benchmark_rejects_symlink_output(tmp_path: Path) -> None:
    runner = CliRunner()
    outside = tmp_path / "outside.json"
    outside.write_text("keep", encoding="utf-8")
    output = tmp_path / "benchmark.json"
    output.symlink_to(outside)
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }

    result = runner.invoke(
        app,
        [
            "benchmark",
            "--paragraphs",
            "1",
            "--paragraph-chars",
            "20",
            "--output",
            str(output),
        ],
        env=env,
    )

    assert result.exit_code != 0
    assert outside.read_text(encoding="utf-8") == "keep"


def test_cli_output_writer_rejects_symlink_parent(tmp_path: Path) -> None:
    runner = CliRunner()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }

    result = runner.invoke(
        app,
        [
            "benchmark",
            "--paragraphs",
            "1",
            "--paragraph-chars",
            "20",
            "--output",
            str(linked_parent / "benchmark.json"),
        ],
        env=env,
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, ValueError)
    assert "crosses symlinked parent" in str(result.exception)
    assert list(outside.iterdir()) == []


def test_cli_export_rejects_symlink_output(tmp_path: Path) -> None:
    async def setup() -> str:
        settings = Settings(
            data_dir=tmp_path / ".librarian",
            database_path=tmp_path / ".librarian" / "librarian.sqlite",
            chunk_target_chars=200,
            chunk_overlap_chars=20,
        )
        container = await build_container(settings)
        source = tmp_path / "notes.txt"
        source.write_text("Horse transcript about saddle fit.", encoding="utf-8")
        ingested = await container.ingest_document.execute(source)
        await container.process_document.execute(ingested.document.id)
        return str(ingested.document.id)

    import asyncio

    document_id = asyncio.run(setup())
    runner = CliRunner()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    output = tmp_path / "export.txt"
    output.symlink_to(outside)
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }

    result = runner.invoke(app, ["export", document_id, "--output", str(output)], env=env)

    assert result.exit_code != 0
    assert outside.read_text(encoding="utf-8") == "keep"


def test_cli_eval_rejects_symlink_output(tmp_path: Path) -> None:
    runner = CliRunner()
    suite = tmp_path / "eval.json"
    suite.write_text(
        """
        {
          "cases": [
            {
              "name": "sample",
              "input_text": "Horse transcript about groundwork.",
              "expected_contains": ["groundwork"],
              "expected_classification_prefix": "636"
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    outside = tmp_path / "outside.json"
    outside.write_text("keep", encoding="utf-8")
    output = tmp_path / "eval-results.json"
    output.symlink_to(outside)
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }

    result = runner.invoke(app, ["eval", str(suite), "--output", str(output)], env=env)

    assert result.exit_code != 0
    assert outside.read_text(encoding="utf-8") == "keep"


def test_cli_corpus_eval_rejects_symlink_output(tmp_path: Path) -> None:
    runner = CliRunner()
    source = tmp_path / "large.md"
    source.write_text("# Transcript\n\nHorse import transcript about saddle fit.", encoding="utf-8")
    suite = tmp_path / "corpus.json"
    suite.write_text(
        """
        {
          "cases": [
            {
              "name": "single file",
              "source_path": "large.md",
              "process": false,
              "expected_contains": ["saddle fit"],
              "require_markdown_headings": true
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    outside = tmp_path / "outside.json"
    outside.write_text("keep", encoding="utf-8")
    output = tmp_path / "corpus-results.json"
    output.symlink_to(outside)
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }

    result = runner.invoke(
        app,
        [
            "corpus-eval",
            str(suite),
            "--output-dir",
            str(tmp_path / "converted"),
            "--output",
            str(output),
        ],
        env=env,
    )

    assert result.exit_code != 0
    assert outside.read_text(encoding="utf-8") == "keep"


def test_cli_generate_corpus_writes_suite_and_files(tmp_path: Path) -> None:
    runner = CliRunner()
    output_dir = tmp_path / "synthetic"

    result = runner.invoke(
        app,
        [
            "generate-corpus",
            "--output-dir",
            str(output_dir),
            "--documents",
            "2",
            "--paragraphs",
            "3",
            "--paragraph-sentences",
            "2",
        ],
    )

    assert result.exit_code == 0
    assert "Generated 2 synthetic document(s)" in result.output
    suite_path = output_dir / "corpus_eval_cases.json"
    payload = json.loads(suite_path.read_text(encoding="utf-8"))
    assert len(payload["cases"]) == 2
    first_file = output_dir / payload["cases"][0]["source_path"]
    assert first_file.exists()
    text = first_file.read_text(encoding="utf-8")
    assert "canter transitions" in text
    assert "saddle fit" in text

    repeated = runner.invoke(app, ["generate-corpus", "--output-dir", str(output_dir)])

    assert repeated.exit_code != 0
    assert "Corpus eval suite already exists" in _strip_ansi(repeated.output)


def test_cli_generate_corpus_can_include_docx_fixtures(tmp_path: Path) -> None:
    runner = CliRunner()
    output_dir = tmp_path / "synthetic"

    result = runner.invoke(
        app,
        [
            "generate-corpus",
            "--output-dir",
            str(output_dir),
            "--documents",
            "1",
            "--paragraphs",
            "3",
            "--paragraph-sentences",
            "1",
            "--include-docx",
        ],
    )

    assert result.exit_code == 0
    assert "Generated 4 synthetic document(s)" in result.output
    payload = json.loads((output_dir / "corpus_eval_cases.json").read_text(encoding="utf-8"))
    docx_cases = [case for case in payload["cases"] if "docx" in case["tags"]]
    assert len(docx_cases) == 3
    first_docx = output_dir / docx_cases[0]["source_path"]
    assert first_docx.exists()
    assert first_docx.suffix == ".docx"
    assert "Table checkpoint" in docx_cases[0]["expected_contains"]
    assert "Synthetic header" in docx_cases[0]["expected_contains"]
    assert "Synthetic footer" in docx_cases[0]["expected_contains"]


def test_cli_generate_corpus_can_include_embedded_pdf_fixtures(tmp_path: Path) -> None:
    runner = CliRunner()
    output_dir = tmp_path / "synthetic"

    result = runner.invoke(
        app,
        [
            "generate-corpus",
            "--output-dir",
            str(output_dir),
            "--documents",
            "1",
            "--paragraphs",
            "40",
            "--paragraph-sentences",
            "1",
            "--include-pdf",
        ],
    )

    assert result.exit_code == 0
    assert "Generated 4 synthetic document(s)" in result.output
    payload = json.loads((output_dir / "corpus_eval_cases.json").read_text(encoding="utf-8"))
    pdf_cases = [case for case in payload["cases"] if "pdf" in case["tags"]]
    assert len(pdf_cases) == 3
    first_pdf = output_dir / pdf_cases[0]["source_path"]
    assert first_pdf.exists()
    assert first_pdf.suffix == ".pdf"
    assert first_pdf.read_bytes().startswith(b"%PDF-1.4")
    assert pdf_cases[0]["expected_page_count"] >= 2
    assert "embedded-text" in pdf_cases[0]["tags"]
    pdfplumber = pytest.importorskip("pdfplumber")
    with pdfplumber.open(first_pdf) as pdf:
        extracted = "\n".join(page.extract_text() or "" for page in pdf.pages)
    assert "canter transitions" in extracted
    assert "saddle fit" in extracted


def test_cli_generate_corpus_can_include_scanned_pdf_fixtures(tmp_path: Path) -> None:
    runner = CliRunner()
    output_dir = tmp_path / "synthetic"

    result = runner.invoke(
        app,
        [
            "generate-corpus",
            "--output-dir",
            str(output_dir),
            "--documents",
            "1",
            "--paragraphs",
            "8",
            "--paragraph-sentences",
            "1",
            "--include-scanned-pdf",
        ],
    )

    assert result.exit_code == 0
    assert "Generated 3 synthetic document(s)" in result.output
    payload = json.loads((output_dir / "corpus_eval_cases.json").read_text(encoding="utf-8"))
    scanned_cases = [case for case in payload["cases"] if "scanned" in case["tags"]]
    assert len(scanned_cases) == 2
    assert any("mixed-embedded-scanned" in case["tags"] for case in scanned_cases)
    first_pdf = output_dir / scanned_cases[0]["source_path"]
    assert first_pdf.exists()
    assert first_pdf.suffix == ".pdf"
    assert "ocr" in scanned_cases[0]["tags"]
    assert scanned_cases[0]["expected_page_count"] >= 1
    pdfplumber = pytest.importorskip("pdfplumber")
    with pdfplumber.open(first_pdf) as pdf:
        extracted = "\n".join(page.extract_text() or "" for page in pdf.pages)
    assert "canter transitions" not in extracted


def test_cli_generate_corpus_can_include_noisy_ocr_pdf_fixture(tmp_path: Path) -> None:
    runner = CliRunner()
    output_dir = tmp_path / "synthetic"

    result = runner.invoke(
        app,
        [
            "generate-corpus",
            "--output-dir",
            str(output_dir),
            "--documents",
            "1",
            "--paragraphs",
            "2",
            "--paragraph-sentences",
            "1",
            "--include-noisy-ocr-pdf",
        ],
    )

    assert result.exit_code == 0
    assert "Generated 2 synthetic document(s)" in result.output
    payload = json.loads((output_dir / "corpus_eval_cases.json").read_text(encoding="utf-8"))
    noisy_cases = [case for case in payload["cases"] if "noisy-ocr" in case["tags"]]
    assert len(noisy_cases) == 1
    noisy_pdf = output_dir / noisy_cases[0]["source_path"]
    assert noisy_pdf.exists()
    assert noisy_pdf.suffix == ".pdf"
    assert noisy_cases[0]["expected_page_count"] == 1
    assert "canter transitions" in noisy_cases[0]["expected_contains"]
    pdfplumber = pytest.importorskip("pdfplumber")
    with pdfplumber.open(noisy_pdf) as pdf:
        extracted = "\n".join(page.extract_text() or "" for page in pdf.pages)
    assert "canter transitions" not in extracted


def test_cli_generate_corpus_rejects_symlink_suite_path(tmp_path: Path) -> None:
    runner = CliRunner()
    output_dir = tmp_path / "synthetic"
    output_dir.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("keep", encoding="utf-8")
    (output_dir / "corpus_eval_cases.json").symlink_to(outside)

    result = runner.invoke(
        app,
        [
            "generate-corpus",
            "--output-dir",
            str(output_dir),
            "--overwrite",
        ],
    )

    assert result.exit_code != 0
    assert outside.read_text(encoding="utf-8") == "keep"


def test_cli_generate_corpus_rejects_symlink_output_dir(tmp_path: Path) -> None:
    runner = CliRunner()
    outside = tmp_path / "outside"
    outside.mkdir()
    output_dir = tmp_path / "synthetic"
    output_dir.symlink_to(outside, target_is_directory=True)

    result = runner.invoke(
        app,
        [
            "generate-corpus",
            "--output-dir",
            str(output_dir),
            "--overwrite",
        ],
    )

    assert result.exit_code != 0
    assert "crosses symlink" in _strip_ansi(result.output)
    assert list(outside.iterdir()) == []


def test_cli_generate_corpus_rejects_symlink_output_parent(tmp_path: Path) -> None:
    runner = CliRunner()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)

    result = runner.invoke(
        app,
        [
            "generate-corpus",
            "--output-dir",
            str(linked_parent / "synthetic"),
            "--overwrite",
        ],
    )

    assert result.exit_code != 0
    assert "crosses symlink" in _strip_ansi(result.output)
    assert list(outside.iterdir()) == []


def test_cli_page_manifest_summarizes_pdf_page_state(tmp_path: Path) -> None:
    manifest = tmp_path / "fixture.md.pages.json"
    manifest.write_text(
        json.dumps(
            {
                "artifact_type": "pdf-page-extraction-manifest",
                "source_sha256": "abc123",
                "page_count": 3,
                "pages": [
                    {
                        "page_number": 1,
                        "source": "embedded",
                        "status": "succeeded",
                        "chars": 25,
                        "confidence": None,
                        "corrected": False,
                        "attempts": 0,
                        "duration_ms": None,
                        "error": None,
                    },
                    {
                        "page_number": 2,
                        "source": "ocr",
                        "status": "succeeded",
                        "chars": 40,
                        "confidence": 91.5,
                        "corrected": True,
                        "attempts": 1,
                        "duration_ms": 123.4,
                        "error": None,
                    },
                    {
                        "page_number": 3,
                        "source": "ocr",
                        "status": "failed",
                        "chars": 0,
                        "confidence": None,
                        "corrected": False,
                        "attempts": 2,
                        "duration_ms": 45.6,
                        "warnings": ["ocr-page-failed"],
                        "error": "scan failed",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    runner = CliRunner()

    result = runner.invoke(app, ["page-manifest", str(manifest), "--failures-only"])

    output = _strip_ansi(result.output)
    assert result.exit_code == 0
    assert "Source SHA-256: abc123" in output
    assert "Pages: 3 (succeeded=2, failed=1, pending=0)" in output
    assert "embedded=1" in output
    assert "ocr=2" in output
    assert "corrected=1" in output
    assert "attempts=3" in output
    assert "avg_confidence=91.5" in output
    assert "ocr-page-failed" in output
    assert "45.6" in output
    assert "scan failed" in output


def test_cli_page_manifest_prints_machine_readable_summary(tmp_path: Path) -> None:
    manifest = tmp_path / "fixture.md.pages.json"
    manifest.write_text(
        json.dumps(
            {
                "artifact_type": "pdf-page-extraction-manifest",
                "source_sha256": "abc123",
                "page_count": 2,
                "pages": [
                    {
                        "page_number": 1,
                        "source": "ocr",
                        "status": "succeeded",
                        "chars": 40,
                        "confidence": 91.5,
                        "corrected": True,
                        "attempts": 1,
                        "duration_ms": 123.4,
                        "raw_text": "raw page text",
                        "text": "corrected page text",
                        "corrected_text": "corrected page text",
                        "error": None,
                    },
                    {
                        "page_number": 2,
                        "source": "ocr",
                        "status": "failed",
                        "chars": 0,
                        "confidence": None,
                        "corrected": False,
                        "attempts": 2,
                        "duration_ms": 45.6,
                        "warnings": ["ocr-page-failed", "missing-ocr-confidence"],
                        "raw_text": "",
                        "text": "",
                        "error": "scan failed",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    runner = CliRunner()

    result = runner.invoke(app, ["page-manifest", str(manifest), "--json", "--failures-only"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["source_sha256"] == "abc123"
    assert payload["page_count"] == 2
    assert payload["statuses"] == {"failed": 1, "succeeded": 1}
    assert payload["sources"] == {"ocr": 2}
    assert payload["warnings"] == {
        "missing-ocr-confidence": 1,
        "ocr-page-failed": 1,
    }
    assert payload["corrected_pages"] == 1
    assert payload["attempts"] == 3
    assert payload["average_confidence"] == 91.5
    assert payload["failures_only"] is True
    assert payload["pages"] == [
        {
            "page_number": 2,
            "source": "ocr",
            "status": "failed",
            "chars": 0,
            "confidence": None,
            "corrected": False,
            "attempts": 2,
            "duration_ms": 45.6,
            "warnings": ["ocr-page-failed", "missing-ocr-confidence"],
            "error": "scan failed",
        }
    ]
    assert "raw page text" not in result.output
    assert "corrected page text" not in result.output


def test_cli_page_manifest_rejects_unexpected_artifact(tmp_path: Path) -> None:
    manifest = tmp_path / "not-pages.json"
    manifest.write_text(
        json.dumps({"artifact_type": "conversion-sidecar", "pages": []}),
        encoding="utf-8",
    )
    runner = CliRunner()

    result = runner.invoke(app, ["page-manifest", str(manifest)])

    assert result.exit_code != 0
    assert "unexpected artifact_type" in _strip_ansi(result.output)


def test_cli_page_manifest_rejects_symlink_path(tmp_path: Path) -> None:
    manifest = tmp_path / "fixture.md.pages.json"
    manifest.write_text(
        json.dumps({"artifact_type": "pdf-page-extraction-manifest", "pages": []}),
        encoding="utf-8",
    )
    link = tmp_path / "manifest-link.json"
    link.symlink_to(manifest)
    runner = CliRunner()

    result = runner.invoke(app, ["page-manifest", str(link)])

    assert result.exit_code != 0
    assert "must not be a symlink" in _strip_ansi(result.output)


def test_cli_page_manifest_rejects_symlink_parent(tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    manifest = real_dir / "fixture.md.pages.json"
    manifest.write_text(
        json.dumps({"artifact_type": "pdf-page-extraction-manifest", "pages": []}),
        encoding="utf-8",
    )
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(real_dir, target_is_directory=True)
    runner = CliRunner()

    result = runner.invoke(app, ["page-manifest", str(linked_dir / manifest.name)])

    assert result.exit_code != 0
    assert "crosses symlinked parent" in _strip_ansi(result.output)


def test_cli_api_public_bind_accepts_rotated_api_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    public_host = "0.0.0.0"  # noqa: S104 - test fixture for public bind validation.

    def fake_run(*args: object, **kwargs: object) -> None:
        del args
        calls.append(kwargs)

    monkeypatch.setattr("librarian.cli.app.uvicorn.run", fake_run)
    runner = CliRunner()
    env = {
        "LIBRARIAN_API_KEYS": "new-secret,break-glass",
        "LIBRARIAN_API_IMPORT_ROOT": str(tmp_path),
    }

    result = runner.invoke(app, ["api", "--host", public_host, "--port", "9000"], env=env)

    assert result.exit_code == 0
    assert calls == [
        {
            "factory": True,
            "host": public_host,
            "port": 9000,
        }
    ]


def test_cli_api_public_bind_accepts_hashed_api_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    public_host = "0.0.0.0"  # noqa: S104 - test fixture for public bind validation.

    def fake_run(*args: object, **kwargs: object) -> None:
        del args
        calls.append(kwargs)

    monkeypatch.setattr("librarian.cli.app.uvicorn.run", fake_run)
    runner = CliRunner()
    env = {
        "LIBRARIAN_API_KEY_HASHES": f"write:{hashlib.sha256(b'public').hexdigest()}",
        "LIBRARIAN_API_IMPORT_ROOT": str(tmp_path),
    }

    result = runner.invoke(app, ["api", "--host", public_host], env=env)

    assert result.exit_code == 0
    assert calls and calls[0]["host"] == public_host


def test_cli_retry_queue_failure_marks_retry_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def setup() -> str:
        settings = Settings(
            data_dir=tmp_path / ".librarian",
            database_path=tmp_path / ".librarian" / "librarian.sqlite",
        )
        container = await build_container(settings)
        source = tmp_path / "notes.txt"
        source.write_text("Horse transcript.", encoding="utf-8")
        ingested = await container.ingest_document.execute(source)
        run = await container.process_document.start(ingested.document.id)
        await container.repository.update_status(
            run.id,
            status=RunStatus.FAILED,
            stage=RunStage.COMPLETE,
            error="original failure",
        )
        return str(run.id)

    import asyncio

    run_id = asyncio.run(setup())

    async def fail_enqueue(self: object, retry_id: object) -> None:
        del self, retry_id
        raise RuntimeError("queue down")

    monkeypatch.setattr(SQLiteRunQueue, "enqueue", fail_enqueue)
    runner = CliRunner()
    env = {
        "LIBRARIAN_DATA_DIR": str(tmp_path / ".librarian"),
        "LIBRARIAN_DATABASE_PATH": str(tmp_path / ".librarian" / "librarian.sqlite"),
    }

    result = runner.invoke(app, ["run-retry", run_id, "--queue"], env=env)

    assert result.exit_code != 0
    assert "Failed to enqueue retry" in _strip_ansi(result.output)

    async def inspect() -> list[str | None]:
        settings = Settings(
            data_dir=tmp_path / ".librarian",
            database_path=tmp_path / ".librarian" / "librarian.sqlite",
        )
        container = await build_container(settings)
        return [run.error for run in await container.repository.list_runs(limit=10)]

    errors = asyncio.run(inspect())
    assert "submission failed: queue down" in errors


def _strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", value)
