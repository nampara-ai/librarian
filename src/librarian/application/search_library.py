"""Application service for library search."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from librarian.application.ports import SearchIndex, SearchScope
from librarian.domain.ids import DocumentId
from librarian.domain.models import (
    Classification,
    CleanedOutput,
    DocumentStatus,
    SearchFacets,
    SearchResult,
)


@dataclass(frozen=True, slots=True)
class SearchLibrary:
    """Search documents through the configured search index adapter."""

    index: SearchIndex

    async def index_output(
        self,
        output: CleanedOutput,
        classification: Classification | None,
    ) -> None:
        """Index a cleaned output for subsequent search."""
        await self.index.index(output, classification)

    async def search(
        self,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
        classification_code: str | None = None,
        document_status: DocumentStatus | None = None,
        filename_contains: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        scope: SearchScope = "cleaned",
        phrase: bool = False,
    ) -> Sequence[DocumentId]:
        """Return document IDs matching a query."""
        return await self.index.search(
            query,
            limit=limit,
            offset=offset,
            classification_code=classification_code,
            document_status=document_status,
            filename_contains=filename_contains,
            created_after=created_after,
            created_before=created_before,
            scope=scope,
            phrase=phrase,
        )

    async def results(
        self,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
        classification_code: str | None = None,
        document_status: DocumentStatus | None = None,
        filename_contains: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        scope: SearchScope = "cleaned",
        phrase: bool = False,
    ) -> Sequence[SearchResult]:
        """Return ranked result records matching a query."""
        return await self.index.search_results(
            query,
            limit=limit,
            offset=offset,
            classification_code=classification_code,
            document_status=document_status,
            filename_contains=filename_contains,
            created_after=created_after,
            created_before=created_before,
            scope=scope,
            phrase=phrase,
        )

    async def count(
        self,
        query: str,
        *,
        classification_code: str | None = None,
        document_status: DocumentStatus | None = None,
        filename_contains: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        scope: SearchScope = "cleaned",
        phrase: bool = False,
    ) -> int:
        """Return the total result count for the query and filters."""
        return await self.index.search_count(
            query,
            classification_code=classification_code,
            document_status=document_status,
            filename_contains=filename_contains,
            created_after=created_after,
            created_before=created_before,
            scope=scope,
            phrase=phrase,
        )

    async def facets(
        self,
        query: str,
        *,
        classification_code: str | None = None,
        document_status: DocumentStatus | None = None,
        filename_contains: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        scope: SearchScope = "cleaned",
        phrase: bool = False,
    ) -> SearchFacets:
        """Return facet counts for the query and filters."""
        return await self.index.search_facets(
            query,
            classification_code=classification_code,
            document_status=document_status,
            filename_contains=filename_contains,
            created_after=created_after,
            created_before=created_before,
            scope=scope,
            phrase=phrase,
        )
