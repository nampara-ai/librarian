from librarian.prompts.loader import PromptCatalog


def test_prompt_catalog_loads_cleaning_prompt() -> None:
    prompt = PromptCatalog().get("cleaning", "cmos_v2")

    assert "Chicago Manual of Style" in prompt
    assert "do not summarize" in prompt
    assert "OCR errors" in prompt


def test_prompt_catalog_loads_v4_cleaning_prompt_with_heading_inference() -> None:
    prompt = PromptCatalog().get("cleaning", "cmos_v4")

    # Everything cmos_v3 guaranteed still holds...
    assert "Chicago Manual of Style" in prompt
    assert "do not summarize" in prompt
    assert "Verbatim data" in prompt
    assert "Markdown tables" in prompt
    # ...plus the tightly-scoped implicit-structure rule: plain-text titles
    # and standalone chapter/section lines become Markdown headings, repeated
    # running title blocks are dropped, and nothing else is promoted.
    assert "Implicit structure" in prompt
    assert "`#` heading" in prompt
    assert "`##` heading" in prompt
    assert "running header" in prompt
    assert "never invent a heading for an ordinary sentence" in prompt


def test_prompt_catalog_loads_classification_prompt() -> None:
    prompt = PromptCatalog().get("classification", "dewey_v2")

    assert "Dewey Decimal Classification" in prompt
    assert "340 = Law" in prompt
    assert "confidence" in prompt


def test_prompt_catalog_loads_v3_classification_prompt() -> None:
    prompt = PromptCatalog().get("classification", "dewey_v3")

    assert "Dewey Decimal Classification" in prompt
    assert "80 to 100 words" in prompt
    assert '"title"' in prompt
    assert '"tags"' in prompt
    assert "confidence" in prompt


def test_prompt_catalog_loads_v4_classification_prompt() -> None:
    prompt = PromptCatalog().get("classification", "dewey_v4")

    assert "Dewey Decimal Classification" in prompt
    assert "single-sentence description" in prompt
    assert '"description"' in prompt
    assert '"title"' in prompt
    assert '"tags"' in prompt


def test_prompt_catalog_caches_prompt_reads() -> None:
    catalog = PromptCatalog()
    catalog.clear_cache()

    catalog.get("cleaning", "cmos_v1")
    catalog.get("cleaning", "cmos_v1")

    assert catalog.cache_info().hits == 1
