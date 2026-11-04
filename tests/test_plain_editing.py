import json

import pytest

from librarian.application.clean_chunks import CleanChunks
from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk
from librarian.prompts import PromptCatalog


@pytest.mark.asyncio
async def test_plain_editor_returns_prose_while_application_preserves_atomic_data() -> None:
    class Editor:
        name = "plain-test"
        sources: list[str] = []

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            assert "units" not in payload
            source = payload["source"]
            self.sources.append(source)
            return source.replace("teh", "the")

    code = "```python\nif ready:\n    run(12)\n```"
    table = "| Name | Value |\n| --- | --- |\n| Nora | 15 |"
    source = "We reviewed teh result.\n\n" + code + "\n\n" + table
    chunk = Chunk(ChunkId("plain"), DocumentId("doc"), 0, source, 0, len(source), "source")
    editor = Editor()
    cleaner = CleanChunks(editor, PromptCatalog(), "cmos_v8", "test", verify_semantics=False)
    result = (await cleaner.execute([chunk]))[0]
    assert "We reviewed the result." in result.text
    assert code in result.text
    assert table in result.text
    assert editor.sources == ["We reviewed teh result."]


@pytest.mark.asyncio
async def test_atomic_only_source_requires_no_generation_or_review() -> None:
    class Editor:
        name = "unused"

        async def complete(self, **_: object) -> str:
            raise AssertionError("Atomic source must remain outside generation")

    source = "```python\nif ready:\n    run()\n```"
    chunk = Chunk(ChunkId("code"), DocumentId("doc"), 0, source, 0, len(source), "source")
    cleaner = CleanChunks(Editor(), PromptCatalog(), "cmos_v8", "test")
    assert (await cleaner.execute([chunk]))[0].text == source


@pytest.mark.asyncio
async def test_semantic_repair_patches_draft_without_rewriting_accepted_prose() -> None:
    class Editor:
        name = "patch-editor"
        drafts = 0
        patches = 0

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            if "candidate_spans" in payload:
                self.patches += 1
                span = payload["candidate_spans"][0]
                return json.dumps(
                    {
                        "patches": [
                            {
                                "id": span["id"],
                                "find": "improved survival",
                                "replace": "did not improve survival",
                            }
                        ]
                    }
                )
            self.drafts += 1
            return "The treatment improved survival. The follow-up report remains pending."

    class Reviewer:
        name = "reviewer"

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            pair = payload.get("pair") or payload["pairs"][0]
            return json.dumps(
                {
                    "issues": []
                    if "not improve" in pair["edited"]
                    else [
                        {
                            "id": pair["id"],
                            "problem": "Restore the original negation.",
                        }
                    ]
                }
            )

    source = "The treatment did not improve survival. The follow-up report remains pending."
    chunk = Chunk(ChunkId("claim"), DocumentId("doc"), 0, source, 0, len(source), "claim")
    editor = Editor()
    cleaner = CleanChunks(
        editor, PromptCatalog(), "cmos_v8", "test", verification_provider=Reviewer()
    )
    result = (await cleaner.execute([chunk]))[0]
    assert result.text == source
    assert editor.drafts == 1
    assert editor.patches == 1
