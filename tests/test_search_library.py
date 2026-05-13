from datetime import UTC, datetime

import pytest

from librarian.application.search_library import SearchLibrary
from librarian.domain.ids import DocumentId, RunId
from librarian.domain.models import (
    Classification,
    CleanedOutput,
    DocumentStatus,
    SearchFacets,
    SearchFacetValue,
    SearchResult,
)


class FakeSearchIndex:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def index(
        self,
        output: CleanedOutput,
        classification: Classification | None,
    ) -> None:
        self.calls.append(
            (
                "index",
                output.text,
                {"classification_code": classification.code if classification else None},
            )
        )

    async def search(self, query: str, **kwargs: object) -> list[DocumentId]:
        self.calls.append(("search", query, kwargs))
        return [DocumentId("doc_search")]

    async def search_results(self, query: str, **kwargs: object) -> list[SearchResult]:
        self.calls.append(("search_results", query, kwargs))
        return [
            SearchResult(
                document_id=DocumentId("doc_search"),
                run_id=RunId("run_search"),
                source="cleaned",
                filename="notes.md",
                document_status=DocumentStatus.READY,
                snippet="<mark>horse</mark>",
                score=-0.25,
                classification_code="636.1",
                classification_label="Animal husbandry",
            )
        ]

    async def search_count(self, query: str, **kwargs: object) -> int:
        self.calls.append(("search_count", query, kwargs))
        return 1

    async def search_facets(self, query: str, **kwargs: object) -> SearchFacets:
        self.calls.append(("search_facets", query, kwargs))
        return SearchFacets(
            classifications=(
                SearchFacetValue("636.1", count=1, label="Animal husbandry"),
            ),
            statuses=(SearchFacetValue("ready", count=1),),
            sources=(SearchFacetValue("cleaned", count=1),),
            filenames=(SearchFacetValue("notes.md", count=1),),
        )


@pytest.mark.asyncio
async def test_search_library_delegates_filters_to_configured_index() -> None:
    index = FakeSearchIndex()
    service = SearchLibrary(index)
    created_after = datetime(2026, 1, 1, tzinfo=UTC)
    created_before = datetime(2026, 2, 1, tzinfo=UTC)
    output = CleanedOutput(
        document_id=DocumentId("doc_search"),
        run_id=RunId("run_search"),
        text="Indexed cleaned text",
        prompt_version="cmos_v2",
        model_provider="mock",
        model_name="mock-cleaner",
    )
    classification = Classification(
        document_id=DocumentId("doc_search"),
        code="636.1",
        label="Animal husbandry",
        summary="Horse care",
        taxonomy="dewey",
        confidence=0.9,
    )

    await service.index_output(output, classification)
    ids = await service.search(
        "follow-up care",
        limit=5,
        offset=2,
        classification_code="636.1",
        document_status=DocumentStatus.READY,
        filename_contains="notes",
        created_after=created_after,
        created_before=created_before,
        scope="raw",
        phrase=True,
    )
    results = await service.results(
        "follow-up care",
        limit=5,
        offset=2,
        classification_code="636.1",
        document_status=DocumentStatus.READY,
        filename_contains="notes",
        created_after=created_after,
        created_before=created_before,
        scope="raw",
        phrase=True,
    )
    count = await service.count(
        "follow-up care",
        classification_code="636.1",
        document_status=DocumentStatus.READY,
        filename_contains="notes",
        created_after=created_after,
        created_before=created_before,
        scope="raw",
        phrase=True,
    )
    facets = await service.facets(
        "follow-up care",
        classification_code="636.1",
        document_status=DocumentStatus.READY,
        filename_contains="notes",
        created_after=created_after,
        created_before=created_before,
        scope="raw",
        phrase=True,
    )
    expected_filters = {
        "classification_code": "636.1",
        "document_status": DocumentStatus.READY,
        "filename_contains": "notes",
        "created_after": created_after,
        "created_before": created_before,
        "scope": "raw",
        "phrase": True,
    }

    assert ids == [DocumentId("doc_search")]
    assert results[0].snippet == "<mark>horse</mark>"
    assert count == 1
    assert facets.classifications[0].value == "636.1"
    assert index.calls == [
        (
            "index",
            "Indexed cleaned text",
            {"classification_code": "636.1"},
        ),
        (
            "search",
            "follow-up care",
            {"limit": 5, "offset": 2, **expected_filters},
        ),
        (
            "search_results",
            "follow-up care",
            {"limit": 5, "offset": 2, **expected_filters},
        ),
        ("search_count", "follow-up care", expected_filters),
        ("search_facets", "follow-up care", expected_filters),
    ]
