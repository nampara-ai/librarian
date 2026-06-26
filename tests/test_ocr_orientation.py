"""Tests for OSD-based auto-orientation of rotated images before OCR."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from librarian.ingest import extractors
from librarian.ingest.extractors import ImageOcrExtractor, auto_orient_image


def _osd_output(rotate: int, confidence: float) -> str:
    return (
        "Page number: 0\n"
        f"Orientation in degrees: {(360 - rotate) % 360}\n"
        f"Rotate: {rotate}\n"
        f"Orientation confidence: {confidence}\n"
        "Script: Latin\n"
        "Script confidence: 5.00\n"
    )


def _fake_osd(
    rotate: int, confidence: float
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        out = _osd_output(rotate, confidence)
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    return run


def _landscape_png(tmp_path: Path) -> Path:
    image_module = pytest.importorskip("PIL.Image")
    src = tmp_path / "img.png"
    image_module.new("RGB", (300, 100), "white").save(src)
    return src


def test_auto_orient_rotates_on_confident_osd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_module = pytest.importorskip("PIL.Image")
    src = _landscape_png(tmp_path)
    monkeypatch.setattr(extractors.subprocess, "run", _fake_osd(90, 4.6))

    out = auto_orient_image(
        src, tesseract_path="/usr/bin/tesseract", timeout_seconds=30, output_dir=tmp_path
    )

    assert out != src
    with image_module.open(out) as oriented:
        assert oriented.size == (100, 300)  # 90° rotation swaps the dimensions


def test_auto_orient_skips_low_confidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("PIL.Image")
    src = _landscape_png(tmp_path)
    monkeypatch.setattr(extractors.subprocess, "run", _fake_osd(90, 0.4))

    out = auto_orient_image(
        src, tesseract_path="/usr/bin/tesseract", timeout_seconds=30, output_dir=tmp_path
    )

    assert out == src  # a shaky guess must not flip a possibly-correct image


def test_auto_orient_noop_on_zero_rotation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("PIL.Image")
    src = _landscape_png(tmp_path)
    monkeypatch.setattr(extractors.subprocess, "run", _fake_osd(0, 9.0))

    out = auto_orient_image(
        src, tesseract_path="/usr/bin/tesseract", timeout_seconds=30, output_dir=tmp_path
    )

    assert out == src


def test_auto_orient_graceful_when_osd_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PIL.Image")
    src = _landscape_png(tmp_path)

    def boom(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del cmd, kwargs
        raise OSError("tesseract exploded")

    monkeypatch.setattr(extractors.subprocess, "run", boom)

    out = auto_orient_image(
        src, tesseract_path="/usr/bin/tesseract", timeout_seconds=30, output_dir=tmp_path
    )

    assert out == src  # never worse than the un-oriented pass


# --- real OSD integration --------------------------------------------------


def _osd_functional() -> bool:
    tesseract = shutil.which("tesseract")
    if tesseract is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603
            [tesseract, "--list-langs"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "osd" in (result.stdout + result.stderr)


def _document_image(tmp_path: Path, *, rotate: int):
    image_module = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw, ImageFont

    image = image_module.new("RGB", (900, 420), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 30)
    except OSError:
        font = ImageFont.load_default()
    lines = [
        "The quarterly revenue report shows strong growth",
        "across every region this fiscal year.",
        "Operating margins improved while costs declined,",
        "and the board approved a new dividend policy.",
    ]
    for index, line in enumerate(lines):
        draw.text((20, 20 + index * 70), line, fill="black", font=font)
    path = tmp_path / f"doc_{rotate}.png"
    image.rotate(rotate, expand=True, fillcolor="white").save(path)
    return path


@pytest.mark.skipif(not _osd_functional(), reason="tesseract OSD data not available")
@pytest.mark.asyncio
@pytest.mark.parametrize("rotate", [90, 180, 270])
async def test_rotated_document_ocr_recovered_by_auto_orient(tmp_path: Path, rotate: int) -> None:
    path = _document_image(tmp_path, rotate=rotate)

    oriented = await ImageOcrExtractor(auto_orient=True).extract(path)

    # The upright text is recovered regardless of how the page was rotated.
    assert "revenue" in oriented.lower()
    assert "dividend" in oriented.lower()
