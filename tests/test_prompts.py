from librarian.prompts.loader import PromptCatalog


def test_prompt_catalog_loads_cleaning_prompt() -> None:
    prompt = PromptCatalog().get("cleaning", "cmos_v1")

    assert "Chicago Manual of Style" in prompt
    assert "Do NOT summarize" in prompt
