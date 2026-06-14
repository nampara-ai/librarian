from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import yaml

from librarian.application.export_okf import OKF_VERSION, OkfSource, build_bundle
from librarian.domain.ids import DocumentId, RunId
from librarian.domain.models import Classification, CleanedOutput, Document, SourceFile
from librarian.taxonomy.dewey import DeweyTaxonomy


def _source(
    *,
    doc_id: str,
    filename: str,
    code: str,
    label: str,
    title: str | None,
    tags: tuple[str, ...] = (),
    description: str | None = None,
    summary: str = "A synopsis. With two sentences.",
    text: str = "Cleaned body text.",
) -> OkfSource:
    document = Document(
        id=DocumentId(doc_id),
        source=SourceFile(
            path=Path("sources") / filename,
            filename=filename,
            media_type="application/octet-stream",
            byte_size=100,
            sha256="0" * 64,
        ),
    )
    output = CleanedOutput(
        document_id=DocumentId(doc_id),
        run_id=RunId("run_test"),
        text=text,
        prompt_version="cmos_v2",
        model_provider="mock",
        model_name="mock-cleaner",
        created_at=datetime(2026, 6, 13, 14, 30, 0, tzinfo=UTC),
    )
    classification = Classification(
        document_id=DocumentId(doc_id),
        code=code,
        label=label,
        summary=summary,
        confidence=0.9,
        title=title,
        tags=tags,
        description=description,
    )
    return OkfSource(document=document, output=output, classification=classification)


def _split_frontmatter(content: str) -> tuple[dict[str, object], str]:
    assert content.startswith("---\n")
    _, frontmatter, body = content.split("---\n", 2)
    parsed: object = yaml.safe_load(frontmatter)
    assert isinstance(parsed, dict)
    return cast(dict[str, object], parsed), body


def test_bundle_is_okf_conformant() -> None:
    files = build_bundle(
        [
            _source(
                doc_id="doc_a",
                filename="saddle.pdf",
                code="636.1",
                label="Horses & Equines",
                title="Saddle Fit Notes",
                tags=("horses", "saddle fit"),
                description="A one-line abstract of the document.",
            ),
            _source(
                doc_id="doc_b",
                filename="catalog.docx",
                code="020",
                label="Library Science",
                title="Catalog Design",
            ),
        ],
        taxonomy=DeweyTaxonomy(),
    )

    # Conformance rule 1+2: every non-index .md has parseable frontmatter with a
    # non-empty `type`.
    concept_files = [p for p in files if not p.endswith("index.md")]
    assert concept_files
    for path in concept_files:
        frontmatter, _ = _split_frontmatter(files[path])
        assert isinstance(frontmatter.get("type"), str) and frontmatter["type"]

    # Bundle-root index declares the OKF version (the only index frontmatter).
    root_frontmatter, _ = _split_frontmatter(files["index.md"])
    assert root_frontmatter["okf_version"] == OKF_VERSION


def test_concept_path_uses_dewey_hierarchy_and_slug() -> None:
    files = build_bundle(
        [
            _source(
                doc_id="doc_a",
                filename="saddle.pdf",
                code="636.1",
                label="Horses & Equines",
                title="Saddle Fit & Groundwork Notes",
            )
        ],
        taxonomy=DeweyTaxonomy(),
    )
    concept_path = next(p for p in files if not p.endswith("index.md"))
    assert concept_path == (
        "600-technology/630-agriculture/636-animal-husbandry/"
        "636-1-horses-equines/saddle-fit-groundwork-notes.md"
    )


def test_frontmatter_maps_fields_and_escapes_safely() -> None:
    files = build_bundle(
        [
            _source(
                doc_id="doc_a",
                filename="weird.pdf",
                code="636.1",
                label="Horses & Equines",
                title='Tricky: "Quoted" Title',
                tags=("a tag", "another"),
                description="One sentence abstract.",
            )
        ],
        taxonomy=DeweyTaxonomy(),
    )
    path = next(p for p in files if not p.endswith("index.md"))
    frontmatter, body = _split_frontmatter(files[path])
    assert frontmatter["type"] == "PDF Document"
    assert frontmatter["title"] == 'Tricky: "Quoted" Title'
    assert frontmatter["description"] == "One sentence abstract."
    assert frontmatter["tags"] == ["a tag", "another"]
    assert frontmatter["dewey_code"] == "636.1"
    assert frontmatter["resource"] == "urn:librarian:doc:doc_a"
    assert frontmatter["timestamp"] == "2026-06-13T14:30:00Z"
    assert "Cleaned body text." in body


def test_description_falls_back_to_first_sentence_of_summary() -> None:
    files = build_bundle(
        [
            _source(
                doc_id="doc_a",
                filename="a.pdf",
                code="636.1",
                label="Horses & Equines",
                title="Notes",
                description=None,
                summary="First sentence here. Second sentence is dropped.",
            )
        ],
        taxonomy=DeweyTaxonomy(),
    )
    path = next(p for p in files if not p.endswith("index.md"))
    frontmatter, _ = _split_frontmatter(files[path])
    assert frontmatter["description"] == "First sentence here."


def test_same_classification_concepts_cross_link() -> None:
    files = build_bundle(
        [
            _source(
                doc_id="doc_a",
                filename="a.pdf",
                code="636.1",
                label="Horses & Equines",
                title="First Horse Doc",
            ),
            _source(
                doc_id="doc_b",
                filename="b.pdf",
                code="636.1",
                label="Horses & Equines",
                title="Second Horse Doc",
            ),
        ],
        taxonomy=DeweyTaxonomy(),
    )
    first = files[
        "600-technology/630-agriculture/636-animal-husbandry/636-1-horses-equines/first-horse-doc.md"
    ]
    assert "## Related" in first
    assert "/636-1-horses-equines/second-horse-doc.md" in first


def test_filename_collisions_are_numbered() -> None:
    files = build_bundle(
        [
            _source(
                doc_id="doc_a",
                filename="a.pdf",
                code="636.1",
                label="Horses & Equines",
                title="Same Title",
            ),
            _source(
                doc_id="doc_b",
                filename="b.pdf",
                code="636.1",
                label="Horses & Equines",
                title="Same Title",
            ),
        ],
        taxonomy=DeweyTaxonomy(),
    )
    concept_paths = {p for p in files if not p.endswith("index.md")}
    assert any(p.endswith("/same-title.md") for p in concept_paths)
    assert any(p.endswith("/same-title-2.md") for p in concept_paths)


def test_indexes_cover_every_directory() -> None:
    files = build_bundle(
        [
            _source(
                doc_id="doc_a",
                filename="a.pdf",
                code="636.1",
                label="Horses & Equines",
                title="Horse Doc",
            )
        ],
        taxonomy=DeweyTaxonomy(),
    )
    # An index.md exists at the root and at every directory level down to the leaf.
    assert "index.md" in files
    assert "600-technology/index.md" in files
    assert (
        "600-technology/630-agriculture/636-animal-husbandry/636-1-horses-equines/index.md" in files
    )
    root = files["index.md"]
    assert "Technology" in root


def test_empty_bundle_still_emits_conformant_root_index() -> None:
    files = build_bundle([], taxonomy=DeweyTaxonomy())
    assert list(files) == ["index.md"]
    root_frontmatter, _ = _split_frontmatter(files["index.md"])
    assert root_frontmatter["okf_version"] == OKF_VERSION
