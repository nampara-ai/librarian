"""SQLite adapter foundation."""

from __future__ import annotations

import asyncio
import sqlite3
from importlib.resources import files
from pathlib import Path


class SQLiteDatabase:
    """Small async-friendly SQLite wrapper for initialization."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        schema = files("librarian.storage").joinpath("schema.sql").read_text(encoding="utf-8")
        with sqlite3.connect(self.path) as connection:
            connection.executescript(schema)
