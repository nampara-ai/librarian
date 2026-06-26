"""Tests for the content-hash extraction cache and the extraction timeout."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from librarian.ingest.extractors import (
    CachingExtractor,
    CompositeExtractor,
    ExtractionCacheEntry,
    ExtractionTimeoutError,
    extraction_config_signature,
)
from librarian.storage.sqlite import SQLiteDatabase, SQLiteRepository


class _InMemoryCache:
    """Minimal ExtractionCacheStore for unit tests."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], str] = {}

    async def get_extraction(self, content_sha256: str, config_signature: str) -> str | None:
        return self.entries.get((content_sha256, config_signature))

    async def put_extraction(self, entry: ExtractionCacheEntry) -> None:
        self.entries[(entry.content_sha256, entry.config_signature)] = entry.text


class _CountingExtractor:
    """Inner extractor that records how many times it ran."""

    supported_extensions = frozenset({".txt"})

    def __init__(self, text: str = "extracted") -> None:
        self.text = text
        self.calls = 0
        self.last_metadata: dict[str, object] | None = {"engine": "counting"}

    async def extract(self, path: Path) -> str:
        del path
        self.calls += 1
        return self.text


def _write(tmp_path: Path, name: str, payload: bytes) -> Path:
    source = tmp_path / name
    source.write_bytes(payload)
    return source


def test_caching_extractor_skips_inner_on_hit(tmp_path: Path) -> None:
    source = _write(tmp_path, "doc.txt", b"hello world")
    inner = _CountingExtractor()
    cache = _InMemoryCache()
    extractor = CachingExtractor(inner, cache, config_signature="sig-1")

    first = asyncio.run(extractor.extract(source))
    assert first == "extracted"
    assert inner.calls == 1
    assert extractor.last_cache_hit is False

    second = asyncio.run(extractor.extract(source))
    assert second == "extracted"
    assert inner.calls == 1  # served from cache
    assert extractor.last_cache_hit is True
    assert extractor.last_metadata is not None
    assert extractor.last_metadata["engine"] == "cache"


def test_caching_extractor_separates_by_config_signature(tmp_path: Path) -> None:
    source = _write(tmp_path, "doc.txt", b"hello world")
    cache = _InMemoryCache()
    inner_a = _CountingExtractor(text="engine-a")
    inner_b = _CountingExtractor(text="engine-b")

    a = CachingExtractor(inner_a, cache, config_signature="sig-a")
    b = CachingExtractor(inner_b, cache, config_signature="sig-b")

    assert asyncio.run(a.extract(source)) == "engine-a"
    # A different signature must miss and re-extract, not serve A's text.
    assert asyncio.run(b.extract(source)) == "engine-b"
    assert inner_a.calls == 1
    assert inner_b.calls == 1


def test_caching_extractor_does_not_cache_failures(tmp_path: Path) -> None:
    source = _write(tmp_path, "doc.txt", b"hello world")
    cache = _InMemoryCache()

    class _FlakyExtractor:
        supported_extensions = frozenset({".txt"})

        def __init__(self) -> None:
            self.calls = 0
            self.last_metadata: dict[str, object] | None = None

        async def extract(self, path: Path) -> str:
            del path
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient failure")
            return "recovered"

    inner = _FlakyExtractor()
    extractor = CachingExtractor(inner, cache, config_signature="sig-1")

    with pytest.raises(RuntimeError, match="transient failure"):
        asyncio.run(extractor.extract(source))
    assert cache.entries == {}  # negative result not stored

    # The retry must re-run the extractor and then succeed.
    assert asyncio.run(extractor.extract(source)) == "recovered"
    assert inner.calls == 2


def test_caching_extractor_bypasses_cache_when_manifest_active(tmp_path: Path) -> None:
    source = _write(tmp_path, "doc.txt", b"hello world")
    inner = _CountingExtractor()
    cache = _InMemoryCache()
    extractor = CachingExtractor(inner, cache, config_signature="sig-1")
    extractor.set_page_manifest_path(tmp_path / "manifest.json")

    asyncio.run(extractor.extract(source))
    asyncio.run(extractor.extract(source))

    assert inner.calls == 2  # cache bypassed while a manifest is requested
    assert cache.entries == {}


