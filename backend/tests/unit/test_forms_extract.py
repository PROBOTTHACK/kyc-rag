"""M2 C1: deterministic form-schema extraction from stored M1 chunks (synthetic chunks)."""

import random
from typing import Any

import pytest
from pydantic import ValidationError

from app.db.corpus import document_row
from app.forms.extract import NotAFormError, build_schema, parse_label
from app.ingestion.chunking import ChunkRecord
from app.schemas.corpus import ChunkMetadata, DocumentMetadata
from app.schemas.forms import FormField, FormSchema
from app.schemas.taxonomy import ContentKind, ExtractionMethod, PageStatus, TextReliability
from tests.unit.test_indexing import doc


def form_doc(key: str = "hdfc_test_form", **kw: Any) -> DocumentMetadata:
    base: dict[str, Any] = {
        "bank": "HDFC",
        "document_type": "form",
        "source_authority": None,
        "attribution_basis": "filename_only",
        "source_status": "unknown",
        "status_reason": "synthetic",
        "title": "Synthetic Form",
    }
    return doc(key, **(base | kw))


def rec(
    d: DocumentMetadata,
    i: int,
    content: str,
    *,
    section: str | None,
    kind: ContentKind = ContentKind.FORM_TEXT,
    page: int = 1,
    ocr: TextReliability | None = None,
    confidence: float | None = None,
    excluded: int = 0,
) -> ChunkRecord:
    meta = ChunkMetadata.for_document(
        d,
        chunk_index=i,
        page_number=page,
        content=content,
        extraction_method=ExtractionMethod.OCR if ocr else ExtractionMethod.TEXT,
        section=section,
        content_kind=kind,
        page_status=PageStatus.OK,
        ocr_confidence=confidence,
        text_reliability=ocr or TextReliability.NATIVE,
    )
    extras: dict[str, Any] = {"raw_text": content}
    if excluded:
        extras["excluded_lines"] = [{"text": "x", "page": page, "confidence": 0.1}] * excluded
    return ChunkRecord(metadata=meta, extras=extras)


def schema(d: DocumentMetadata, records: list[ChunkRecord], **row: Any) -> FormSchema:
    return build_schema(document_row(d, **({"quality": "text"} | row)), records)


FIELDS = ContentKind.FORM_FIELDS

# --- labels ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("printed", "expected"),
    [
        ("*Name", ("Name", True, None)),
        ("Surname", ("Surname", False, None)),
        ("  *  Spaced label ", ("Spaced label", True, None)),
        ("**Thumb impression", ("Thumb impression", None, "2 leading asterisks")),
        (
            "Date of Birth *Marital Status",
            ("Date of Birth *Marital Status", None, "asterisk not at the start of the label"),
        ),
        (
            '(All fields marked "*" are MANDATORY)',
            (
                '(All fields marked "*" are MANDATORY)',
                None,
                "asterisk not at the start of the label",
            ),
        ),
        ("*", ("", True, None)),  # no label left: reported as an empty piece, not a field
    ],
)
def test_parse_label(printed: str, expected: tuple[str, bool | None, str | None]) -> None:
    assert parse_label(printed) == expected


def test_fields_keep_printed_labels_and_provenance() -> None:
    d = form_doc()
    r = rec(
        d,
        0,
        "Fields: *Name | Surname | **Odd | Email *ID",
        section="A) DETAILS",
        kind=FIELDS,
        page=2,
    )
    s = schema(d, [r])
    fields = s.sections[0].fields
    assert [f.printed_label for f in fields] == ["*Name", "Surname", "**Odd", "Email *ID"]
    assert [f.label for f in fields] == ["Name", "Surname", "Odd", "Email *ID"]
    assert [f.required_as_shown for f in fields] == [True, False, None, None]
    assert [f.position for f in fields] == [0, 1, 2, 3]
    for f in fields:
        assert f.chunk_id == r.metadata.chunk_id and f.page_number == 2
        assert f.appears_on_form is True and f.mandatory_under_policy is None
        assert f.verification_status == "extracted" and f.text_reliability == "native"
    assert s.field_count == 4 and s.required_as_shown_count == 1
    assert "ambiguous_required_markers" in s.flags


def test_empty_pieces_and_unexpected_formats_are_reported_not_invented() -> None:
    d = form_doc()
    s = schema(
        d,
        [
            rec(d, 0, "Fields: A |  | B", section="S", kind=FIELDS),
            rec(d, 1, "A | B without the prefix", section="S", kind=FIELDS),
        ],
    )
    assert [f.printed_label for f in s.sections[0].fields] == ["A", "B"]
    assert [i.issue for i in s.issues] == ["empty_label", "unexpected_field_chunk_format"]
    assert [b.text for b in s.sections[0].text_blocks] == ["A | B without the prefix"]
    assert "parse_issues" in s.flags


