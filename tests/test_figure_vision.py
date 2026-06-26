"""Tests for the vision-LLM figure/chart enrichment pass."""

from __future__ import annotations

import asyncio
import base64
import zlib
from pathlib import Path

import pytest

from librarian.config import Settings
from librarian.ingest.extractors import (
    CompositeExtractor,
    FigureImage,
    LiteParseExtractor,
    enrich_markdown_figures,
    figure_media_type,
    liteparse_available,
)
from librarian.llm.lazy import LazyLLMProvider
from librarian.llm.mock import MockLLMProvider

requires_liteparse = pytest.mark.skipif(
    not liteparse_available(), reason="liteparse extra not installed"
)


class _FakeVision:
    """Vision provider double that echoes image size and can fail chosen images."""

    name = "fake-vision"

    def __init__(self, *, fail_on: bytes | None = None) -> None:
        self.fail_on = fail_on
        self.calls = 0

    async def describe_image(
        self,
        *,
        image_base64: str,
        media_type: str,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        del media_type, system_prompt, user_prompt, model, max_tokens, temperature
        self.calls += 1
        data = base64.b64decode(image_base64)
        if self.fail_on is not None and data == self.fail_on:
            raise RuntimeError("vision failed")
        return f"described {len(data)} bytes"


def _fig(fig_id: str, page: int, size: int) -> FigureImage:
    # Distinct bytes per id (so fail_on can target one figure), exact length size.
    data = (fig_id.encode("ascii") + bytes(size))[:size]
    return FigureImage(id=fig_id, page=page, media_type="image/png", data=data)


def _enrich(markdown: str, figures: list[FigureImage], provider: _FakeVision, **kw: object):
    params = {
        "model": "m",
        "max_figures": 20,
        "min_bytes": 2048,
        "max_bytes": 10_000_000,
        "max_concurrency": 2,
        "max_response_chars": 16_000,
        **kw,
    }
    return asyncio.run(enrich_markdown_figures(markdown, figures, provider=provider, **params))  # type: ignore[arg-type]


def test_enrich_replaces_placeholders_with_descriptions() -> None:
    md = "intro\n\n![](image_p1_0.png)\n\nmore\n\n![](image_p2_0.png)\n"
    figures = [_fig("p1_0", 1, 5000), _fig("p2_0", 2, 4000)]

    out, count = _enrich(md, figures, _FakeVision())

    assert count == 2
    assert "![](image_p1_0.png)\n\n**Figure (page 1):** described 5000 bytes" in out
    assert "**Figure (page 2):** described 4000 bytes" in out


def test_enrich_respects_max_figures() -> None:
    md = "![](image_a.png)\n![](image_b.png)\n![](image_c.png)\n"
    figures = [_fig("a", 1, 5000), _fig("b", 1, 5000), _fig("c", 1, 5000)]

    out, count = _enrich(md, figures, _FakeVision(), max_figures=2)

    assert count == 2
    assert "image_c.png)\n" in out  # third placeholder left untouched
    assert out.count("**Figure") == 2


def test_enrich_skips_tiny_and_huge_images() -> None:
    md = "![](image_tiny.png)\n![](image_ok.png)\n![](image_huge.png)\n"
    figures = [_fig("tiny", 1, 100), _fig("ok", 1, 5000), _fig("huge", 1, 9000)]

    out, count = _enrich(md, figures, _FakeVision(), min_bytes=2048, max_bytes=8000)

    assert count == 1
    assert "**Figure (page 1):** described 5000 bytes" in out


def test_enrich_skips_placeholders_absent_from_markdown() -> None:
    md = "only one figure here\n\n![](image_present.png)\n"
    figures = [_fig("present", 1, 5000), _fig("absent", 1, 5000)]

    provider = _FakeVision()
    _out, count = _enrich(md, figures, provider)

    assert count == 1
    assert provider.calls == 1  # the absent figure was never sent to the model


def test_enrich_swallows_per_figure_failure() -> None:
    md = "![](image_good.png)\n![](image_bad.png)\n"
    good = _fig("good", 1, 5000)
    bad = _fig("bad", 2, 5000)

    out, count = _enrich(md, [good, bad], _FakeVision(fail_on=bad.data))

    assert count == 1
    assert "**Figure (page 1):** described 5000 bytes" in out
    assert "![](image_bad.png)\n" in out  # failed figure placeholder left as-is
    assert "**Figure (page 2)" not in out


def test_enrich_dedupes_duplicate_placeholders() -> None:
    # Two figures resolving to the same placeholder must not both inject (the
    # second replace(count=1) would otherwise land inside the first's block).
    md = "![](image_dup.png)\n"
    figures = [_fig("dup", 1, 5000), _fig("dup", 2, 5000)]
    provider = _FakeVision()

    out, count = _enrich(md, figures, provider)

    assert count == 1
    assert provider.calls == 1
    assert out.count("**Figure") == 1


def test_figure_media_type_mapping() -> None:
    assert figure_media_type("png") == "image/png"
    assert figure_media_type("JPG") == "image/jpeg"
    assert figure_media_type(".webp") == "image/webp"
    assert figure_media_type("tiff") == "image/png"  # unknown -> default png


def test_figure_image_placeholder_property() -> None:
    assert _fig("p3_1", 3, 10).placeholder == "![](image_p3_1.png)"


@pytest.mark.asyncio
async def test_mock_provider_describe_image() -> None:
    out = await MockLLMProvider().describe_image(
        image_base64="QUJD",
        media_type="image/png",
        system_prompt="s",
        user_prompt="u",
        model="m",
        max_tokens=64,
        temperature=0.0,
    )
    assert "mock description" in out


@pytest.mark.asyncio
async def test_lazy_provider_delegates_describe_image_to_mock() -> None:
    provider = LazyLLMProvider(Settings(llm_provider="mock"))
    out = await provider.describe_image(
        image_base64="QUJD",
        media_type="image/png",
        system_prompt="s",
        user_prompt="u",
        model="m",
        max_tokens=64,
        temperature=0.0,
    )
    assert "mock description" in out


# --- liteparse integration -------------------------------------------------


def _embedded_image_pdf() -> bytes:
    raw = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 0])  # 2x2 RGB
    img = zlib.compress(raw)

    def obj(n: int, body: bytes) -> bytes:
        return b"%d 0 obj\n%s\nendobj\n" % (n, body)

    content = b"q 100 0 0 100 50 50 cm /Im0 Do Q\nBT /F1 10 Tf 20 170 Td (Figure 1: sales) Tj ET"
    objs = [
        obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
        obj(
            3,
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            b"/Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>",
        ),
        obj(4, b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content)),
        obj(
            5,
            b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceRGB "
            b"/BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n%s\nendstream"
            % (len(img), img),
        ),
    ]
    pdf = b"%PDF-1.4\n"
    offsets: list[int] = []
    for body in objs:
        offsets.append(len(pdf))
        pdf += body
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        pdf += b"%010d 00000 n \n" % off
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1,
        xref,
    )
    return pdf


