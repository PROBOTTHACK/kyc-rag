"""M2 D: `forms show` never crashes on characters the console cannot encode.

Presentation only: the console prints "?" for "₹" on a cp1252 console, while `--json`
output stays lossless (ASCII-escaped) and stored data is untouched.
"""

import io
import json
import os
import subprocess
import sys

import pytest

from app.config import REPO_ROOT
from app.ingestion.cli import safe_console, schema_json_text
from tests.unit.test_forms_extract import FIELDS, form_doc, rec, schema

LABEL = "Gross Annual Income (₹)"


def test_cp1252_console_prints_a_replacement_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", newline="\n")
    monkeypatch.setattr(sys, "stdout", console)
    with pytest.raises(UnicodeEncodeError):  # the original failure
        print(LABEL)
        console.flush()
    raw.seek(0)
    raw.truncate()
    safe_console()
    print(LABEL)
    console.flush()
    assert raw.getvalue().decode("cp1252") == "Gross Annual Income (?)\n"


def test_json_output_is_ascii_safe_and_lossless() -> None:
    meta = form_doc()
    s = schema(meta, [rec(meta, 0, f"Fields: *{LABEL} | PAN", section="S", kind=FIELDS)])
    text = schema_json_text(s)
    text.encode("cp1252")  # printable on any console
    assert "\\u20b9" in text and "₹" not in text
    restored = json.loads(text)
    assert restored == s.model_dump(mode="json")  # original Unicode preserved
    assert restored["sections"][0]["fields"][0]["label"] == LABEL


def test_cli_process_on_a_cp1252_console_does_not_crash() -> None:
    code = "from app.ingestion.cli import safe_console; safe_console(); print('Income (₹)')"
    env = os.environ | {"PYTHONIOENCODING": "cp1252", "TESSDATA_DIR": ""}
    out = subprocess.run(  # noqa: S603 (fixed interpreter and code)
        [sys.executable, "-c", code],
        cwd=REPO_ROOT / "backend",
        env=env,
        capture_output=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr[-400:]
    assert out.stdout.decode("cp1252").strip() == "Income (?)"
