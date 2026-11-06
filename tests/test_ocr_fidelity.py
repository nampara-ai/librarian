import pytest

from librarian.ingest.extractors import OcrCorrectionResult, PdfExtractor


class CorrectionExtractor(PdfExtractor):
    async def correct(
        self, *, page_number: int, text: str, confidence: float | None = None
    ) -> OcrCorrectionResult:
        return await self._correct_ocr_page(
            page_number=page_number, text=text, confidence=confidence
        )


@pytest.mark.asyncio
async def test_unknown_confidence_is_checked_and_numeric_loss_retried() -> None:
    class Provider:
        name = "test"

        def __init__(self) -> None:
            self.requests: list[str] = []

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            self.requests.append(user_prompt)
            return "There were failures." if len(self.requests) == 1 else "There were 15 failures."

    provider = Provider()
    extractor = CorrectionExtractor(
        ocr_correction_provider=provider, ocr_correction_mode="low-confidence"
    )
    result = await extractor.correct(
        page_number=1, text="There were 15 failures.", confidence=None
    )
    assert result.text == "There were 15 failures."
    assert len(provider.requests) == 2
    assert "missing-verbatim-number" in provider.requests[1]
    assert provider.requests[1].endswith("There were 15 failures.")


@pytest.mark.asyncio
async def test_ocr_fidelity_exhaustion_never_returns_damaged_text() -> None:
    class Provider:
        name = "test"
        calls = 0

        async def complete(self, **_: object) -> str:
            self.calls += 1
            return "There were 20 failures."

    provider = Provider()
    extractor = CorrectionExtractor(ocr_correction_provider=provider)
    with pytest.raises(ValueError, match="after 3 attempts"):
        await extractor.correct(page_number=2, text="There were 15 failures.")
    assert provider.calls == 3
