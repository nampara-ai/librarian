import hashlib
from pathlib import Path

import pytest

from librarian.application.ingest_document import IngestDocument
from librarian.domain.models import ExtractedAsset, ExtractionPayload
from librarian.storage.sqlite import SQLiteDatabase, SQLiteRepository


class AssetExtractor:
    supported_extensions = frozenset({".pdf"})

    def __init__(self) -> None:
        self.calls = 0

    async def extract(self, path: Path) -> str:
        return (await self.extract_with_assets(path)).text

    async def extract_with_assets(self, path: Path) -> ExtractionPayload:
        del path
        self.calls += 1
        image = b"image-data"
        return ExtractionPayload(
            text="Guide\n\n![](image_p1_0.png)",
            assets=(
                ExtractedAsset(
                    filename="image_p1_0.png",
                    media_type="image/png",
                    data=image,
                    sha256=hashlib.sha256(image).hexdigest(),
                ),
            ),
        )


@pytest.mark.asyncio
async def test_ingest_persists_assets_and_duplicate_skips_reextraction(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "librarian.sqlite")
    await database.initialize()
    repository = SQLiteRepository(database)
    extractor = AssetExtractor()
    ingest = IngestDocument(
        documents=repository,
        content=repository,
        extractor=extractor,
        assets=repository,
    )
    source = tmp_path / "guide.pdf"
    source.write_bytes(b"pdf-data")

    first = await ingest.execute(source)
    second = await ingest.execute(source)

    assert first.duplicate is False
    assert second.duplicate is True
    assert extractor.calls == 1
    assets = list(await repository.list_document_assets(first.document.id))
    assert [asset.filename for asset in assets] == ["image_p1_0.png"]
    assert assets[0].data == b"image-data"
