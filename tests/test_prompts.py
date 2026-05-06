from librarian.prompts.loader import PromptCatalog


def test_prompt_catalog_loads_cleaning_prompt() -> None:
    prompt = PromptCatalog().get("cleaning", "cmos_v1")

    assert "Chicago Manual of Style" in prompt
    assert "Do NOT summarize" in prompt


def test_prompt_catalog_caches_prompt_reads() -> None:
    catalog = PromptCatalog()
    catalog.clear_cache()

    catalog.get("cleaning", "cmos_v1")
    catalog.get("cleaning", "cmos_v1")

    assert catalog.cache_info().hits == 1
