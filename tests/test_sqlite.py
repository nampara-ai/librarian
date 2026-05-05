from pathlib import Path

import pytest

from librarian.storage.sqlite import SQLiteDatabase


@pytest.mark.asyncio
async def test_sqlite_initializes_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "librarian.sqlite"
    database = SQLiteDatabase(database_path)

    await database.initialize()

    assert database_path.exists()
