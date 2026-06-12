import json
from pathlib import Path

from librarian.application.export_document import ExportedDocument
from librarian.domain.ids import DocumentId, RunId
from librarian.domain.models import Classification, CleanedOutput, Document, SourceFile


def _document(filename: str = "notes.txt") -> Document:
    return Document(
        id=DocumentId("doc_test"),
        source=SourceFile(
            path=Path("sources") / "notes.txt",
            filename=filename,
            media_type="text/plain",
            byte_size=100,
            sha256="0" * 64,
        ),
    )


def _output(text: str = "Cleaned body text.") -> CleanedOutput:
    return CleanedOutput(
        document_id=DocumentId("doc_test"),
        run_id=RunId("run_test"),
        text=text,
        prompt_version="cmos_v2",
        model_provider="mock",
        model_name="mock-cleaner",
    )


def _classification(
    *,
    title: str | None = "Saddle Fit Field Notes",
    tags: tuple[str, ...] = ("saddle fit", "groundwork"),
) -> Classification:
    return Classification(
        document_id=DocumentId("doc_test"),
        code="636.1",
        label="Horses & Equines",
        summary="A synopsis of the document.",
        confidence=0.9,
        title=title,
        tags=tags,
    )


def test_markdown_render_leads_with_title_synopsis_and_tags() -> None:
    exported = ExportedDocument(_document(), _output(), _classification())

    rendered = exported.render("md")

    assert rendered.startswith("# Saddle Fit Field Notes\n")
    assert "> A synopsis of the document." in rendered
    assert "Classification: 636.1 - Horses & Equines" in rendered
    assert "Tags: saddle fit, groundwork" in rendered
    assert rendered.endswith("---\n\nCleaned body text.")
    assert rendered.index("> A synopsis") < rendered.index("Cleaned body text.")


def test_markdown_render_without_classification_uses_filename_stem() -> None:
    exported = ExportedDocument(_document("report.docx"), _output(), None)

    rendered = exported.render("md")

    assert rendered.startswith("# report\n")
    assert "Classification:" not in rendered
    assert "Tags:" not in rendered
    assert ">" not in rendered
    assert rendered.endswith("---\n\nCleaned body text.")


def test_txt_render_stays_verbatim_cleaned_text() -> None:
    exported = ExportedDocument(_document(), _output("Exact cleaned text."), _classification())

    assert exported.render("txt") == "Exact cleaned text."


def test_json_render_includes_synopsis_title_tags_and_stem() -> None:
    exported = ExportedDocument(_document(), _output(), _classification())

    payload = json.loads(exported.render("json"))

    assert payload["title"] == "Saddle Fit Field Notes"
    assert payload["summary"] == "A synopsis of the document."
    assert payload["tags"] == ["saddle fit", "groundwork"]
    assert payload["suggested_stem"] == "636.1 Saddle Fit Field Notes"
    assert payload["text"] == "Cleaned body text."


def test_export_stem_combines_code_and_title() -> None:
    exported = ExportedDocument(_document(), _output(), _classification())

    assert exported.export_stem() == "636.1 Saddle Fit Field Notes"


def test_export_stem_sanitizes_hostile_title_characters() -> None:
    classification = _classification(title='Bad/Name: "Evil;Stuff"\r\n<>|*?')

    exported = ExportedDocument(_document(), _output(), classification)

    assert exported.export_stem() == "636.1 Bad Name Evil Stuff"


def test_export_stem_falls_back_to_sanitized_source_filename() -> None:
    exported = ExportedDocument(_document('../"bad;name"\r\n.txt'), _output(), None)
    untitled = ExportedDocument(
        _document("notes.txt"), _output(), _classification(title=None)
    )

    assert exported.export_stem() == "bad name"
    assert untitled.export_stem() == "notes"


def test_export_stem_caps_length_and_never_returns_empty() -> None:
    long_title = "Word " * 60
    capped = ExportedDocument(_document(), _output(), _classification(title=long_title))
    hostile = ExportedDocument(_document("///.txt"), _output(), None)

    assert len(capped.export_stem()) <= 100
    assert not capped.export_stem().endswith(" ")
    assert hostile.export_stem() == "document"
