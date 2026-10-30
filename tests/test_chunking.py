from librarian.domain.ids import DocumentId
from librarian.pipeline.chunking import ChunkingPolicy, chunk_text


def test_chunk_text_is_deterministic() -> None:
    text = "\n\n".join(f"Paragraph {index}. This is source text." for index in range(100))
    policy = ChunkingPolicy(target_chars=500, overlap_chars=50, min_chunk_chars=100)

    first = chunk_text(DocumentId("doc_test"), text, policy)
    second = chunk_text(DocumentId("doc_test"), text, policy)

    assert first == second
    assert len(first) > 1
    assert [chunk.ordinal for chunk in first] == list(range(len(first)))


def test_chunk_text_returns_empty_for_blank_input() -> None:
    assert chunk_text(DocumentId("doc_test"), "   \n\n ", ChunkingPolicy()) == []


def test_chunking_policy_rejects_invalid_overlap() -> None:
    try:
        ChunkingPolicy(target_chars=100, overlap_chars=100)
    except ValueError as exc:
        assert "overlap_chars" in str(exc)
    else:
        raise AssertionError("expected invalid policy to fail")


def test_unpunctuated_transcript_never_splits_words() -> None:
    text = " ".join("presupposes amazing things happen" for _ in range(80))
    chunks = chunk_text(
        DocumentId("words"),
        text,
        ChunkingPolicy(target_chars=67, overlap_chars=5, min_chunk_chars=5),
    )
    assert len(chunks) > 3
    for chunk in chunks:
        assert text[chunk.start_char : chunk.end_char] == chunk.text
        assert chunk.start_char == 0 or text[chunk.start_char - 1].isspace()
        assert chunk.end_char == len(text) or text[chunk.end_char].isspace()
    assert " ".join(chunk.text for chunk in chunks) == text


def test_chunker_keeps_large_code_blocks_and_tables_atomic() -> None:
    code = "```python\n" + "if ready:\n    run()\n" * 20 + "```"
    table = "| Name | Value |\n|---|---|\n" + "| Alice | 120 |\n" * 20
    text = "Before the code.\n\n" + code + "\n\nBetween blocks.\n\n" + table + "\nAfter."
    chunks = chunk_text(
        DocumentId("blocks"),
        text,
        ChunkingPolicy(target_chars=100, overlap_chars=10, min_chunk_chars=5),
    )
    assert sum(code in chunk.text for chunk in chunks) == 1
    assert sum(table.strip() in chunk.text for chunk in chunks) == 1
    assert all(chunk.text.count("```") in (0, 2) for chunk in chunks)


def test_chunker_does_not_cut_a_long_identifier_without_whitespace() -> None:
    text = "identifier" * 100
    chunks = chunk_text(
        DocumentId("identifier"), text, ChunkingPolicy(target_chars=50, overlap_chars=0)
    )
    assert [chunk.text for chunk in chunks] == [text]
