"""PDFium text-layer extraction, the retention guard, and OCR failure isolation."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import cast

import pytest

from librarian.ingest import extractors
from librarian.ingest.extractors import (
    CompositeExtractor,
    FallbackExtractor,
    OcrTextResult,
    PdfExtractor,
    PdfPageExtraction,
    measure_retention,
    pdfium_page_texts,
    render_pdf_pages_with_breaks,
    strip_recurring_page_numerals,
    text_layer_is_usable,
    text_retention,
)

PAGE_BREAK = "\n\n<!-- page-break -->\n\n"
PROSE = "The quick brown fox jumps over the lazy dog near the river bank today."


def _pdf_bytes(pages: list[tuple[list[str], bool]]) -> bytes:
    """Build a PDF with Helvetica text lines and, optionally, a raster image per page."""
    objects: list[bytes | None] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        None,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceGray "
        b"/BitsPerComponent 8 /Length 4 >>\nstream\n\x00\xff\xff\x00\nendstream",
    ]
    kids: list[int] = []
    for lines, image in pages:
        content = b""
        y = 350
        for line in lines:
            content += b"BT /F1 12 Tf 50 %d Td (%s) Tj ET\n" % (y, line.encode("latin-1"))
            y -= 16
        if image:
            content += b"q 300 0 0 200 150 100 cm /Im1 Do Q\n"
        objects.append(b"<< /Length %d >>\nstream\n%sendstream" % (len(content), content))
        content_ref = len(objects)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 400] /Resources << /Font "
            b"<< /F1 3 0 R >> /XObject << /Im1 4 0 R >> >> /Contents %d 0 R >>" % content_ref
        )
        kids.append(len(objects))
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % kid for kid in kids),
        len(kids),
    )
    pdf = b"%PDF-1.4\n"
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        assert obj is not None
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n%s\nendobj\n" % (index, obj)
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        pdf += b"%010d 00000 n \n" % offset
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return pdf


def _mixed_pdf(tmp_path: Path) -> Path:
    """Page 1: prose. Page 2: image only (a scan). Page 3: image plus a stray '3'."""
    path = tmp_path / "mixed.pdf"
    path.write_bytes(_pdf_bytes([([PROSE] * 4, False), ([], True), (["3"], True)]))
    return path


# --- metric -----------------------------------------------------------------


def test_text_retention_is_recall_over_word_tokens() -> None:
    reference = "Alpha beta gamma delta epsilon zeta eta theta"
    assert text_retention(reference, reference) == 1.0
    assert (
        text_retention("# Alpha\n\n| beta | gamma |\n\ndelta epsilon zeta eta theta", reference)
        == 1.0
    )
    assert text_retention("theta eta zeta epsilon delta gamma beta alpha", reference) == 1.0
    assert text_retention("Alpha beta", reference) == pytest.approx(0.25)
    assert text_retention("", "") == 1.0


def test_text_layer_usability_rejects_scan_noise() -> None:
    assert text_layer_is_usable(PROSE)
    assert not text_layer_is_usable("")
    assert not text_layer_is_usable("12")
    assert not text_layer_is_usable("|" * 80)
    assert not text_layer_is_usable("aaaa" * 40)


def test_measure_retention_returns_none_for_pure_scans() -> None:
    assert measure_retention("anything", ["", "  ", "3"], threshold=0.8) is None


def test_measure_retention_replaces_only_the_gutted_pages() -> None:
    reference = [f"Page {n} " + PROSE * 4 for n in range(1, 7)]
    blocks = list(reference)
    blocks[2] = "profitable gold mine start are your . if in this"  # liteparse-style fragments
    blocks[2] += "\n\n![](img_p3_0.png)"
    verdict = measure_retention(PAGE_BREAK.join(blocks), reference, threshold=0.8)

    assert verdict is not None
    assert verdict.fallback is False
    assert verdict.failed_pages == (3,)
    assert verdict.replaced_pages == (3,)
    assert verdict.measured_pages == 6
    assert verdict.text is not None
    repaired = verdict.text.split(PAGE_BREAK)
    assert repaired[2].startswith(reference[2])
    assert "![](img_p3_0.png)" in repaired[2]  # figure assets stay referenced
    assert repaired[0] == blocks[0]
    report = verdict.as_dict()
    assert report["pages_below_threshold"] == [3]
    assert cast(float, report["document"]) < 1.0


def test_measure_retention_requests_full_fallback_when_most_pages_fail() -> None:
    reference = [f"Page {n} " + PROSE * 4 for n in range(1, 7)]
    blocks = [block if n % 2 else "a few words" for n, block in enumerate(reference, start=1)]
    verdict = measure_retention(PAGE_BREAK.join(blocks), reference, threshold=0.8)

    assert verdict is not None
    assert verdict.fallback is True
    assert verdict.failed_pages == (2, 4, 6)
    assert verdict.text is None


def test_measure_retention_scores_whole_document_without_page_blocks() -> None:
    reference = [PROSE * 4, PROSE * 4]
    good = measure_retention(PROSE * 8, reference, threshold=0.8)
    bad = measure_retention(PROSE, reference, threshold=0.8)

    assert good is not None and good.fallback is False and good.page_retention == ()
    assert bad is not None and bad.fallback is True


# --- guard -------------------------------------------------------------------


class _StubExtractor:
    supported_extensions = frozenset({".pdf"})

    def __init__(self, text: str, *, engine: str) -> None:
        self.text = text
        self.calls = 0
        self.last_metadata: dict[str, object] | None = {"engine": engine}

    async def extract(self, path: Path) -> str:
        del path
        self.calls += 1
        return self.text


def _reference_pages() -> list[str]:
    return [f"Page {n} " + PROSE * 4 for n in range(1, 4)]


def test_guard_falls_back_when_primary_returns_a_third_of_the_text(tmp_path: Path) -> None:
    reference = _reference_pages()
    primary = _StubExtractor(
        PAGE_BREAK.join(" ".join(p.split()[:12]) for p in reference), engine="liteparse"
    )
    fallback = _StubExtractor(PAGE_BREAK.join(reference), engine="pdfium")
    guard = FallbackExtractor(
        primary,
        fallback,
        supported_extensions=frozenset({".pdf"}),
        reference_texts=lambda _path: reference,
    )

    text = asyncio.run(guard.extract(tmp_path / "x.pdf"))

    assert text == fallback.text
    assert fallback.calls == 1
    metadata = guard.last_metadata
    assert metadata is not None
    assert metadata["engine"] == "pdfium"
    assert metadata["fallback_from_primary"] is True
    assert metadata["fallback_reason"] == "low-retention"
    retention = cast(dict[str, object], metadata["primary_retention"])
    assert retention["fallback"] is True
    assert retention["pages_below_threshold"] == [1, 2, 3]


def test_guard_is_silent_on_a_faithful_primary(tmp_path: Path) -> None:
    reference = _reference_pages()
    markdown = PAGE_BREAK.join(f"## Heading\n\n{page}" for page in reference)
    primary = _StubExtractor(markdown, engine="liteparse")
    fallback = _StubExtractor("never", engine="pdfium")
    guard = FallbackExtractor(
        primary,
        fallback,
        supported_extensions=frozenset({".pdf"}),
        reference_texts=lambda _path: reference,
    )

    text = asyncio.run(guard.extract(tmp_path / "x.pdf"))

    assert text == markdown
    assert fallback.calls == 0
    metadata = guard.last_metadata
    assert metadata is not None
    assert metadata["engine"] == "liteparse"
    assert "fallback_from_primary" not in metadata
    retention = cast(dict[str, object], metadata["extraction_retention"])
    assert retention["document"] == 1.0
    assert retention["pages_below_threshold"] == []


def test_guard_splices_a_single_bad_page_from_the_text_layer(tmp_path: Path) -> None:
    reference = _reference_pages()
    blocks = [
        f"## Heading\n\n{reference[0]}",
        "scrambled handful words",
        f"## Heading\n\n{reference[2]}",
    ]
    primary = _StubExtractor(PAGE_BREAK.join(blocks), engine="liteparse")
    fallback = _StubExtractor("never", engine="pdfium")
    guard = FallbackExtractor(
        primary,
        fallback,
        supported_extensions=frozenset({".pdf"}),
        reference_texts=lambda _path: reference,
    )

    text = asyncio.run(guard.extract(tmp_path / "x.pdf"))

    assert fallback.calls == 0
    assert text.split(PAGE_BREAK) == [blocks[0], reference[1], blocks[2]]
    metadata = guard.last_metadata
    assert metadata is not None
    retention = cast(dict[str, object], metadata["extraction_retention"])
    assert retention["replaced_pages"] == [2]


def test_guard_never_breaks_extraction_when_the_reference_fails(tmp_path: Path) -> None:
    primary = _StubExtractor("primary text", engine="liteparse")
    fallback = _StubExtractor("never", engine="pdfium")

    def broken(_path: Path) -> list[str]:
        raise RuntimeError("corrupt xref")

    guard = FallbackExtractor(
        primary, fallback, supported_extensions=frozenset({".pdf"}), reference_texts=broken
    )

    assert asyncio.run(guard.extract(tmp_path / "x.pdf")) == "primary text"
    assert guard.last_metadata == {"engine": "liteparse"}


def test_guard_ignores_non_pdf_inputs(tmp_path: Path) -> None:
    primary = _StubExtractor("ocr words", engine="liteparse")
    fallback = _StubExtractor("never", engine="tesseract")
    guard = FallbackExtractor(
        primary,
        fallback,
        supported_extensions=frozenset({".png"}),
        reference_texts=lambda _path: ["a very long reference " * 40],
    )

    assert asyncio.run(guard.extract(tmp_path / "scan.png")) == "ocr words"
    assert fallback.calls == 0


# --- PDFium text layer in the built-in extractor ---------------------------


def test_pdfium_reads_every_page_and_marks_image_pages(tmp_path: Path) -> None:
    path = _mixed_pdf(tmp_path)

    texts = pdfium_page_texts(path)

    assert len(texts) == 3
    assert texts[0].count(PROSE) == 4
    assert texts[1] == ""
    assert texts[2].strip() == "3"


@pytest.mark.asyncio
async def test_pdfium_extractor_isolates_ocr_failures_and_keeps_text_layers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _mixed_pdf(tmp_path)

    def failing_ocr(*args: object, **kwargs: object) -> OcrTextResult:
        del args, kwargs
        raise RuntimeError("tesseract is not installed")

    monkeypatch.setattr("librarian.ingest.extractors._ocr_pdf_page", failing_ocr)
    extractor = PdfExtractor(
        ocr_correction_mode="never", text_layer_engine="pdfium", page_markup="breaks"
    )

    text = await extractor.extract(path)

    blocks = text.split(PAGE_BREAK)
    assert len(blocks) == 3
    assert blocks[0].count(PROSE) == 4
    assert blocks[1] == "<!-- page 2: OCR failed: tesseract is not installed -->"
    assert blocks[2].strip() == "3"  # the sparse text layer survives a failed OCR
    metadata = extractor.last_metadata
    assert metadata is not None
    assert metadata["engine"] == "pdfium"
    pages = cast(list[dict[str, object]], metadata["pages"])
    assert [page["source"] for page in pages] == ["embedded", "ocr", "embedded"]
    assert pages[0]["warnings"] == []
    assert pages[1]["warnings"] == ["ocr-page-failed"]
    assert pages[2]["warnings"] == ["ocr-page-failed"]
    summary = cast(dict[str, object], metadata["quality_summary"])
    assert summary["pages"] == 3
    assert summary["ocr_failed_pages"] == 2
    assert summary["pages_with_warnings"] == 2


@pytest.mark.asyncio
async def test_pdfium_extractor_ocrs_only_image_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _mixed_pdf(tmp_path)
    ocr_pages: list[int] = []

    def fake_ocr(*args: object, **kwargs: object) -> OcrTextResult:
        del args
        number = cast(int, kwargs["page_number"])
        ocr_pages.append(number)
        return OcrTextResult(text=f"Scanned words recovered from page {number}.", confidence=91.0)

    monkeypatch.setattr("librarian.ingest.extractors._ocr_pdf_page", fake_ocr)
    extractor = PdfExtractor(
        ocr_correction_mode="never", text_layer_engine="pdfium", page_markup="breaks"
    )

    text = await extractor.extract(path)

    assert sorted(ocr_pages) == [2, 3]  # the prose page is never sent to OCR
    blocks = text.strip().split(PAGE_BREAK)
    assert blocks[1] == "Scanned words recovered from page 2."
    assert blocks[2] == "Scanned words recovered from page 3."
    metadata = extractor.last_metadata
    assert metadata is not None
    pages = cast(list[dict[str, object]], metadata["pages"])
    assert [page["source"] for page in pages] == ["embedded", "ocr", "ocr"]


@pytest.mark.asyncio
async def test_pdfium_extractor_keeps_a_text_layer_longer_than_its_ocr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sparse.pdf"
    path.write_bytes(_pdf_bytes([(["Figure 7: Revenue by region"], True)]))

    def fake_ocr(*args: object, **kwargs: object) -> OcrTextResult:
        del args, kwargs
        return OcrTextResult(text="Fig 7", confidence=40.0)

    monkeypatch.setattr("librarian.ingest.extractors._ocr_pdf_page", fake_ocr)
    extractor = PdfExtractor(
        ocr_correction_mode="never", text_layer_engine="pdfium", page_markup="breaks"
    )

    text = await extractor.extract(path)

    assert text.strip() == "Figure 7: Revenue by region"
    metadata = extractor.last_metadata
    assert metadata is not None
    pages = cast(list[dict[str, object]], metadata["pages"])
    assert pages[0]["source"] == "embedded"
    assert pages[0]["warnings"] == ["ocr-shorter-than-text-layer"]


@pytest.mark.asyncio
async def test_strict_mode_still_aborts_on_a_failed_ocr_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _mixed_pdf(tmp_path)

    def failing_ocr(*args: object, **kwargs: object) -> OcrTextResult:
        del args, kwargs
        raise RuntimeError("no tesseract")

    monkeypatch.setattr("librarian.ingest.extractors._ocr_pdf_page", failing_ocr)
    extractor = PdfExtractor(
        ocr_correction_mode="never",
        text_layer_engine="pdfium",
        page_markup="breaks",
        ocr_fail_on_page_error=True,
    )

    with pytest.raises(RuntimeError, match="Unable to OCR scanned PDF page"):
        await extractor.extract(path)


def test_sync_extraction_matches_the_async_isolation_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _mixed_pdf(tmp_path)

    def failing_ocr(*args: object, **kwargs: object) -> OcrTextResult:
        del args, kwargs
        raise RuntimeError("no tesseract")

    monkeypatch.setattr("librarian.ingest.extractors._ocr_pdf_page", failing_ocr)
    extractor = PdfExtractor(
        ocr_correction_mode="never", text_layer_engine="pdfium", page_markup="breaks"
    )

    text = extractor._extract_sync(path)  # pyright: ignore[reportPrivateUsage]

    blocks = text.split(PAGE_BREAK)
    assert blocks[0].count(PROSE) == 4
    assert blocks[1].startswith("<!-- page 2: OCR failed")
    assert blocks[2].strip() == "3"


def test_page_break_rendering_marks_failed_pages_explicitly() -> None:
    pages = [
        PdfPageExtraction(page_number=2, text="", source="ocr", error="boom"),
        PdfPageExtraction(page_number=1, text="  first  ", source="embedded"),
    ]

    assert render_pdf_pages_with_breaks(pages) == (
        "first\n\n<!-- page-break -->\n\n<!-- page 2: OCR failed: boom -->\n"
    )


def test_pdfium_extractor_falls_back_to_pdfplumber_without_pypdfium2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _mixed_pdf(tmp_path)

    def no_pdfium(_path: Path) -> list[str]:
        raise ImportError("pypdfium2 missing")

    monkeypatch.setattr(extractors, "pdfium_page_texts", no_pdfium)
    extractor = PdfExtractor(ocr_correction_mode="never", text_layer_engine="pdfium")

    pages, count = extractor._extract_embedded_pages(path)  # pyright: ignore[reportPrivateUsage]

    assert count == 3
    assert pages[0].source == "embedded" and PROSE in pages[0].text
    assert extractor.last_metadata is None  # nothing extracted yet
    assert extractor._resolved_text_layer_engine == "pdfplumber"  # pyright: ignore[reportPrivateUsage]


# --- composite wiring --------------------------------------------------------


def test_default_composite_extracts_pdf_text_layer_without_liteparse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _mixed_pdf(tmp_path)

    def failing_ocr(*args: object, **kwargs: object) -> OcrTextResult:
        del args, kwargs
        raise RuntimeError("no tesseract")

    monkeypatch.setattr("librarian.ingest.extractors._ocr_pdf_page", failing_ocr)
    composite = CompositeExtractor(ocr_correction_mode="never")

    text = asyncio.run(composite.extract(path))

    assert composite.resolved_pdf_engine == "pdfium"
    assert text.count(PROSE) == 4
    assert "<!-- page-break -->" in text
    assert composite.last_metadata is not None
    assert composite.last_metadata["engine"] == "pdfium"


def test_legacy_composite_keeps_page_headings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _mixed_pdf(tmp_path)

    def failing_ocr(*args: object, **kwargs: object) -> OcrTextResult:
        del args, kwargs
        raise RuntimeError("no tesseract")

    monkeypatch.setattr("librarian.ingest.extractors._ocr_pdf_page", failing_ocr)
    composite = CompositeExtractor(pdf_engine="legacy", ocr_correction_mode="never")

    text = asyncio.run(composite.extract(path))

    assert composite.resolved_pdf_engine == "legacy"
    assert "#### Page 1" in text
    assert "status: failed" in text
    assert composite.last_metadata is not None
    assert composite.last_metadata["engine"] == "pdfplumber"


def test_engine_and_enhancer_changes_invalidate_the_extraction_cache() -> None:
    signatures = {
        CompositeExtractor(pdf_engine="auto").config_signature,
        CompositeExtractor(pdf_engine="pdfium").config_signature,
        CompositeExtractor(pdf_engine="legacy").config_signature,
    }
    assert len(signatures) == 3
    # "auto" and "pdfium" differ only in name until an enhancer is requested.
    assert (
        CompositeExtractor(pdf_engine="auto").pdf_text_layer_engine
        == CompositeExtractor(pdf_engine="pdfium").pdf_text_layer_engine
        == "pdfium"
    )


# --- tesseract invocation and page furniture -------------------------------


def test_tesseract_outputs_are_requested_with_parameters_not_config_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``txt``/``tsv`` config names need files under TESSDATA_PREFIX/configs.

    The Mac bundle ships only the language models, so tesseract silently
    dropped the tsv output and the page failed with a missing-file error.
    """
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        seen.append(cmd)
        base = Path(cmd[2])
        # Behave like tesseract without configs: honour -c parameters only.
        for fmt in ("txt", "tsv"):
            if f"tessedit_create_{fmt}=1" in cmd:
                base.with_suffix(f".{fmt}").write_text(f"{fmt} output", encoding="utf-8")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(extractors.subprocess, "run", fake_run)

    outputs = extractors._run_tesseract_to_files(  # pyright: ignore[reportPrivateUsage]
        tmp_path / "page.png",
        tesseract_path="/usr/bin/tesseract",
        language="eng",
        timeout_seconds=5,
        output_dir=tmp_path,
        formats=("txt", "tsv"),
    )

    assert outputs == {"txt": "txt output", "tsv": "tsv output"}
    command = seen[0]
    assert "txt" not in command and "tsv" not in command
    assert command.count("-c") == 2


