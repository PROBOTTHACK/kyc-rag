"""Tesseract setup and OCR runs for Pipeline A's OCR fallback.

PyMuPDF renders a page (or a region of it) to PNG; the configured `tesseract`
executable recognises it and returns TSV, which carries per-word confidence for
`document_pages.ocr_confidence` (PyMuPDF's built-in OCR does not expose confidence).
Runs are deterministic: fixed DPI, page-segmentation mode and language, and a single
thread (`OMP_THREAD_LIMIT=1`). Resolution order for the executable and language data:

    executable: TESSERACT_CMD -> `tesseract` on PATH -> standard install location
    tessdata:   TESSDATA_DIR  -> `tessdata/` next to the executable (Windows installer)
                -> the directory the executable reports (`--list-langs`; Linux packages
                   keep it elsewhere, e.g. /usr/share/tesseract-ocr/5/tessdata)

An explicitly configured path that does not exist is an error, never a silent fallback.
"""

import csv
import io
import os
import re
import shutil
import subprocess  # noqa: S404 - runs only the configured tesseract binary, no shell
import sys
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings

REQUIRED_LANGUAGES: tuple[str, ...] = ("eng",)
TESSERACT_TIMEOUT_S = 10.0  # --version / --list-langs
OCR_TIMEOUT_S = 120.0  # one rendered page or region
OCR_LANGUAGE = "eng"
OCR_PSM = 3  # fully automatic page segmentation
LOW_CONFIDENCE_LINE = 40.0  # a line below this mean word confidence is mostly noise


def _standard_locations() -> tuple[Path, ...]:
    if sys.platform == "win32":
        return (
            Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
            Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
        )
    return (Path("/usr/bin/tesseract"), Path("/usr/local/bin/tesseract"))


class OcrSetupError(RuntimeError):
    """Tesseract cannot be found or is missing required language data."""


class OcrRunError(RuntimeError):
    """One OCR run failed (process error, timeout, unreadable output)."""


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float  # mean word confidence, 0-100
    word_count: int
    bbox: tuple[float, float, float, float]  # pixels of the rendered image
    block: int
    paragraph: int
    line: int

    @property
    def low_confidence(self) -> bool:
        return self.confidence < LOW_CONFIDENCE_LINE


@dataclass(frozen=True)
class OcrText:
    """Parsed result of one Tesseract run."""

    lines: tuple[OcrLine, ...]
    word_confidences: tuple[float, ...]

    @property
    def word_count(self) -> int:
        return len(self.word_confidences)

    @property
    def mean_confidence(self) -> float:
        c = self.word_confidences
        return sum(c) / len(c) if c else 0.0

    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


@dataclass(frozen=True)
class TesseractSetup:
    cmd: Path
    tessdata: Path
    version: str
    languages: tuple[str, ...]


def find_tesseract_cmd(settings: Settings) -> Path:
    if settings.TESSERACT_CMD is not None:
        if not settings.TESSERACT_CMD.is_file():
            raise OcrSetupError(f"TESSERACT_CMD does not exist: {settings.TESSERACT_CMD}")
        return settings.TESSERACT_CMD
    on_path = shutil.which("tesseract")
    if on_path:
        return Path(on_path)
    for candidate in _standard_locations():
        if candidate.is_file():
            return candidate
    raise OcrSetupError(
        "Tesseract not found: set TESSERACT_CMD, add tesseract to PATH, or install it "
        "(Windows: winget install UB-Mannheim.TesseractOCR)"
    )


def _run_tesseract(cmd: Path, arg: str) -> str:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, configured binary, no shell
            [str(cmd), arg],
            capture_output=True,
            text=True,
            timeout=TESSERACT_TIMEOUT_S,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OcrSetupError(f"cannot run {cmd} {arg}: {type(exc).__name__}") from exc
    return out.stdout or out.stderr


def reported_tessdata(cmd: Path) -> Path | None:
    """Directory the executable itself uses, from `List of ... languages in "<dir>"`."""
    match = re.search(r'languages in "([^"]+)"', _run_tesseract(cmd, "--list-langs"))
    return Path(match.group(1)) if match else None


def resolve_tessdata(settings: Settings, cmd: Path) -> Path:
    if settings.TESSDATA_DIR is not None:
        tessdata, source = settings.TESSDATA_DIR, "TESSDATA_DIR"
    elif (cmd.parent / "tessdata").is_dir():
        tessdata, source = cmd.parent / "tessdata", f"tessdata next to {cmd}"
    else:
        reported = reported_tessdata(cmd)
        if reported is None:
            raise OcrSetupError(
                f"Tesseract language data not found next to {cmd} and {cmd} did not report "
                "a tessdata directory; set TESSDATA_DIR"
            )
        tessdata, source = reported, f"reported by {cmd} --list-langs"
    if not tessdata.is_dir():
        raise OcrSetupError(f"Tesseract language data not found ({source}): {tessdata}")
    missing = [
        lang for lang in REQUIRED_LANGUAGES if not (tessdata / f"{lang}.traineddata").is_file()
    ]
    if missing:
        raise OcrSetupError(f"missing Tesseract language data {missing} in {tessdata}")
    return tessdata


