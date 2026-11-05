import json
from collections import Counter
from typing import Literal

import pytest

from librarian.application.clean_chunks import CleanChunks
from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk
from librarian.prompts import PromptCatalog


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["advisory", "strict"])
async def test_passed_units_are_never_edited_or_reviewed_again(
    mode: Literal["advisory", "strict"],
) -> None:
    class Editor:
        name = "editor"
        calls: Counter[str] = Counter()

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            source = payload["source"]
            name = next(label for label in ("first", "second", "third") if label in source)
            self.calls[name] += 1
            if name == "second" and self.calls[name] < 3:
                return "The second result was unknown."
            return source.replace("teh", "The").replace("the second", "The second")

    class Reviewer:
        name = "reviewer"
        calls: Counter[str] = Counter()

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            pair = json.loads(user_prompt)["pairs"][0]
            name = next(label for label in ("first", "second", "third") if label in pair["source"])
            self.calls[name] += 1
            return json.dumps({"issues": []})

    source = (
        "teh first result stands.\n\nthe second result was 318 units.\n\nteh third result stands."
    )
    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, source, 0, len(source), "sha")
    editor, reviewer = Editor(), Reviewer()
    cleaner = CleanChunks(
        editor,
        PromptCatalog(),
        "cmos_v8",
        "test",
        review_mode=mode,
        verification_provider=reviewer,
        unit_target_chars=45,
        unit_max_retries=2,
    )
    result = (await cleaner.execute([chunk]))[0]
    assert editor.calls == {"first": 1, "second": 3, "third": 1}
    assert reviewer.calls == {"first": 1, "second": 1, "third": 1}
    assert result.text == (
        "The first result stands.\n\nThe second result was 318 units.\n\nThe third result stands."
    )
