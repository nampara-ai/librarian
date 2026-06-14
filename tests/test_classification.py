import json

import pytest

from librarian.application.classify_document import ClassifyDocument
from librarian.domain.ids import DocumentId
from librarian.llm.mock import MockLLMProvider
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


def _classifier(provider: MockLLMProvider | _CannedProvider) -> ClassifyDocument:
    return ClassifyDocument(
        provider=provider,
        prompt_catalog=PromptCatalog(),
        prompt_version="dewey_v3",
        model="mock-cleaner",
        taxonomy=DeweyTaxonomy(),
    )


@pytest.mark.asyncio
async def test_structured_classification_with_mock_provider() -> None:
    classifier = ClassifyDocument(
        provider=MockLLMProvider(),
        prompt_catalog=PromptCatalog(),
        prompt_version="dewey_v1",
        model="mock-cleaner",
        taxonomy=DeweyTaxonomy(),
    )

    result = await classifier.execute(
        DocumentId("doc_test"),
        "A horse training transcript about a colt and groundwork.",
    )

    assert result.code == "636.1"
    assert result.label == "Horses & Equines"
    assert result.confidence == 1.0


@pytest.mark.asyncio
async def test_v3_classification_includes_title_and_tags() -> None:
    classifier = _classifier(MockLLMProvider())

    result = await classifier.execute(
        DocumentId("doc_test"),
        "A horse training transcript about a colt and groundwork.",
    )

    assert result.title == "Horses & Equines Notes"
    assert result.tags == ("horses", "equines")


@pytest.mark.asyncio
async def test_classification_includes_one_sentence_description() -> None:
    classifier = _classifier(MockLLMProvider())

    result = await classifier.execute(
        DocumentId("doc_test"),
        "A horse training transcript about a colt and groundwork.",
    )

    assert result.description == "A document about Horses & Equines."


@pytest.mark.asyncio
async def test_payload_without_description_leaves_it_unset() -> None:
    classifier = _classifier(
        _CannedProvider(
            json.dumps(
                {
                    "summary": "A summary.",
                    "dewey_code": "636.1",
                    "category_name": "Horses & Equines",
                    "confidence": 0.9,
                }
            )
        )
    )

    result = await classifier.execute(DocumentId("doc_test"), "Saddle fit notes.")

    assert result.description is None


@pytest.mark.asyncio
async def test_v2_style_payload_without_title_or_tags_still_parses() -> None:
    classifier = _classifier(
        _CannedProvider(
            json.dumps(
                {
                    "summary": "A summary.",
                    "dewey_code": "636.1",
                    "category_name": "Horses & Equines",
                    "confidence": 0.9,
                }
            )
        )
    )

    result = await classifier.execute(DocumentId("doc_test"), "Saddle fit notes.")

    assert result.code == "636.1"
    assert result.title is None
    assert result.tags == ()


@pytest.mark.asyncio
async def test_messy_title_and_tags_are_sanitized() -> None:
    classifier = _classifier(
        _CannedProvider(
            json.dumps(
                {
                    "summary": "A summary.",
                    "dewey_code": "636.1",
                    "category_name": "Horses & Equines",
                    "title": "  Saddle \n Fit   Field Notes  " + "x" * 300,
                    "tags": [
                        " Saddle Fit ",
                        "saddle fit",
                        "",
                        "  ",
                        *(f"tag {index}" for index in range(20)),
                    ],
                    "confidence": 0.9,
                }
            )
        )
    )

    result = await classifier.execute(DocumentId("doc_test"), "Saddle fit notes.")

    assert result.title is not None
    assert result.title.startswith("Saddle Fit Field Notes")
    assert "\n" not in result.title
    assert len(result.title) <= 120
    assert result.tags[0] == "saddle fit"
    assert len(result.tags) == len(set(result.tags)) == 8


@pytest.mark.asyncio
async def test_invalid_payload_falls_back_to_heuristic_without_title() -> None:
    classifier = _classifier(_CannedProvider("this is not json at all"))

    result = await classifier.execute(
        DocumentId("doc_test"),
        "A horse training transcript about a colt and groundwork.",
    )

    assert result.code == "636.1"
    assert result.title is None
    assert result.tags == ()
