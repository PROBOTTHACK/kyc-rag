"""Renders the checklist item catalogue as a Markdown review report (A4 content review).

`python -m app.assistant.catalogue_report [--out FILE]` (read-only: it writes only the
report). Group, classification and quotation status are derived exactly as the checklist
service derives them: group from the item kind, class from the source role (`classify`),
quotation only for native text (scanned-form text is OCR-uncertain, as the dev-stack test
verifies against Postgres).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.assistant.catalogue import Catalogue, CatalogueItem, NotConfirmedEntry, catalogue
from app.assistant.checklist import classify
from app.assistant.coverage import Coverage, coverage
from app.assistant.taxonomy import AccountType, CustomerCategory

REPORT_FILE = Path(__file__).resolve().parents[3] / "docs" / "assistant" / "A4_CATALOGUE_REVIEW.md"
ORGANISATIONS = {
    CustomerCategory.SOLE_PROPRIETORSHIP, CustomerCategory.PARTNERSHIP_FIRM, CustomerCategory.LLP,
    CustomerCategory.COMPANY, CustomerCategory.TRUST, CustomerCategory.HUF,
    CustomerCategory.OTHER_ORGANISATION,
}  # fmt: skip


def _cell(text: str | None) -> str:
    return "—" if not text else text.replace("|", "\\|").replace("\n", " ")


def _categories(cats: tuple[CustomerCategory, ...]) -> str:
    if set(cats) == ORGANISATIONS:
        return "All 7 organisation categories"
    return ", ".join(c.value for c in cats)


def _accounts(item: CatalogueItem, cov: Coverage) -> str:
    if item.scope == "conversion":
        return "Resident → NRO conversion request only"
    if item.accounts is not None:
        return ", ".join(a.value for a in item.accounts)
    covered = [a.value for a in AccountType if any(cov.cell(c, a).covered for c in item.categories)]
    return ", ".join(covered) + " (all covered)"


def _visibility(item: CatalogueItem) -> str:
    parts = []
    for slot, values in item.when.items():
        parts.append(
            f"{slot.value} = {' / '.join(values)} (shown with an “applies if” note when unknown)"
        )
    if item.only_when_asked:
        parts.append(f"only when asked about: {', '.join(t.value for t in item.topics)}")
    return "; ".join(parts) or "always"


def _item_row(item: CatalogueItem, cat: Catalogue, cov: Coverage) -> str:
    group = "B" if item.kind == "information" else "A"
    sources = []
    quotes = []
    ocr_texts = []
    scanned = False
    for ev in item.evidence:
        doc = cov.documents[ev.document_key]
        scanned |= doc.scanned
        sources.append(f"`{ev.document_key}` p{ev.page}")
        if doc.scanned:
            ocr_texts.append(f"p{ev.page}: `{ev.quote}`")
        else:
            quotes.append(f"p{ev.page}: “{ev.quote}”")
    cls = classify(cov.role(item.evidence[0].document_key)).value
    warning = cat.wording.ocr_warning if scanned else None
    quotation = "<br>".join(quotes) if quotes else "Not shown (OCR-uncertain)"
    stored = "<br>".join(ocr_texts) if ocr_texts else None
    return (
        "| "
        + " | ".join(
            [
                f"`{item.id}`",
                _cell(item.title),
                _cell(item.note),
                _categories(item.categories) if item.categories else "Conversion request",
                _cell(_accounts(item, cov)),
                _cell(_visibility(item)),
                f"{group} ({item.kind})",
                f"`{cls}`",
                "<br>".join(dict.fromkeys(sources)),
                quotation,
                _cell(stored),
                _cell(warning),
                _cell(item.condition),
                _cell(item.condition_note),
                ", ".join(t.value for t in item.topics) or "—",
            ]
        )
        + " |"
    )


def _entry_row(entry: NotConfirmedEntry, cov: Coverage) -> str:
    cats = entry.categories
    reason = entry.reason
    if entry.ruling is not None:
        ruling = cov.ruling(entry.ruling)
        cats = ruling.categories if ruling else ()
        reason = ruling.reason if ruling else None
    applies = "Conversion request" if entry.scope == "conversion" else _categories(cats)
    return (
        "| "
        + " | ".join(
            [
                f"`{entry.id}`",
                _cell(entry.title),
                _cell(entry.explanation),
                applies,
                f"only when asked about: {', '.join(t.value for t in entry.topics)}",
                "C",
                "`unconfirmed`",
                (
                    f"`{entry.ruling}` (A2 ruling)"
                    if entry.ruling
                    else "A1 §4 rule 1 (identifier field)"
                ),
                _cell(reason),
                ", ".join(f"`{r}`" for r in entry.related) or "—",
            ]
        )
        + " |"
    )


ITEM_HEADER = (
    "| ID | Customer-facing title | Note | Customer categories | Account types | Shown when "
    "| Group | Classification | Source (document, page) | Exact quotation (reliable text only) "
    "| Stored OCR text (verification only, never shown as a quotation) | OCR warning "
    "| Condition (verbatim) | Condition as shown to the customer | Topics |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
)
ENTRY_HEADER = (
    "| ID | Customer-facing title | Explanation shown to the customer | Customer categories "
    "| Shown when | Group | Classification | Basis "
    "| Internal rationale (review only, never shown) "
    "| Related item (context only, never evidence) |\n"
    "|---|---|---|---|---|---|---|---|---|---|"
)


def render(cat: Catalogue | None = None, cov: Coverage | None = None) -> str:
    cat = cat or catalogue()
    cov = cov or coverage()
    sections = [
        ("NRI individual: `hdfc_nri_account_opening` (native text)", "hdfc_nri_account_opening"),
        ("Organisations: `hdfc_non_individual_account_opening` (scanned, OCR-uncertain)",
         "hdfc_non_individual_account_opening"),  # fmt: skip
        ("Resident → NRO conversion: `hdfc_resident_to_nro_conversion` (native text)",
         "hdfc_resident_to_nro_conversion"),  # fmt: skip
    ]
    out = [
        "# A4 Catalogue Review: Checklist Items",
        "",
        "Generated from `backend/app/assistant/checklist.yaml` (catalogue "
        f"`{cat.version}`) and `coverage.yaml` (`{cov.version}`) by "
        "`python -m app.assistant.catalogue_report`. Do not edit by hand: a unit test checks "
        "that this file matches the data.",
        "",
        f"**{len(cat.items)} items** (Groups A and B) and **{len(cat.not_confirmed)} "
        "could-not-be-confirmed entries** (Group C).",
        "",
        "**How to read the columns:**",
        "- **Group:** A = documents and declarations the form asks for; B = information the "
        "form asks for; C = could not be confirmed.",
        "- **Classification:** assigned by code from the source's role (`classify`), never "
        "stored in the catalogue. Every A/B item here is `form_indicated`; nothing is "
        "`policy_supported` (no HDFC policy exists in the corpus).",
        "- **Exact quotation:** shown to customers only for native (reliable) text. "
        "Scanned-form text is OCR-uncertain: it is stored exactly as read so the citation can "
        "be verified, but it is shown only as an approximate reading with the OCR warning "
        "(A1 §8.2 rule 3a).",
        "- **Every quote is re-verified** as an exact substring of the stored chunk, on the "
        "stated document and page, on every checklist build and by "
        "`tests/integration/test_assistant_checklist_dev.py`.",
        "- **Shown when:** facet constraints come from the A3 profile. When the value is "
        "unknown, the item is shown with an “Applies if…” note; a known, different value "
        "leaves it out.",
        "",
    ]
    for title, key in sections:
        rows = [i for i in cat.items if i.evidence[0].document_key == key]
        out += [f"## {title}: {len(rows)} items", "", ITEM_HEADER]
        out += [_item_row(i, cat, cov) for i in rows]
        out.append("")
    out += [
        f"## Group C: could not be confirmed ({len(cat.not_confirmed)} entries)",
        "",
        "Shown **only when the customer explicitly asks** about one of the entry's topics "
        "(owner decision at the A4 review); never shown proactively. They never carry a "
        "basis citation. A related item's field may be shown as context, never as evidence "
        "(a PAN number field is not evidence of a PAN card copy).",
        "",
        ENTRY_HEADER,
        *(_entry_row(e, cov) for e in cat.not_confirmed),
        "",
        "**Also in Group C, generated at run time:** a topic the customer asked about that no "
        "item or entry answers becomes an `unknown` item with the prescribed fallback. Titles:",
        "",
        "| Topic | Title shown |",
        "|---|---|",
        *(f"| `{t.value}` | {_cell(w)} |" for t, w in cat.wording.unknown_topics.items()),
        "",
    ]
    return "\n".join(out)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.assistant.catalogue_report")
    parser.add_argument("--out", type=Path, default=REPORT_FILE)
    args = parser.parse_args(argv)
    args.out.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
