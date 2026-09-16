"""Final document fidelity checks and persisted quality-report keys."""

from __future__ import annotations

import re
from collections import Counter

from librarian.domain.ids import DocumentId, RunId
from librarian.pipeline.validation import LOCAL_IMAGE_REFERENCE_REGEX, PAGE_BREAK_REGEX

_EMPTY_FENCE_RE = re.compile(r"(?m)^\s*(`{3,}|~{3,})[^\n]*\n\s*\1\s*$")
_TOC_ENTRY_RE = re.compile(r"(?m)^\s*[-*]\s+(.+?)\s+(?:\d{1,4}|[ivxlcdm]{1,8})\s*$", re.I)
_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+(.+)$")
_INDEX_LETTER_RE = re.compile(r"(?m)^\s*([A-Z])\s*$")


def extraction_report_key(document_id: DocumentId) -> str:
    return f"quality:extraction:{document_id}"


def run_quality_key(run_id: RunId) -> str:
    return f"quality:run:{run_id}"


def audit_final_document(source: str, output: str, *, title: str | None) -> dict[str, object]:
    """Check global invariants after chunk-level fidelity validation.

    Chunk validation catches local omissions. This catches cross-chunk losses,
    missing pages and assets, invalid final Markdown, and title mismatches.
    Structural checks are advisory because scanned PDFs can lack headings.
    """
    source_images = Counter(LOCAL_IMAGE_REFERENCE_REGEX.findall(source))
    output_images = Counter(LOCAL_IMAGE_REFERENCE_REGEX.findall(output))
    source_boundaries = len(PAGE_BREAK_REGEX.findall(source))
    output_boundaries = len(PAGE_BREAK_REGEX.findall(output))
    fatal: list[str] = []
    warnings: list[str] = []
    if source_images != output_images:
        fatal.append("image-references-changed")
    if source_boundaries != output_boundaries:
        fatal.append("page-boundaries-changed")
    if _EMPTY_FENCE_RE.search(output):
        warnings.append("empty-code-fence")
    giant_rows = sum(
        len(line) > 240 for line in output.splitlines() if line.lstrip().startswith("|")
    )
    if giant_rows:
        warnings.append("oversized-table-rows")
    source_headings = _HEADING_RE.findall(source)
    output_headings = _HEADING_RE.findall(output)
    if source_headings and not output_headings:
        warnings.append("missing-headings")
    first_title = source_headings[0].strip() if source_headings else None
    if title and first_title and _canonical(title) != _canonical(first_title):
        warnings.append("title-differs-from-source-heading")
    toc_entries = _TOC_ENTRY_RE.findall(output[: min(len(output), 75_000)])
    unmatched_toc = unmatched_toc_entries(output)
    if toc_entries and len(unmatched_toc) / len(toc_entries) >= 0.01:
        warnings.append("toc-entries-unmatched")
    # Index letter headings are unambiguous; an out-of-order letter is a
    # useful warning, while books without this format remain unscored.
    index_headings = list(re.finditer(r"(?im)^#{0,3}[ \t]*index[ \t]*$", output))
    index_body = output[index_headings[-1].end() :] if index_headings else ""
    index_letters = _INDEX_LETTER_RE.findall(index_body)
    index_order_ok = (
        all(a <= b for a, b in zip(index_letters, index_letters[1:], strict=False))
        if len(index_letters) >= 4
        else None
    )
    if index_order_ok is False:
        warnings.append("index-letter-order")
    pages = output_boundaries + 1 if source_boundaries else None
    return {
        "pages": pages,
        "image_references": sum(output_images.values()),
        "page_boundaries": output_boundaries,
        "headings": len(output_headings),
        "toc_entries": len(toc_entries),
        "toc_entries_without_matching_heading_or_body": len(unmatched_toc),
        "toc_unmatched_entries": unmatched_toc,
        "index_letter_order_ok": index_order_ok,
        "oversized_table_rows": giant_rows,
        "fatal": fatal,
        "warnings": warnings,
    }


def unmatched_toc_entries(output: str) -> list[str]:
    """Return outline titles absent from headings and body for review in the app."""
    entries = _TOC_ENTRY_RE.findall(output[: min(len(output), 75_000)])
    heading_text = _canonical(" ".join(_HEADING_RE.findall(output)))
    # Search the entire document while excluding the outline rows themselves;
    # a fixed cutoff can miss early chapters in books with long figure lists.
    body_text = _canonical(_TOC_ENTRY_RE.sub("", output))
    return [
        entry
        for entry in entries
        if len(_canonical(entry).split()) >= 3
        and _canonical(entry) not in heading_text
        and _canonical(entry) not in body_text
    ]


def _canonical(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))
