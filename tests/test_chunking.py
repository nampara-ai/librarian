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