def test_extraction_config_signature_is_stable_and_sensitive() -> None:
    base = {"pdf_engine": "auto", "ocr_language": "eng"}
    assert extraction_config_signature(base) == extraction_config_signature(dict(base))
    assert extraction_config_signature(base) != extraction_config_signature(
        {"pdf_engine": "legacy", "ocr_language": "eng"}
    )


def test_composite_config_signature_tracks_engine_change() -> None:
    auto = CompositeExtractor(pdf_engine="auto")
    legacy = CompositeExtractor(pdf_engine="legacy")
    assert auto.config_signature != legacy.config_signature


def test_composite_config_signature_tracks_output_affecting_ocr_options() -> None:
    base = CompositeExtractor(pdf_engine="legacy")
    fail_changed = CompositeExtractor(pdf_engine="legacy", ocr_fail_on_page_error=False)
    resp_changed = CompositeExtractor(
        pdf_engine="legacy", ocr_max_correction_response_chars=4096
    )
    assert base.config_signature != fail_changed.config_signature
    assert base.config_signature != resp_changed.config_signature


def test_extraction_timeout_raises(tmp_path: Path) -> None:
    source = _write(tmp_path, "doc.txt", b"hello world")
    composite = CompositeExtractor(pdf_engine="legacy", extraction_timeout_seconds=1)

    class _SlowExtractor:
        supported_extensions = frozenset({".txt"})
        last_metadata: dict[str, object] | None = None

        async def extract(self, path: Path) -> str:
            del path
            await asyncio.sleep(5)
            return "never"

    composite._extractors[".txt"] = _SlowExtractor()  # type: ignore[assignment]

    with pytest.raises(ExtractionTimeoutError):
        asyncio.run(composite.extract(source))


def test_extraction_timeout_disabled_passes_through(tmp_path: Path) -> None:
    source = _write(tmp_path, "doc.txt", b"hello world")
    composite = CompositeExtractor(pdf_engine="legacy", extraction_timeout_seconds=0)

    class _FastExtractor:
        supported_extensions = frozenset({".txt"})
        last_metadata: dict[str, object] | None = None

        async def extract(self, path: Path) -> str:
            del path
            return "fast"

    composite._extractors[".txt"] = _FastExtractor()  # type: ignore[assignment]

    assert asyncio.run(composite.extract(source)) == "fast"


@pytest.mark.asyncio
async def test_sqlite_extraction_cache_round_trip(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "librarian.sqlite")
    await database.initialize()
    repository = SQLiteRepository(database)

    assert await repository.get_extraction("sha-1", "sig-1") is None

    await repository.put_extraction(
        ExtractionCacheEntry(
            content_sha256="sha-1",
            config_signature="sig-1",
            source_extension=".pdf",
            text="# Cached Markdown",
        )
    )
    assert await repository.get_extraction("sha-1", "sig-1") == "# Cached Markdown"
    # A different signature is a distinct cache slot.
    assert await repository.get_extraction("sha-1", "sig-2") is None

    # Upsert overwrites the stored text for the same key.
    await repository.put_extraction(
        ExtractionCacheEntry(
            content_sha256="sha-1",
            config_signature="sig-1",
            source_extension=".pdf",
            text="# Updated",
        )
    )
    assert await repository.get_extraction("sha-1", "sig-1") == "# Updated"


@pytest.mark.asyncio
async def test_sqlite_stats_report_extraction_cache(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "librarian.sqlite")
    await database.initialize()
    repository = SQLiteRepository(database)
    await repository.put_extraction(
        ExtractionCacheEntry(
            content_sha256="sha-1",
            config_signature="sig-1",
            source_extension=".pdf",
            text="# Cached",
        )
    )

    stats = await database.stats()
    assert stats.table_counts["extraction_cache"] == 1


@pytest.mark.asyncio
async def test_caching_extractor_with_sqlite_repository(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "librarian.sqlite")
    await database.initialize()
    repository = SQLiteRepository(database)
    source = _write(tmp_path, "doc.txt", b"hello world")
    inner = _CountingExtractor(text="from-engine")
    extractor = CachingExtractor(inner, repository, config_signature="sig-1")

    assert await extractor.extract(source) == "from-engine"
    assert await extractor.extract(source) == "from-engine"
    assert inner.calls == 1  # second call served by the SQLite cache