def test_missing_tesseract_output_is_reported_with_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        Path(cmd[2]).with_suffix(".txt").write_text("words", encoding="utf-8")
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr="read_params_file: Can't open tsv\n"
        )

    monkeypatch.setattr(extractors.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match=r"no \.tsv output.*Can't open tsv"):
        extractors._run_tesseract_to_files(  # pyright: ignore[reportPrivateUsage]
            tmp_path / "page.png",
            tesseract_path="/usr/bin/tesseract",
            language="eng",
            timeout_seconds=5,
            output_dir=tmp_path,
            formats=("txt", "tsv"),
        )


def _page(number: int, text: str) -> PdfPageExtraction:
    return PdfPageExtraction(page_number=number, text=text, source="embedded")


def test_recurring_page_numerals_are_stripped_from_page_edges() -> None:
    pages = [
        _page(1, "Make it Rain\nChris Orzechowski"),
        _page(2, "1  \nIntroduction  \nIn my work as a copywriter I learned things."),
        _page(3, "2  \nA while back I was doing launches."),
        _page(4, "3  \nIs Email Taking its Last Gasping Breath?"),
        _page(5, "Some prose that ends with a folio.\n\n4"),
        _page(
            6,
            "",
        ),
    ]

    stripped, removed = strip_recurring_page_numerals(pages)

    assert removed == 3
    assert stripped[0].text == "Make it Rain\nChris Orzechowski"
    assert stripped[1].text == "Introduction  \nIn my work as a copywriter I learned things."
    assert stripped[2].text == "A while back I was doing launches."
    assert stripped[3].text == "Is Email Taking its Last Gasping Breath?"
    # Only one page carries a trailing numeral: not recurring, so it stays.
    assert stripped[4].text == pages[4].text
    assert stripped[5].text == ""


