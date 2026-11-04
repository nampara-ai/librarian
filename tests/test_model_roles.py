import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openai.types.chat import ChatCompletion
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message import ChatCompletionMessage

from librarian.application.clean_chunks import CleanChunks
from librarian.application.factory import build_container
from librarian.config import Settings
from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk
from librarian.llm.mock import MockLLMProvider
from librarian.llm.openai_compatible import OpenAICompatibleProvider
from librarian.prompts import PromptCatalog


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", [None, "high"])
@pytest.mark.parametrize("structured", [False, True])
async def test_reasoning_effort_reaches_provider_only_when_configured(
    monkeypatch: pytest.MonkeyPatch, effort: str | None, structured: bool
) -> None:
    requests: list[dict[str, object]] = []

    async def create(**kwargs: object) -> ChatCompletion:
        requests.append(kwargs)
        return ChatCompletion(
            id="test",
            created=0,
            model="test",
            object="chat.completion",
            choices=[
                Choice(
                    index=0,
                    finish_reason="stop",
                    message=ChatCompletionMessage(role="assistant", content="Done."),
                )
            ],
        )

    def client(**_: object) -> Any:
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setenv("LIBRARIAN_TEST_ROLE_KEY", "test")
    monkeypatch.setattr("librarian.llm.openai_compatible.AsyncOpenAI", client)
    provider = OpenAICompatibleProvider(
        api_key_env="LIBRARIAN_TEST_ROLE_KEY",
        base_url=None,
        timeout_seconds=1,
        max_concurrency=1,
        reasoning_effort=effort,
    )
    operation = provider.complete_json if structured else provider.complete
    assert (
        await operation(
            system_prompt="Edit.",
            user_prompt="Source.",
            model="test",
            max_tokens=512,
            temperature=0,
        )
        == "Done."
    )
    if effort is None:
        assert "reasoning_effort" not in requests[0]
    else:
        assert requests[0]["reasoning_effort"] == effort
    assert ("response_format" in requests[0]) is structured
    if structured:
        assert requests[0]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_factory_keeps_editor_and_verifier_profiles_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles: list[Settings] = []

    def build(settings: Settings, **_: object) -> MockLLMProvider:
        profiles.append(settings)
        return MockLLMProvider()

    monkeypatch.setattr("librarian.application.factory.build_provider", build)
    container = await build_container(
        Settings(
            data_dir=tmp_path,
            database_path=tmp_path / "test.sqlite",
            llm_model="editor",
            llm_reasoning_effort="high",
            cleaning_verification_model="reviewer",
            cleaning_verification_reasoning_effort="medium",
        )
    )
    cleaner = container.process_document.cleaner
    assert cleaner.model == "editor"
    assert cleaner.verification_model == "reviewer"
    assert cleaner.provider is not cleaner.verification_provider
    assert profiles[0].llm_model == "editor"
    assert profiles[0].llm_reasoning_effort == "high"
    assert profiles[1].llm_model == "reviewer"
    assert profiles[1].llm_reasoning_effort == "medium"


@pytest.mark.asyncio
async def test_editing_reasoning_override_keeps_classification_profile(tmp_path: Path) -> None:
    container = await build_container(
        Settings(
            data_dir=tmp_path,
            database_path=tmp_path / "roles.sqlite",
            cleaning_reasoning_effort="high",
            cleaning_temperature=1.0,
        )
    )
    assert container.process_document.cleaner.provider is not (
        container.process_document.classifier.provider
    )
    assert container.process_document.cleaner.preparation_provider is (
        container.process_document.classifier.provider
    )
    assert (
        json.loads(container.process_document.cleaner.generation_signature)["editing_reasoning"]
        == "high"
    )
    assert container.process_document.cleaner.temperature == 1.0


@pytest.mark.asyncio
async def test_independent_verifier_can_reject_and_repair_an_editor_claim() -> None:
    class Editor:
        name = "editor"
        calls = 0

        async def complete(self, *, user_prompt: str, model: str, **_: object) -> str:
            assert model == "editor-model"
            unit = json.loads(user_prompt)["units"][0]
            self.calls += 1
            text = (
                "The treatment improved survival."
                if self.calls == 1
                else ("The treatment did not improve survival.")
            )
            return json.dumps({"edits": [{"id": unit["id"], "text": text}]})

    class Reviewer:
        name = "reviewer"
        calls = 0

        async def complete(self, *, user_prompt: str, model: str, **_: object) -> str:
            assert model == "review-model"
            self.calls += 1
            payload = json.loads(user_prompt)
            pair = payload.get("pair") or payload["pairs"][0]
            issues = (
                []
                if "not improve" in pair["edited"]
                else [{"id": pair["id"], "problem": "Negation was reversed."}]
            )
            return json.dumps({"issues": issues})

    source = "The treatment did not improve survival"
    chunk = Chunk(ChunkId("claim"), DocumentId("doc"), 0, source, 0, len(source), "claim")
    editor, reviewer = Editor(), Reviewer()
    cleaner = CleanChunks(
        editor,
        PromptCatalog(),
        "cmos_v6",
        "editor-model",
        verification_provider=reviewer,
        verification_model="review-model",
    )
    output = await cleaner.execute([chunk])
    assert output[0].text == "The treatment did not improve survival."
    assert editor.calls == 2
    assert reviewer.calls == 3
