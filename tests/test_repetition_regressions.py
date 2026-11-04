import json

import pytest

from librarian.application.clean_chunks import CleanChunks
from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk
from librarian.pipeline.validation import validate_cleaned_text
from librarian.prompts import PromptCatalog


def test_new_adjacent_copied_fragment_is_rejected_without_deleting_it() -> None:
    source = "The committee accepted the revised schedule. Work began immediately."
    candidate = (
        "The committee accepted the revised schedule.\n\n"
        "accepted the revised schedule. Work began immediately."
    )
    result = validate_cleaned_text(candidate, input_size=len(source), source_text=source)
    assert "new-adjacent-repetition" in result.warnings
    assert result.text == candidate


def test_intentional_source_repetition_remains_valid() -> None:
    source = "We will finish the work. We will finish the work."
    result = validate_cleaned_text(source, input_size=len(source), source_text=source)
    assert "new-adjacent-repetition" not in result.warnings


def test_repeated_code_is_outside_prose_repetition_detection() -> None:
    source = "```python\nprint('run the full test suite')\nprint('run the full test suite')\n```"
    result = validate_cleaned_text(source, input_size=len(source), source_text=source)
    assert "new-adjacent-repetition" not in result.warnings


@pytest.mark.asyncio
async def test_document_join_failure_retries_real_work_instead_of_reusing_the_same_draft() -> None:
    class Editor:
        name = "join-test"
        second_calls = 0

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            unit = payload["units"][0]
            text = unit["text"]
            if text.startswith("The board"):
                self.second_calls += 1
                if self.second_calls == 1:
                    text = "Finish the work today. " + text
            return json.dumps({"edits": [{"id": unit["id"], "text": text}]})

    source = "Please finish the work today.\n\nThe board will publish the timetable."
    chunk = Chunk(ChunkId("join"), DocumentId("doc"), 0, source, 0, len(source), "source")
    editor = Editor()
    cleaner = CleanChunks(
        editor, PromptCatalog(), "cmos_v6", "test", verify_semantics=False, max_fidelity_retries=1
    )
    assert (await cleaner.execute([chunk]))[0].text == source
    assert editor.second_calls == 2
