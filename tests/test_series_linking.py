import json

import pytest

from librarian.application.classify_document import ClassifyDocument
from librarian.domain.ids import DocumentId
from librarian.prompts import PromptCatalog
from librarian.taxonomy.dewey import DeweyTaxonomy


class _CannedProvider:
    """Returns one fixed completion regardless of prompt."""

    name = "canned"

    def __init__(self, response: str) -> None:
        self._response = response

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        del system_prompt, user_prompt, model, max_tokens, temperature
        return self._response


def _payload(**extra: object) -> str:
    base: dict[str, object] = {
        "summary": "A market report.",
        "dewey_code": "330",
        "category_name": "Economics",
        "title": "Dallas Office MarketView",
        "confidence": 0.9,
    }
    base.update(extra)
    return json.dumps(base)


async def _classify(response: str, *, filename: str | None = None):
    classifier = ClassifyDocument(
        provider=_CannedProvider(response),
        prompt_catalog=PromptCatalog(),
        prompt_version="dewey_v5",
        model="mock-cleaner",
        taxonomy=DeweyTaxonomy(),
    )
    return await classifier.execute(
        DocumentId("doc_test"),
        "Quarterly Dallas office absorption and vacancy.",
        source_filename=filename,
    )


@pytest.mark.asyncio
async def test_classifier_extracts_issuer_series_and_period() -> None:
    result = await _classify(
        _payload(issuer="CBRE", series="MarketView — Dallas Office", period="June 2026")
    )
    assert result.issuer == "CBRE"
    assert result.series_title == "MarketView — Dallas Office"
    assert result.period == "2026-06"
    assert result.series_key == "cbre-marketview-dallas-office"


@pytest.mark.asyncio
async def test_period_is_canonicalized_to_orderable_tokens() -> None:
    assert (await _classify(_payload(period="2026-05"))).period == "2026-05"
    assert (await _classify(_payload(period="May 2026"))).period == "2026-05"
    assert (await _classify(_payload(period="Q2 2026"))).period == "2026-Q2"
    assert (await _classify(_payload(period="H1 2026"))).period == "2026-H1"
    assert (await _classify(_payload(period="2026"))).period == "2026"
    # Unrecognized free text is preserved rather than dropped.
    assert (await _classify(_payload(period="interim"))).period == "interim"


@pytest.mark.asyncio
async def test_editions_converge_to_one_series_key() -> None:
    # The same recurring report named slightly differently month to month (and with
    # the date embedded) must still produce an identical series_key.
    may = await _classify(_payload(issuer="CBRE", series="MarketView Dallas Office May 2026"))
    june = await _classify(
        _payload(issuer="CBRE", series="MarketView, Dallas — Office (June 2026)")
    )
    assert may.series_key == june.series_key == "cbre-marketview-dallas-office"


@pytest.mark.asyncio
async def test_filename_fallback_converges_across_months() -> None:
    june = await _classify(_payload(), filename="CBRE_Dallas_Office_MarketView_2026-06.pdf")
    july = await _classify(_payload(), filename="CBRE_Dallas_Office_MarketView_2026-07.pdf")
    assert june.series_key == july.series_key == "cbre-dallas-office-marketview"
    assert june.issuer is None
    assert june.series_title is None


@pytest.mark.asyncio
async def test_generic_or_missing_filename_creates_no_series() -> None:
    for filename in ("report.pdf", "scan_2026-06.pdf", "final-draft.pdf", None):
        result = await _classify(_payload(), filename=filename)
        assert result.series_key is None


@pytest.mark.asyncio
async def test_pre_v5_payload_leaves_series_fields_unset() -> None:
    result = await _classify(_payload())
    assert result.issuer is None
    assert result.series_key is None
    assert result.series_title is None
    assert result.period is None