def test_isolated_leading_numbers_are_not_page_furniture() -> None:
    pages = [
        _page(1, "7\nSeven habits open this chapter."),
        _page(2, "Plain prose page two."),
        _page(3, "Plain prose page three."),
        _page(4, "Plain prose page four."),
    ]

    stripped, removed = strip_recurring_page_numerals(pages)

    assert removed == 0
    assert [page.text for page in stripped] == [page.text for page in pages]


@pytest.mark.asyncio
async def test_pdfium_extractor_removes_printed_folios(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "folios.pdf"
    path.write_bytes(_pdf_bytes([([str(n), PROSE, PROSE], False) for n in range(1, 5)]))

    def no_ocr(*args: object, **kwargs: object) -> OcrTextResult:
        del args, kwargs
        raise AssertionError("text pages must not be sent to OCR")

    monkeypatch.setattr("librarian.ingest.extractors._ocr_pdf_page", no_ocr)
    extractor = PdfExtractor(
        ocr_correction_mode="never", text_layer_engine="pdfium", page_markup="breaks"
    )

    text = await extractor.extract(path)

    blocks = text.strip().split(PAGE_BREAK)
    assert all(block.startswith(PROSE) for block in blocks)
    assert extractor.last_metadata is not None
    assert extractor.last_metadata["page_numerals_removed"] == 4
