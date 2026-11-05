from dataclasses import replace

from librarian.application.assemble_document import assemble_cleaned_document
from librarian.application.clean_chunks import CleanedChunk
from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk


def test_assembly_removes_context_artifacts_and_duplicate_boundaries() -> None:
    chunks = [
        _cleaned_chunk(0, "First sentence. Duplicate sentence."),
        _cleaned_chunk(
            1,
            "[CONTEXT: This continues from: Duplicate sentence.]\n\n"
            "Duplicate sentence. Second sentence.\n"
            "Here is the cleaned transcript:\n"
            "## Heading ## Heading",
        ),
    ]

    assembled = assemble_cleaned_document(chunks)

    assert "[CONTEXT:" not in assembled
    assert "Here is the cleaned transcript" not in assembled
    assert assembled.count("Duplicate sentence.") == 1
    assert "## Heading ## Heading" not in assembled
    assert "## Heading" in assembled


def test_assembly_preserves_markdown_rendering_quality_at_chunk_boundaries() -> None:
    chunks = [
        _cleaned_chunk(
            0,
            "# Visit Notes\n\n"
            "[Page 1]\n\n"
            "Opening paragraph with citation [A12].\n\n"
            "- First finding\n"
            "- Second finding\n\n"
            "| Field | Value |\n"
            "|---|---|\n"
            "| Gait | Canter |\n\n"
            "Boundary sentence.",
        ),
        _cleaned_chunk(
            1,
            "[CONTEXT: This continues from: Boundary sentence.]\n\n"
            "Boundary sentence. Follow-up paragraph remains separate.\n\n"
            "1. Ordered action\n"
            "2. Confirmed action\n\n"
            "(Smith, 2024)\n"
            "I have cleaned the transcript.",
        ),
    ]

    assembled = assemble_cleaned_document(chunks)

    assert assembled == (
        "# Visit Notes\n\n"
        "[Page 1]\n\n"
        "Opening paragraph with citation [A12].\n\n"
        "- First finding\n"
        "- Second finding\n\n"
        "| Field | Value |\n"
        "|---|---|\n"
        "| Gait | Canter |\n\n"
        "Boundary sentence.\n\n"
        "Follow-up paragraph remains separate.\n\n"
        "1. Ordered action\n"
        "2. Confirmed action\n\n"
        "(Smith, 2024)"
    )


def test_assembly_removes_empty_code_fences() -> None:
    assembled = assemble_cleaned_document(
        [_cleaned_chunk(0, "Before.\n\n```text\n\n```\n\nAfter.")]
    )

    assert assembled == "Before.\n\nAfter."


def test_assembly_preserves_intentional_repetition_and_code() -> None:
    source = (
        "Never again. Never again. We meant it.\n\n"
        'She said, "I cannot read the inscription."\n\n'
        "```python\nif ready:\n    run()\n\n\n\n    run()\n```"
    )
    chunk = _cleaned_chunk(0, source)
    chunk = replace(chunk, chunk=replace(chunk.chunk, text=source))
    assert assemble_cleaned_document([chunk]) == source


def test_assembly_preserves_repetition_that_exists_in_both_source_chunks() -> None:
    chunks = [_cleaned_chunk(0, "Never again."), _cleaned_chunk(1, "Never again. We meant it.")]
    chunks = [replace(chunk, chunk=replace(chunk.chunk, text=chunk.text)) for chunk in chunks]
    assert assemble_cleaned_document(chunks) == "Never again.\n\nNever again. We meant it."


def _cleaned_chunk(ordinal: int, text: str) -> CleanedChunk:
    chunk = Chunk(
        id=ChunkId(f"chk_{ordinal}"),
        document_id=DocumentId("doc_test"),
        ordinal=ordinal,
        text=f"source {ordinal}",
        start_char=ordinal,
        end_char=ordinal + 1,
        sha256=f"sha-{ordinal}",
    )
    return CleanedChunk(chunk=chunk, text=text, warnings=())


def test_assembly_keeps_source_repetition_when_cleaner_supplies_missing_punctuation() -> None:
    first = _cleaned_chunk(0, "We will finish the work.")
    second = _cleaned_chunk(1, "We will finish the work. I promise.")
    second = replace(second, chunk=replace(second.chunk, text="we will finish the work i promise"))
    assert assemble_cleaned_document([first, second]) == (
        "We will finish the work.\n\nWe will finish the work. I promise."
    )


def test_assembly_preserves_source_joins_inside_sentences_and_between_paragraphs() -> None:
    from librarian.pipeline.chunking import ChunkingPolicy, chunk_text

    source = (
        "The committee considered the evidence and carefully weighed each proposal before deciding "
        "which members would speak next. The discussion continued while the clerk recorded it."
        "\n\nThe next session began after lunch and ended promptly."
    )
    chunks = chunk_text(
        DocumentId("joins"), source, ChunkingPolicy(target_chars=70, overlap_chars=10)
    )
    assert len(chunks) > 2
    assert (
        assemble_cleaned_document(
            [CleanedChunk(chunk, chunk.text, ()) for chunk in chunks], source_text=source
        )
        == source
    )


def test_assembly_uses_source_offsets_even_when_the_words_were_copyedited() -> None:
    source = "Teh first clause continues through the second."
    left = _cleaned_chunk(0, "The first clause")
    left = replace(
        left, chunk=replace(left.chunk, text="Teh first clause", start_char=0, end_char=16)
    )
    right_text = "continues through the second."
    right = _cleaned_chunk(1, right_text)
    start = source.index(right_text)
    right = replace(
        right, chunk=replace(right.chunk, text=right_text, start_char=start, end_char=len(source))
    )
    assert assemble_cleaned_document([left, right], source_text=source) == (
        "The first clause continues through the second."
    )


def test_source_backed_assembly_never_deletes_an_audited_repetition() -> None:
    first_source = "Never again."
    second_source = "and never again we meant it"
    source = first_source + " " + second_source
    first = _cleaned_chunk(0, first_source)
    first = replace(
        first,
        chunk=replace(first.chunk, text=first_source, start_char=0, end_char=len(first_source)),
    )
    second = _cleaned_chunk(1, "Never again. We meant it.")
    start = len(first_source) + 1
    second = replace(
        second,
        chunk=replace(second.chunk, text=second_source, start_char=start, end_char=len(source)),
    )
    assert assemble_cleaned_document([first, second], source_text=source) == (
        "Never again. Never again. We meant it."
    )
