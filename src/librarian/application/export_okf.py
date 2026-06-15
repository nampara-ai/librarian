"""Render processed documents as an Open Knowledge Format (OKF) v0.1 bundle.

OKF spec: https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf

A bundle is a directory of markdown "concept" files, each with a YAML
frontmatter block and a markdown body. Here, concepts are organized into a
Dewey-derived directory hierarchy, cross-linked to same-classification
siblings, and accompanied by generated ``index.md`` files for progressive
disclosure. The spec's only hard requirements are that every non-reserved
``.md`` file has a parseable frontmatter block containing a non-empty ``type``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from librarian.application.ports import TaxonomyProvider
from librarian.domain.ids import DocumentId
from librarian.domain.models import Classification, CleanedOutput, Document

OKF_VERSION = "0.1"

_MAX_RELATED = 12
_MAX_DESCRIPTION_CHARS = 280

# Source extension -> OKF `type` (the "kind of concept", not its subject).
_TYPE_BY_EXTENSION = {
    "pdf": "PDF Document",
    "docx": "Word Document",
    "doc": "Word Document",
    "md": "Markdown Document",
    "markdown": "Markdown Document",
    "txt": "Text Document",
    "srt": "Transcript",
    "vtt": "Transcript",
    "png": "Scanned Image",
    "jpg": "Scanned Image",
    "jpeg": "Scanned Image",
    "tif": "Scanned Image",
    "tiff": "Scanned Image",
}


@dataclass(frozen=True, slots=True)
class OkfSource:
    """One processed document to render into the bundle."""

    document: Document
    output: CleanedOutput
    classification: Classification


class OkfRepository(Protocol):
    """The repository surface needed to assemble a bundle."""

    async def list(self, *, limit: int = ..., offset: int = ...) -> Sequence[Document]: ...

    async def get_cleaned_output(self, document_id: DocumentId) -> CleanedOutput | None: ...

    async def get_classification(self, document_id: DocumentId) -> Classification | None: ...


async def collect_sources(
    repository: OkfRepository,
    *,
    classification_prefix: str | None = None,
    tag: str | None = None,
    series: str | None = None,
    limit: int | None = None,
) -> tuple[list[OkfSource], list[str]]:
    """Gather processed documents (with output + classification) for a bundle.

    Returns the included sources and the ids of documents skipped because they
    have not been processed (no cleaned output or classification yet). Documents
    excluded by the prefix/tag/series filters are not counted as skipped. The
    ``series`` filter matches the stable ``series_key`` exactly or any substring
    of the series key or display name, so a key or a name fragment both work.
    """
    sources: list[OkfSource] = []
    skipped: list[str] = []
    wanted_tag = tag.lower() if tag else None
    wanted_series = series.lower() if series else None
    page = 200
    offset = 0
    while True:
        documents = await repository.list(limit=page, offset=offset)
        if not documents:
            break
        for document in documents:
            output = await repository.get_cleaned_output(document.id)
            classification = await repository.get_classification(document.id)
            if output is None or classification is None:
                skipped.append(str(document.id))
                continue
            if classification_prefix and not classification.code.startswith(classification_prefix):
                continue
            if wanted_tag and wanted_tag not in {t.lower() for t in classification.tags}:
                continue
            if wanted_series and not _series_matches(wanted_series, classification):
                continue
            sources.append(
                OkfSource(document=document, output=output, classification=classification)
            )
            if limit is not None and len(sources) >= limit:
                return sources, skipped
        if len(documents) < page:
            break
        offset += page
    return sources, skipped


def _series_matches(wanted_series: str, classification: Classification) -> bool:
    key = (classification.series_key or "").lower()
    name = (classification.series_title or "").lower()
    if wanted_series == key:
        return True
    return bool((key and wanted_series in key) or (name and wanted_series in name))


@dataclass(frozen=True, slots=True)
class _Concept:
    path: str  # bundle-relative, e.g. "600-technology/636-1-horses-equines/notes.md"
    code: str
    type: str
    title: str
    description: str | None
    resource: str
    tags: tuple[str, ...]
    timestamp: str
    dewey_label: str
    source_filename: str
    confidence: float | None
    issuer: str | None
    series_key: str | None
    series_title: str | None
    period: str | None
    summary: str
    body: str


def build_bundle(sources: list[OkfSource], *, taxonomy: TaxonomyProvider) -> dict[str, str]:
    """Render the sources into a map of bundle-relative path -> file content."""
    concepts: list[_Concept] = []
    by_code: dict[str, list[_Concept]] = defaultdict(list)
    by_series: dict[str, list[_Concept]] = defaultdict(list)
    dir_labels: dict[str, str] = {}
    used_paths: set[str] = set()

    for source in sources:
        concept = _build_concept(source, taxonomy=taxonomy, dir_labels=dir_labels, used=used_paths)
        used_paths.add(concept.path)
        concepts.append(concept)
        by_code[concept.code].append(concept)
        if concept.series_key:
            by_series[concept.series_key].append(concept)

    files: dict[str, str] = {}
    for concept in concepts:
        related = [
            (other.title, "/" + other.path)
            for other in by_code[concept.code]
            if other.path != concept.path
        ][:_MAX_RELATED]
        editions = _series_editions(concept, by_series)
        files[concept.path] = _render_concept(concept, related, editions)

    files.update(_build_indexes(concepts, dir_labels))
    return files


def _series_editions(
    concept: _Concept,
    by_series: dict[str, list[_Concept]],
) -> list[tuple[str, str]]:
    """Other editions of the same recurring series, ordered by reporting period."""
    if not concept.series_key:
        return []
    siblings = [
        other for other in by_series.get(concept.series_key, []) if other.path != concept.path
    ]
    siblings.sort(key=lambda other: (other.period or "", other.title.lower()))
    return [
        (other.period or other.series_title or other.title, "/" + other.path)
        for other in siblings[:_MAX_RELATED]
    ]


def _build_concept(
    source: OkfSource,
    *,
    taxonomy: TaxonomyProvider,
    dir_labels: dict[str, str],
    used: set[str],
) -> _Concept:
    classification = source.classification
    code = classification.code.strip() or "000"
    label = classification.label.strip() or taxonomy.label_for(code) or "General"

    segments: list[str] = []
    for level_code, level_label in _dewey_hierarchy(code, taxonomy):
        # The deepest level is the document's own code; fall back to its
        # classification label when the taxonomy has no label for it.
        effective_label = level_label or (label if level_code == code else None)
        if effective_label:
            segment = f"{_slug(level_code)}-{_slug(effective_label)}"
        else:
            segment = _slug(level_code)
        segments.append(segment)
        dir_labels["/".join(segments)] = effective_label or level_code
    directory = "/".join(segments) or _slug(code)

    stem = classification.title or _strip_extension(source.document.source.filename)
    filename = _unique_filename(directory, _slug(stem, fallback="document"), used)
    path = f"{directory}/{filename}.md"

    description = classification.description or _first_sentence(classification.summary)

    return _Concept(
        path=path,
        code=code,
        type=_concept_type(source.document.source.filename),
        title=stem or "Untitled",
        description=description,
        resource=f"urn:librarian:doc:{source.document.id}",
        tags=tuple(classification.tags),
        timestamp=source.output.created_at.isoformat().replace("+00:00", "Z"),
        dewey_label=label,
        source_filename=source.document.source.filename,
        confidence=classification.confidence,
        issuer=classification.issuer,
        series_key=classification.series_key,
        series_title=classification.series_title,
        period=classification.period,
        summary=classification.summary,
        body=source.output.text,
    )


def _render_concept(
    concept: _Concept,
    related: list[tuple[str, str]],
    editions: list[tuple[str, str]],
) -> str:
    frontmatter = _frontmatter(
        [
            ("type", concept.type),
            ("title", concept.title),
            ("description", concept.description),
            ("resource", concept.resource),
            ("tags", list(concept.tags)),
            ("timestamp", concept.timestamp),
            ("dewey_code", concept.code),
            ("dewey_label", concept.dewey_label),
            ("source_filename", concept.source_filename),
            ("classification_confidence", concept.confidence),
            ("issuer", concept.issuer),
            ("series", concept.series_title),
            ("series_key", concept.series_key),
            ("period", concept.period),
        ]
    )
    parts = [frontmatter, ""]
    summary = " ".join(concept.summary.split())
    if summary:
        parts.extend([f"> {summary}", ""])
    parts.append(concept.body.strip())
    if editions:
        parts.extend(["", "## Series Editions", ""])
        parts.extend(f"- [{label}]({link})" for label, link in editions)
    if related:
        parts.extend(["", "## Related", ""])
        parts.extend(f"- [{title}]({link})" for title, link in related)
    return "\n".join(parts).rstrip() + "\n"


def _build_indexes(concepts: list[_Concept], dir_labels: dict[str, str]) -> dict[str, str]:
    # Map each directory to its immediate child concepts and child subdirectories.
    child_concepts: dict[str, list[_Concept]] = defaultdict(list)
    child_dirs: dict[str, set[str]] = defaultdict(set)
    all_dirs: set[str] = {""}

    for concept in concepts:
        directory = concept.path.rsplit("/", 1)[0]
        child_concepts[directory].append(concept)
        parts = directory.split("/")
        for depth in range(len(parts)):
            current = "/".join(parts[: depth + 1])
            parent = "/".join(parts[:depth])
            all_dirs.add(current)
            child_dirs[parent].add(current)

    files: dict[str, str] = {}
    for directory in all_dirs:
        index_path = f"{directory}/index.md" if directory else "index.md"
        files[index_path] = _render_index(directory, dir_labels, child_concepts, child_dirs)
    return files


def _render_index(
    directory: str,
    dir_labels: dict[str, str],
    child_concepts: dict[str, list[_Concept]],
    child_dirs: dict[str, set[str]],
) -> str:
    lines: list[str] = []
    if directory == "":
        # Only the bundle-root index may carry frontmatter; declare the version.
        lines.extend(["---", f'okf_version: "{OKF_VERSION}"', "---", ""])
        title = "Librarian Knowledge Bundle"
    else:
        title = dir_labels.get(directory, directory.rsplit("/", 1)[-1])
    lines.append(f"# {title}")

    subdirs = sorted(child_dirs.get(directory, set()))
    if subdirs:
        lines.extend(["", "## Sections"])
        for subdir in subdirs:
            label = dir_labels.get(subdir, subdir.rsplit("/", 1)[-1])
            count = _concept_count(subdir, child_concepts, child_dirs)
            lines.append(f"- [{label}](/{subdir}/index.md) — {count} concept(s)")

    concepts = sorted(child_concepts.get(directory, []), key=lambda c: c.title.lower())
    if concepts:
        lines.extend(["", "## Concepts"])
        for concept in concepts:
            suffix = f" — {concept.description}" if concept.description else ""
            lines.append(f"- [{concept.title}](/{concept.path}){suffix}")

    return "\n".join(lines).rstrip() + "\n"


def _concept_count(
    directory: str,
    child_concepts: dict[str, list[_Concept]],
    child_dirs: dict[str, set[str]],
) -> int:
    total = len(child_concepts.get(directory, []))
    for subdir in child_dirs.get(directory, set()):
        total += _concept_count(subdir, child_concepts, child_dirs)
    return total


def _dewey_hierarchy(code: str, taxonomy: TaxonomyProvider) -> list[tuple[str, str | None]]:
    """Derive ancestor codes (class, division, section, full) for a Dewey code."""
    digits = "".join(char for char in code if char.isdigit())
    candidates: list[str] = []
    if digits:
        candidates.append(digits[0] + "00")
        if len(digits) >= 2:
            candidates.append(digits[:2] + "0")
        if len(digits) >= 3:
            candidates.append(digits[:3])
    if code and code not in candidates:
        candidates.append(code)

    hierarchy: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            hierarchy.append((candidate, taxonomy.label_for(candidate)))
    if not hierarchy:
        fallback = code or "000"
        hierarchy.append((fallback, taxonomy.label_for(fallback)))
    return hierarchy


def _concept_type(filename: str) -> str:
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _TYPE_BY_EXTENSION.get(extension, "Document")


def _strip_extension(filename: str) -> str:
    return filename.rsplit(".", 1)[0] if "." in filename else filename


def _first_sentence(text: str) -> str | None:
    collapsed = " ".join(text.split())
    if not collapsed:
        return None
    match = re.search(r".+?[.!?](?:\s|$)", collapsed)
    sentence = (match.group(0) if match else collapsed).strip()
    return sentence[:_MAX_DESCRIPTION_CHARS].strip() or None


def _slug(text: str, *, fallback: str = "untitled") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or fallback


def _unique_filename(directory: str, stem: str, used: set[str]) -> str:
    candidate = stem
    counter = 2
    while f"{directory}/{candidate}.md" in used:
        candidate = f"{stem}-{counter}"
        counter += 1
    return candidate


def _frontmatter(fields: list[tuple[str, object]]) -> str:
    lines = ["---"]
    for key, value in fields:
        if value is None:
            continue
        if isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        elif isinstance(value, (list, tuple)):
            items = cast(Sequence[object], value)
            if not items:
                continue
            rendered = ", ".join(_yaml_quote(str(item)) for item in items)
            lines.append(f"{key}: [{rendered}]")
        else:
            lines.append(f"{key}: {_yaml_quote(str(value))}")
    lines.append("---")
    return "\n".join(lines)


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", " ").replace("\t", " ")
    return f'"{escaped}"'
