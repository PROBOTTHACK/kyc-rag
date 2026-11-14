"""A4 content review: the committed catalogue report matches the catalogue data exactly."""

from app.assistant.catalogue import catalogue
from app.assistant.catalogue_report import REPORT_FILE, render


def test_the_committed_report_is_current() -> None:
    assert (
        REPORT_FILE.read_text(encoding="utf-8") == render()
    ), "run `python -m app.assistant.catalogue_report` to regenerate"


def test_the_report_lists_every_item_and_entry_once() -> None:
    rows = [line for line in render().splitlines() if line.startswith("| `")]
    first_cells = [row.split(" | ")[0] for row in rows]
    ids = [i.id for i in catalogue().items] + [e.id for e in catalogue().not_confirmed]
    for item_id in ids:
        assert first_cells.count(f"| `{item_id}`") == 1, item_id


def test_ocr_text_is_never_presented_as_a_quotation() -> None:
    for line in render().splitlines():
        if "`hdfc_non_individual_account_opening` p" in line:
            assert "Not shown (OCR-uncertain)" in line and "“" not in line
