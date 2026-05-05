import pytest

from librarian.application.classify_document import ClassifyDocument
from librarian.domain.ids import DocumentId
from librarian.llm.mock import MockLLMProvider
from librarian.prompts import PromptCatalog
from librarian.taxonomy.dewey import DeweyTaxonomy


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
