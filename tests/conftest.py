"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from librarian.ingest import extractors


@pytest.fixture(autouse=True)
def _reset_page_manifest_context() -> Iterator[None]:
    """Reset the page-manifest ContextVar between tests.

    The extractor stores the current extraction's page-manifest path in a
    ContextVar (so concurrent conversions stay isolated). Tests that call
    ``set_page_manifest_path`` synchronously mutate the test process's own
    context, which would otherwise leak into unrelated tests. Production code
    runs each conversion in its own asyncio task context and also sets the
    value explicitly per conversion, so this only guards test isolation.
    """
    extractors.reset_page_manifest_path()
    try:
        yield
    finally:
        extractors.reset_page_manifest_path()
