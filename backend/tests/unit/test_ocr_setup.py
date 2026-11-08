"""Tesseract resolution (fake install layouts) and a real OCR smoke test when installed."""

from pathlib import Path

import pytest

from app.config import Settings
from app.ingestion import ocr
from app.ingestion.ocr import (
    SMOKE_TEXT,
    OcrSetupError,
    find_tesseract_cmd,
    ocr_smoke_test,
    resolve_tessdata,
    tesseract_setup,
)


def fake_install(root: Path, langs: tuple[str, ...] = ("eng", "osd")) -> Path:
    cmd = root / "Tesseract-OCR" / "tesseract.exe"
    (cmd.parent / "tessdata").mkdir(parents=True)
    cmd.write_bytes(b"synthetic")
    for lang in langs:
        (cmd.parent / "tessdata" / f"{lang}.traineddata").write_bytes(b"synthetic")
    return cmd


def settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key in ("TESSERACT_CMD", "TESSDATA_DIR"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def nothing_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr.shutil, "which", lambda _name: None)
    monkeypatch.setattr(ocr, "_standard_locations", lambda: ())


def test_explicit_cmd_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nothing_installed: None
) -> None:
    cmd = fake_install(tmp_path)
    s = settings(monkeypatch, TESSERACT_CMD=str(cmd))
    assert find_tesseract_cmd(s) == cmd
    assert resolve_tessdata(s, cmd) == cmd.parent / "tessdata"


def test_missing_explicit_cmd_is_an_error_not_a_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = fake_install(tmp_path)
    monkeypatch.setattr(ocr.shutil, "which", lambda _name: str(fake))  # a valid one exists
    s = settings(monkeypatch, TESSERACT_CMD=str(tmp_path / "typo.exe"))
    with pytest.raises(OcrSetupError, match="TESSERACT_CMD does not exist"):
        find_tesseract_cmd(s)


def test_falls_back_to_path_then_standard_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nothing_installed: None
) -> None:
    cmd = fake_install(tmp_path)
    s = settings(monkeypatch)
    monkeypatch.setattr(ocr, "_standard_locations", lambda: (tmp_path / "nope.exe", cmd))
    assert find_tesseract_cmd(s) == cmd
    on_path = fake_install(tmp_path / "onpath")
    monkeypatch.setattr(ocr.shutil, "which", lambda _name: str(on_path))
    assert find_tesseract_cmd(s) == on_path


def test_not_found_explains_how_to_fix(
    monkeypatch: pytest.MonkeyPatch, nothing_installed: None
) -> None:
    with pytest.raises(OcrSetupError, match="set TESSERACT_CMD"):
        find_tesseract_cmd(settings(monkeypatch))


def test_explicit_tessdata_dir_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cmd = fake_install(tmp_path)
    other = fake_install(tmp_path / "other").parent / "tessdata"
    s = settings(monkeypatch, TESSERACT_CMD=str(cmd), TESSDATA_DIR=str(other))
    assert resolve_tessdata(s, cmd) == other


def linux_layout(root: Path) -> tuple[Path, Path]:
    """Debian-style install: binary in usr/bin, data in usr/share/tesseract-ocr/5/tessdata."""
    cmd = root / "usr" / "bin" / "tesseract"
    cmd.parent.mkdir(parents=True)
    cmd.write_bytes(b"synthetic")
    data = root / "usr" / "share" / "tesseract-ocr" / "5" / "tessdata"
    data.mkdir(parents=True)
    (data / "eng.traineddata").write_bytes(b"synthetic")
    return cmd, data


def test_tessdata_reported_by_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cmd, data = linux_layout(tmp_path)
    listing = f'List of available languages in "{data.as_posix()}/" (1):\neng\n'
    monkeypatch.setattr(ocr, "_run_tesseract", lambda _cmd, arg: listing)
    s = settings(monkeypatch, TESSERACT_CMD=str(cmd))
    assert resolve_tessdata(s, cmd) == data


def test_executable_reporting_nothing_asks_for_tessdata_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd, _ = linux_layout(tmp_path)
    monkeypatch.setattr(ocr, "_run_tesseract", lambda _cmd, arg: "unexpected output")
    with pytest.raises(OcrSetupError, match="set TESSDATA_DIR"):
        resolve_tessdata(settings(monkeypatch, TESSERACT_CMD=str(cmd)), cmd)


def test_explicit_tessdata_dir_skips_probing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd, data = linux_layout(tmp_path)

    def boom(_cmd: Path, arg: str) -> str:
        raise AssertionError("must not run tesseract when TESSDATA_DIR is set")

    monkeypatch.setattr(ocr, "_run_tesseract", boom)
    s = settings(monkeypatch, TESSERACT_CMD=str(cmd), TESSDATA_DIR=str(data))
    assert resolve_tessdata(s, cmd) == data


def test_missing_english_data_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cmd = fake_install(tmp_path, langs=("osd",))
    with pytest.raises(OcrSetupError, match=r"missing Tesseract language data \['eng'\]"):
        resolve_tessdata(settings(monkeypatch, TESSERACT_CMD=str(cmd)), cmd)


def test_missing_tessdata_dir_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cmd = fake_install(tmp_path)
    s = settings(monkeypatch, TESSERACT_CMD=str(cmd), TESSDATA_DIR=str(tmp_path / "missing"))
    with pytest.raises(OcrSetupError, match="TESSDATA_DIR"):
        resolve_tessdata(s, cmd)


@pytest.fixture
def real_setup() -> ocr.TesseractSetup:
    try:
        return tesseract_setup(Settings(_env_file=None))  # type: ignore[call-arg]
    except OcrSetupError as exc:
        pytest.skip(f"Tesseract is not installed on this machine: {exc}")


def test_real_ocr_reads_an_image_only_page(real_setup: ocr.TesseractSetup) -> None:
    assert "tesseract" in real_setup.version.lower()
    text = ocr_smoke_test(real_setup).upper()
    for word in SMOKE_TEXT.split():
        assert word in text