def test_sections_keep_order_pages_and_declarations() -> None:
    d = form_doc()
    s = schema(
        d,
        [
            rec(d, 0, "Heading text", section="A) DETAILS", page=1),
            rec(d, 1, "Fields: X", section="A) DETAILS", kind=FIELDS, page=1),
            rec(d, 2, "I declare that ...", section="B) DECLARATION", page=2),
            rec(d, 3, "Fields: Y", section="A) DETAILS", kind=FIELDS, page=3),  # recurs later
        ],
    )
    assert [(x.ordinal, x.name, x.page_start, x.page_end) for x in s.sections] == [
        (0, "A) DETAILS", 1, 1),
        (1, "B) DECLARATION", 2, 2),
        (2, "A) DETAILS", 3, 3),
    ]
    assert [x.is_declaration for x in s.sections] == [False, True, False]
    assert s.sections[0].chunk_ids == [
        s.sections[0].text_blocks[0].chunk_id,
        s.sections[0].fields[0].chunk_id,
    ]


def test_ocr_text_stays_verbatim_text_with_its_uncertainty() -> None:
    d = form_doc("hdfc_scanned_form")
    text = "NRI CUSTOMER UPDATION FORM\n“CUSTOMER ID LITT ttt"
    s = schema(
        d,
        [
            rec(
                d,
                0,
                text,
                section="1 NRI FORM",
                ocr=TextReliability.OCR_UNCERTAIN,
                confidence=0.78,
                excluded=2,
            ),
            rec(
                d,
                1,
                "Fields: *Name | PAN",
                section="1 NRI FORM",
                kind=FIELDS,
                ocr=TextReliability.OCR_RELIABLE,
                confidence=0.91,
            ),
        ],
        quality="scanned",
    )
    sec = s.sections[0]
    assert sec.fields == [] and s.field_count == 0  # OCR is never structured into fields
    assert [b.text for b in sec.text_blocks] == [text, "Fields: *Name | PAN"]  # verbatim
    first, second = sec.text_blocks
    assert (first.verification_status, first.ocr_confidence, first.excluded_line_count) == (
        "ocr_uncertain",
        0.78,
        2,
    )
    assert second.verification_status == "ocr_reliable"
    assert set(sec.flags) == {"no_field_structure", "ocr_uncertain_text"}
    assert {"no_field_structure", "scanned_document", "ocr_uncertain_text"} <= set(s.flags)
    assert [i.issue for i in s.issues] == ["ocr_field_list_kept_as_text"]


def test_extraction_is_deterministic_and_timestamp_free() -> None:
    d = form_doc()
    records = [
        rec(
            d,
            i,
            f"Fields: *F{i} | G{i}" if i % 2 else f"text {i}",
            section=f"S{i // 3}",
            kind=FIELDS if i % 2 else ContentKind.FORM_TEXT,
            page=1 + i // 4,
        )
        for i in range(12)
    ]
    first = schema(d, records).model_dump_json()
    shuffled = records[:]
    random.Random(7).shuffle(shuffled)  # noqa: S311 - test input order, not security
    assert schema(d, shuffled).model_dump_json() == first  # input order does not matter
    assert schema(d, records).model_dump_json() == first
    assert "created" not in first and "timestamp" not in first


def test_roles_and_non_forms() -> None:
    ref = form_doc("hdfc_reference_doc", document_type="reference", source_status="unknown")
    s = schema(ref, [rec(ref, 0, "Fields: Name", section="S", kind=FIELDS)])
    assert s.document_role == "reference_document" and s.evidence_eligibility == "context_only"
    assert {"reference_document", "context_only"} <= set(s.flags)
    policy = doc("rbi_policy_doc")  # regulatory
    with pytest.raises(NotAFormError):
        schema(policy, [])


def test_mandatory_under_policy_cannot_be_set_by_extraction() -> None:
    base = {
        "printed_label": "*Name",
        "label": "Name",
        "required_as_shown": True,
        "page_number": 1,
        "chunk_id": "c",
        "position": 0,
        "text_reliability": "native",
        "verification_status": "extracted",
    }
    assert FormField.model_validate(base).mandatory_under_policy is None
    with pytest.raises(ValidationError):
        FormField.model_validate(base | {"mandatory_under_policy": True})
    with pytest.raises(ValidationError):
        FormField.model_validate(base | {"appears_on_form": False})
