"""M2 G1: multimodal contract, fake provider, rendering, approval list, schema additions.

No network: the fake provider is the only provider used here.
"""

import asyncio
import datetime as dt
import hashlib
import json
from pathlib import Path

import pymupdf
import pytest

from app.db.forms import schema_sha256
from app.forms.approval import (
    DEFAULT_PATH,
    ApprovedForm,
    NotApproved,
    check_approved,
    load_approvals,
)
from app.forms.render import RenderRefused, render_pages
from app.llm.fake import scripted
from app.llm.multimodal import MultimodalRequest, ProviderRefused, parse_structured
from app.schemas.forms import (
    ExtractedField,
    ExtractedSection,
    ExtractionProvenance,
    FormExtraction,
    PageProvenance,
    extraction_errors,
)
from tests.unit.test_forms_extract import FIELDS, form_doc, rec, schema

SHA = "a" * 64


def extraction(*fields: ExtractedField, name: str | None = "A) DETAILS") -> FormExtraction:
    return FormExtraction(
        sections=[ExtractedSection(name=name, is_declaration=False, fields=list(fields))]
    )


def fld(label: str = "*Name", page: int = 1) -> ExtractedField:
    return ExtractedField(printed_label=label, field_type="text", page_number=page)


# --- structured-output validation -------------------------------------------------------


def test_valid_output_parses() -> None:
    parsed, errors = parse_structured(extraction(fld()).model_dump_json(), FormExtraction)
    assert errors == [] and parsed == extraction(fld())


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"sections": [',
        '{"sections": "x"}',
        "{}",
        '{"sections": [], "notes": "extra key"}',
        # the model may never assert a policy obligation
        json.dumps(
            {
                "sections": [
                    {
                        "name": "A",
                        "is_declaration": False,
                        "fields": [
                            {
                                "printed_label": "*Name",
                                "field_type": "text",
                                "page_number": 1,
                                "mandatory_under_policy": True,
                            }
                        ],
                    }
                ]
            }
        ),
        '{"sections": [{"name": "A", "is_declaration": false, "fields": '
        '[{"printed_label": "", "field_type": "text", "page_number": 1}]}]}',
        '{"sections": [{"name": "A", "is_declaration": false, "fields": '
        '[{"printed_label": "x", "field_type": "radio", "page_number": 1}]}]}',
        '{"sections": [{"name": "A", "is_declaration": false, "fields": '
        '[{"printed_label": "x", "field_type": "text", "page_number": 0}]}]}',
    ],
)
def test_malformed_output_is_invalid(raw: str) -> None:
    parsed, errors = parse_structured(raw, FormExtraction)
    assert parsed is None and errors


def test_structural_errors_pages_not_sent_and_duplicates() -> None:
    ex = extraction(fld("*Name", 1), fld("*Name", 1), fld("PAN", 3))
    errors = extraction_errors(ex, sent_pages={1, 2})
    assert any("page 3 was not sent" in e for e in errors)
    assert any("duplicate label" in e for e in errors)
    assert extraction_errors(extraction(fld("*Name", 1), fld("*Name", 2)), {1, 2}) == []


# --- fake provider ----------------------------------------------------------------------


def request() -> MultimodalRequest:
    return MultimodalRequest(
        prompt_id="form_extraction", prompt_version="1", instruction="x", pages=[]
    )


def test_fake_provider_scripts_valid_malformed_and_refusal() -> None:
    fake = scripted(extraction(fld()), "{bad", ProviderRefused("blocked"))
    ok = asyncio.run(fake.analyze_pages([b"png"], request(), FormExtraction))
    assert ok.parsed == extraction(fld()) and ok.validation_errors == []
    bad = asyncio.run(fake.analyze_pages([b"png"], request(), FormExtraction))
    assert bad.parsed is None and bad.validation_errors
    assert bad.raw_text_sha256 == hashlib.sha256(b"{bad").hexdigest()
    with pytest.raises(ProviderRefused):
        asyncio.run(fake.analyze_pages([], request(), FormExtraction))
    assert [c.image_count for c in fake.calls] == [1, 1, 0]


# --- schema additions keep m1_chunks output byte-identical ------------------------------


def m1_schema() -> object:
    meta = form_doc()
    return schema(meta, [rec(meta, 0, "Fields: *Name | Surname", section="S", kind=FIELDS)])


def test_extension_keys_are_omitted_when_unset() -> None:
    s = m1_schema()
    data = s.model_dump(mode="json")  # type: ignore[attr-defined]
    assert "provenance" not in data
    for f in data["sections"][0]["fields"]:
        assert "field_type" not in f and "options" not in f
        assert f["appears_on_form"] is True and f["mandatory_under_policy"] is None