@requires_liteparse
def test_liteparse_extractor_enriches_embedded_figure(tmp_path: Path) -> None:
    source = tmp_path / "fig.pdf"
    source.write_bytes(_embedded_image_pdf())
    provider = _FakeVision()
    extractor = LiteParseExtractor(vision_provider=provider, vision_min_bytes=0)

    markdown = asyncio.run(extractor.extract(source))

    assert provider.calls == 1
    assert "**Figure (page 1):** described" in markdown
    assert extractor.last_metadata is not None
    assert extractor.last_metadata["figures_described"] == 1


@requires_liteparse
def test_liteparse_extractor_without_vision_leaves_placeholder(tmp_path: Path) -> None:
    source = tmp_path / "fig.pdf"
    source.write_bytes(_embedded_image_pdf())
    extractor = LiteParseExtractor()  # no vision provider

    markdown = asyncio.run(extractor.extract(source))

    assert "**Figure (page" not in markdown
    assert "![](image_" in markdown  # placeholder retained, unenriched


@requires_liteparse
def test_composite_vision_active_and_signature_changes() -> None:
    plain = CompositeExtractor()
    with_vision = CompositeExtractor(figure_vision_provider=_FakeVision())

    assert plain.figure_vision_active is False
    assert with_vision.figure_vision_active is True
    assert plain.config_signature != with_vision.config_signature


@requires_liteparse
def test_composite_signature_tracks_vision_size_gates() -> None:
    # Changing which figures get described (min/max bytes) or how much text each
    # yields (response cap) must invalidate the extraction cache.
    base = CompositeExtractor(figure_vision_provider=_FakeVision())
    min_changed = CompositeExtractor(
        figure_vision_provider=_FakeVision(), figure_vision_min_bytes=999
    )
    max_changed = CompositeExtractor(
        figure_vision_provider=_FakeVision(), figure_vision_max_bytes=123
    )
    resp_changed = CompositeExtractor(
        figure_vision_provider=_FakeVision(), figure_vision_max_response_chars=512
    )

    assert base.config_signature != min_changed.config_signature
    assert base.config_signature != max_changed.config_signature
    assert base.config_signature != resp_changed.config_signature