def installed_languages(tessdata: Path) -> tuple[str, ...]:
    return tuple(sorted(p.stem for p in tessdata.glob("*.traineddata")))


def tesseract_version(cmd: Path) -> str:
    first = _run_tesseract(cmd, "--version").strip().splitlines()
    return first[0] if first else "unknown"


def parse_tsv(tsv: str) -> OcrText:
    """Group Tesseract TSV words (level 5) into lines; keep word confidences."""
    rows = csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)
    if rows.fieldnames is None or "conf" not in rows.fieldnames:
        raise OcrRunError("tesseract output is not TSV")
    grouped: dict[tuple[int, int, int], list[dict[str, str]]] = {}
    for row in rows:
        if row.get("level") != "5" or not (row.get("text") or "").strip():
            continue
        key = (int(row["block_num"]), int(row["par_num"]), int(row["line_num"]))
        grouped.setdefault(key, []).append(row)
    lines: list[OcrLine] = []
    confidences: list[float] = []
    for (block, par, line_no), words in grouped.items():  # TSV order = reading order
        confs = [float(w["conf"]) for w in words if float(w["conf"]) >= 0]
        confidences.extend(confs)
        x0 = min(int(w["left"]) for w in words)
        y0 = min(int(w["top"]) for w in words)
        x1 = max(int(w["left"]) + int(w["width"]) for w in words)
        y1 = max(int(w["top"]) + int(w["height"]) for w in words)
        lines.append(
            OcrLine(
                text=" ".join(w["text"].strip() for w in words),
                confidence=round(sum(confs) / len(confs), 2) if confs else 0.0,
                word_count=len(words),
                bbox=(float(x0), float(y0), float(x1), float(y1)),
                block=block,
                paragraph=par,
                line=line_no,
            )
        )
    return OcrText(lines=tuple(lines), word_confidences=tuple(confidences))


def run_ocr(setup: TesseractSetup, png: bytes, dpi: int) -> OcrText:
    """Recognise one rendered PNG with the configured executable."""
    argv = [
        str(setup.cmd), "stdin", "stdout",
        "--tessdata-dir", str(setup.tessdata),
        "-l", OCR_LANGUAGE,
        "--psm", str(OCR_PSM),
        "--dpi", str(dpi),
        "tsv",
    ]  # fmt: skip
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"}
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, configured binary, no shell
            argv, input=png, capture_output=True, timeout=OCR_TIMEOUT_S, check=True, env=env
        )
    except subprocess.TimeoutExpired as exc:
        raise OcrRunError(f"tesseract timed out after {OCR_TIMEOUT_S:.0f}s") from exc
    except subprocess.CalledProcessError as exc:
        detail = (
            exc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] if exc.stderr else []
        )
        raise OcrRunError(f"tesseract exited with {exc.returncode}: {' '.join(detail)}") from exc
    except OSError as exc:
        raise OcrRunError(f"cannot run tesseract: {type(exc).__name__}") from exc
    return parse_tsv(out.stdout.decode("utf-8", "replace"))


SMOKE_TEXT = "KYC OCR SMOKE TEST 2026"


def ocr_smoke_test(setup: TesseractSetup) -> str:
    """OCR a synthetic image-only page through the pipeline's own path; return the text.

    The page is rasterised first, so it has no text layer, just like the scanned forms.
    """
    import pymupdf  # local: keeps app import time low for non-ingestion code

    src = pymupdf.open()
    page = src.new_page(width=400, height=120)
    page.insert_text((20, 70), SMOKE_TEXT, fontsize=24)
    pix = page.get_pixmap(dpi=200)

    scanned = pymupdf.open()
    img_page = scanned.new_page(width=400, height=120)
    img_page.insert_image(img_page.rect, pixmap=pix)
    if img_page.get_text().strip():
        raise OcrSetupError("smoke page unexpectedly has a text layer")
    dpi = 300
    return run_ocr(setup, img_page.get_pixmap(dpi=dpi).tobytes("png"), dpi).text()


def tesseract_setup(settings: Settings) -> TesseractSetup:
    cmd = find_tesseract_cmd(settings)
    tessdata = resolve_tessdata(settings, cmd)
    return TesseractSetup(
        cmd=cmd,
        tessdata=tessdata,
        version=tesseract_version(cmd),
        languages=installed_languages(tessdata),
    )
