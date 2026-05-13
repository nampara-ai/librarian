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