def test_extension_keys_are_serialised_when_set() -> None:
    s = m1_schema()
    f0 = s.sections[0].fields[0]  # type: ignore[attr-defined]
    f1 = f0.model_copy(update={"field_type": "checkbox", "options": ["Yes", "No"]})
    data = f1.model_dump(mode="json")
    assert data["field_type"] == "checkbox" and data["options"] == ["Yes", "No"]
    prov = ExtractionProvenance(
        provider="fake",
        model_id="m",
        prompt_id="p",
        prompt_version="1",
        render_dpi=150,
        pages=[PageProvenance(page_number=1, png_sha256=SHA)],
        latency_ms=1,
        attempts=1,
    )
    with_prov = s.model_copy(update={"provenance": prov})  # type: ignore[attr-defined]
    assert with_prov.model_dump(mode="json")["provenance"]["model_id"] == "m"
    assert schema_sha256(with_prov) != schema_sha256(s)  # type: ignore[arg-type]


# --- approval list ----------------------------------------------------------------------


def approved(**kw: object) -> ApprovedForm:
    base: dict[str, object] = {
        "document_key": "k",
        "sha256": SHA,
        "attestation": "public_blank_form",
        "source": "s",
        "approved_by": "owner",
        "approved_on": dt.date(2026, 11, 10),
    }
    return ApprovedForm.model_validate(base | kw)


def check(entry: ApprovedForm | None, **kw: str) -> ApprovedForm:
    args = {
        "document_key": "k",
        "document_type": "form",
        "manifest_sha256": SHA,
        "stored_sha256": SHA,
    }
    return check_approved({"k": entry} if entry else {}, **(args | kw))


def test_approved_form_passes() -> None:
    assert check(approved()).document_key == "k"


@pytest.mark.parametrize(
    ("entry", "kw", "reason"),
    [
        (None, {}, "not on the approved-forms list"),
        (approved(approved_by=None), {}, "not yet approved"),
        (approved(approved_on=None), {}, "not yet approved"),
        (approved(approved_by="  "), {}, "not yet approved"),
        (approved(), {"document_type": "reference"}, "not 'form'"),
        (approved(), {"manifest_sha256": "b" * 64}, "differs"),
        (approved(), {"stored_sha256": "b" * 64}, "differs"),
    ],
)
def test_refusals(entry: ApprovedForm | None, kw: dict[str, str], reason: str) -> None:
    with pytest.raises(NotApproved, match=reason):
        check(entry, **kw)


def test_attestation_is_required() -> None:
    with pytest.raises(ValueError):
        approved(attestation="filled_form")


def test_tracked_list_parses_and_blocks_everything_until_approved() -> None:
    approvals = load_approvals(DEFAULT_PATH)
    assert len(approvals) == 5
    assert all(e.attestation == "public_blank_form" for e in approvals.values())
    assert not any(e.is_approved for e in approvals.values())  # owner has not signed off


def test_malformed_and_duplicate_lists_are_refused(tmp_path: Path) -> None:
    p = tmp_path / "a.yaml"
    p.write_text("- document_key: k\n  sha256: nothex\n", encoding="utf-8")
    with pytest.raises(NotApproved, match="invalid"):
        load_approvals(p)
    entry = f"- {{document_key: k, sha256: {SHA}, attestation: public_blank_form, source: s}}\n"
    p.write_text(entry * 2, encoding="utf-8")
    with pytest.raises(NotApproved, match="duplicate"):
        load_approvals(p)


# --- rendering --------------------------------------------------------------------------


@pytest.fixture
def pdf(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "f.pdf"
    with pymupdf.open() as doc:
        for text in ("*Name", "Signature"):
            doc.new_page().insert_text((72, 72), text)
        doc.save(path)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_render_verifies_hash_and_returns_png(pdf: tuple[Path, str]) -> None:
    path, sha = pdf
    pages = render_pages(path, document_key="k", expected_sha256=sha, dpi=72)
    assert [p.image.page_number for p in pages] == [1, 2]
    for p in pages:
        assert p.png.startswith(b"\x89PNG") and p.image.dpi == 72
        assert p.image.png_sha256 == hashlib.sha256(p.png).hexdigest()
    again = render_pages(path, document_key="k", expected_sha256=sha, dpi=72)
    assert [p.image.png_sha256 for p in again] == [p.image.png_sha256 for p in pages]


def test_render_refuses_changed_file_missing_file_and_bad_page(
    pdf: tuple[Path, str], tmp_path: Path
) -> None:
    path, sha = pdf
    with pytest.raises(RenderRefused, match="SHA-256"):
        render_pages(path, document_key="k", expected_sha256="0" * 64)
    with pytest.raises(RenderRefused, match="not found"):
        render_pages(tmp_path / "nope.pdf", document_key="k", expected_sha256=sha)
    with pytest.raises(RenderRefused, match="page 3"):
        render_pages(path, document_key="k", expected_sha256=sha, page_numbers=[3])
