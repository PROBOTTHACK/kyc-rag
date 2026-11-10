"""Hash-verified page rendering for multimodal form extraction (M2 Pipeline B).

The PDF is re-hashed before any page is rendered: bytes that differ from the expected
SHA-256 (manifest and `documents.sha256`) are refused, so a model only ever sees the
exact approved file. Rendering is deterministic for a given PyMuPDF version and DPI.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from app.ingestion.manifest import sha256_file
from app.llm.multimodal import PageImage

RENDER_DPI = 150


class RenderRefused(ValueError):
    """The file is missing, its hash differs, or a requested page does not exist."""


@dataclass(frozen=True)
class RenderedPage:
    image: PageImage
    png: bytes


def render_pages(
    pdf_path: Path,
    *,
    document_key: str,
    expected_sha256: str,
    page_numbers: list[int] | None = None,
    dpi: int = RENDER_DPI,
) -> list[RenderedPage]:
    """Render 1-based `page_numbers` (default: all) to PNG after verifying the hash."""
    if not pdf_path.is_file():
        raise RenderRefused(f"{document_key}: file not found")
    actual = sha256_file(pdf_path)
    if actual != expected_sha256:
        raise RenderRefused(f"{document_key}: SHA-256 differs from the approved file")
    out: list[RenderedPage] = []
    with pymupdf.open(pdf_path) as doc:  # type: ignore[no-untyped-call]
        wanted = page_numbers or list(range(1, doc.page_count + 1))
        for n in wanted:
            if not 1 <= n <= doc.page_count:
                raise RenderRefused(f"{document_key}: page {n} outside 1..{doc.page_count}")
            png = doc[n - 1].get_pixmap(dpi=dpi).tobytes("png")
            image = PageImage(
                document_key=document_key,
                page_number=n,
                png_sha256=hashlib.sha256(png).hexdigest(),
                dpi=dpi,
            )
            out.append(RenderedPage(image=image, png=png))
    return out
