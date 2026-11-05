import json

import pytest

from librarian.application.clean_chunks import CleanChunks, FidelityValidationError
from librarian.application.errors import LLMResponseTruncatedError
from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk
from librarian.prompts import PromptCatalog


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["json", "truncated"])
async def test_invalid_review_retries_the_reviewer_without_redrafting(failure: str) -> None:
    class Editor:
        name = "editor"
        calls = 0

        async def complete(self, **_: object) -> str:
            self.calls += 1
            return "The report is ready."

    class Reviewer:
        name = "reviewer"
        calls = 0

        async def complete(self, **_: object) -> str:
            self.calls += 1
            if self.calls == 1:
                if failure == "truncated":
                    raise LLMResponseTruncatedError("limit", partial_text='{"issues":')
                return '{"issues": "invalid shape"}'
            return json.dumps({"issues": []})

    source = "The report is ready."
    chunk = Chunk(ChunkId("review"), DocumentId("doc"), 0, source, 0, len(source), "source")
    editor, reviewer = Editor(), Reviewer()
    cleaner = CleanChunks(
        editor, PromptCatalog(), "cmos_v8", "test", verification_provider=reviewer
    )
    assert (await cleaner.execute([chunk]))[0].text == source
    assert editor.calls == 1
    assert reviewer.calls == 2


@pytest.mark.asyncio
async def test_invalid_review_exhaustion_does_not_change_the_candidate_or_claim_success() -> None:
    class Editor:
        name = "editor"
        calls = 0

        async def complete(self, **_: object) -> str:
            self.calls += 1
            return "The report is ready."

    class Reviewer:
        name = "reviewer"
        calls = 0

        async def complete(self, **_: object) -> str:
            self.calls += 1
            return "not JSON"

    source = "The report is ready."
    chunk = Chunk(ChunkId("review"), DocumentId("doc"), 0, source, 0, len(source), "source")
    editor, reviewer = Editor(), Reviewer()
    cleaner = CleanChunks(
        editor, PromptCatalog(), "cmos_v8", "test", verification_provider=reviewer
    )
    with pytest.raises(FidelityValidationError, match="invalid-fidelity-verification"):
        await cleaner.execute([chunk])
    assert editor.calls == 1
    assert reviewer.calls == 2
