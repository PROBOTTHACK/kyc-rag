"""Tesseract setup for PyMuPDF OCR. (The OCR fallback itself is added in M1 Phase 3.)

PyMuPDF runs OCR with MuPDF's built-in Tesseract and only needs the language-data
directory; the `tesseract` executable is used to locate that directory and to report
the installed version. Resolution order:

    executable: TESSERACT_CMD -> `tesseract` on PATH -> standard install location
    tessdata:   TESSDATA_DIR  -> `tessdata/` next to the executable (Windows installer)
                -> the directory the executable reports (`--list-langs`; Linux packages
                   keep it elsewhere, e.g. /usr/share/tesseract-ocr/5/tessdata)

An explicitly configured path that does not exist is an error, never a silent fallback.
"""

import re
import shutil
import subprocess  # noqa: S404 - runs only the configured tesseract binary, no shell
import sys
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings

REQUIRED_LANGUAGES: tuple[str, ...] = ("eng",)
TESSERACT_TIMEOUT_S = 10.0


def _standard_locations() -> tuple[Path, ...]:
    if sys.platform == "win32":
        return (
            Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
            Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
        )
    return (Path("/usr/bin/tesseract"), Path("/usr/local/bin/tesseract"))


class OcrSetupError(RuntimeError):
    """Tesseract cannot be found or is missing required language data."""


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


SMOKE_TEXT = "KYC OCR SMOKE TEST 2026"


def ocr_smoke_test(setup: TesseractSetup) -> str:
    """OCR a synthetic image-only page through PyMuPDF; return the recognised text.

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
    tp = img_page.get_textpage_ocr(language="eng", dpi=300, full=True, tessdata=str(setup.tessdata))
    return str(img_page.get_text(textpage=tp)).strip()


def tesseract_setup(settings: Settings) -> TesseractSetup:
    cmd = find_tesseract_cmd(settings)
    tessdata = resolve_tessdata(settings, cmd)
    return TesseractSetup(
        cmd=cmd,
        tessdata=tessdata,
        version=tesseract_version(cmd),
        languages=installed_languages(tessdata),
    )
