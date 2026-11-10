"""M2 G3: multimodal form pipeline units (fake provider, synthetic PDF, no database)."""

import asyncio
import hashlib
import uuid
from pathlib import Path

import pymupdf
import pytest

from app.db.models import Document
from app.forms.approval import ApprovedForm
from app.forms.pipeline import (
    PageOutcome,
    PageText,
    Prepared,
    build_schema,
    corroborate,
    extract_pages,
    normalise,
)
from app.llm.fake import scripted
from app.llm.multimodal import ProviderRateLimited
from app.schemas.forms import ExtractedField, ExtractedSection, FormExtraction


def ex(*labels: str, page: int = 1, declaration: bool = False) -> FormExtraction:
    return FormExtraction(
        sections=[
            ExtractedSection(
                name="A) DETAILS",
                is_declaration=declaration,
                fields=[
                    ExtractedField(printed_label=lbl, field_type="text", page_number=page)
                    for lbl in labels
                ],
            )
        ]
    )


def text(chunk: str, content: str, reliability: str = "native") -> PageText:
    return PageText(chunk, f" {normalise(content)} ", reliability)


# --- corroboration ----------------------------------------------------------------------


def test_normalise() -> None:
    assert normalise("  *Mother's  Maiden-Name: ") == "mother s maiden name"
    assert normalise("ﬁle Nº") == "file no"  # NFKC ligature and numero sign


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("SURNAME", "c1"),
        ("*Surname", "c1"),  # the marker is not part of the match
        ("Address Proof", "c2"),
        ("Customer Name:", "c1"),
        ("Name", "c1"),  # whole words: "Customer Name"
        ("Mother's Maiden Name", None),
        ("ame", None),  # not a whole word
        ("No", None),  # too short to corroborate
    ],
)
def test_corroborate(label: str, expected: str | None) -> None:
    texts = [
        text("c1", "Fields: Account Number Customer Name | SURNAME | Form 60."),
        text("c2", "Fields: Address Proof | Identity Proof"),
    ]
    match = corroborate(label, texts)
    assert (match.chunk_id if match else None) == expected


# --- page extraction loop ---------------------------------------------------------------


@pytest.fixture
def prepared(tmp_path: Path) -> Prepared:
    path = tmp_path / "form.pdf"
    with pymupdf.open() as doc:
        for _ in range(2):
            doc.new_page().insert_text((72, 72), "Customer Name | SURNAME")
        doc.save(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    document = Document(
        id=uuid.uuid4(),
        document_key="synthetic_form",
        sha256=sha,
        title="Synthetic blank form",
        version=None,
        document_type="form",
        evidence_eligibility="supporting",
        quality="text",
    )
    approval = ApprovedForm(
        document_key="synthetic_form",
        sha256=sha,
        attestation="public_blank_form",
        source="synthetic",
        approved_by="project owner",
        approved_on="2026-11-10",  # type: ignore[arg-type]
    )
    texts = {
        1: [text("chunk-p1", "Fields: Customer Name | SURNAME")],
        2: [text("chunk-p2", "Fields: Signature | Date", "ocr_reliable")],
    }
    return Prepared(document, approval, path, texts)


def run_pages(prepared: Prepared, *responses: object) -> tuple[list[PageOutcome], object]:
    fake = scripted(*responses)  # type: ignore[arg-type]
    return asyncio.run(extract_pages(prepared, fake, dpi=72)), fake


def test_one_request_per_page_with_page_provenance(prepared: Prepared) -> None:
    outs, fake = run_pages(prepared, ex("*Customer Name"), ex("Signature", page=2))
    assert [o.page_number for o in outs] == [1, 2]
    assert all(o.extraction is not None and o.attempts == 1 for o in outs)
    calls = fake.calls  # type: ignore[attr-defined]
    assert [c.image_count for c in calls] == [1, 1]
    assert [c.request.pages[0].page_number for c in calls] == [1, 2]
    assert "physical page 2" in calls[1].request.instruction
    assert all(len(o.png_sha256) == 64 for o in outs)


def test_invalid_output_is_reasked_once_then_accepted(prepared: Prepared) -> None:
    outs, fake = run_pages(prepared, "{bad json", ex("SURNAME"), ex("Signature", page=2))
    assert outs[0].extraction == ex("SURNAME") and outs[0].attempts == 2
    reask = fake.calls[1].request.instruction  # type: ignore[attr-defined]
    assert "rejected by validation" in reask
    assert outs[0].errors and outs[1].attempts == 1


def test_field_on_a_page_not_sent_is_invalid(prepared: Prepared) -> None:
    outs, _ = run_pages(prepared, ex("SURNAME", page=2), ex("SURNAME", page=1), ex("x", page=2))
    assert outs[0].attempts == 2 and any("was not sent" in e for e in outs[0].errors)


def test_invalid_twice_degrades_the_page_never_repairs(prepared: Prepared) -> None:
    outs, fake = run_pages(prepared, "{bad", '{"sections": "x"}', ex("Signature", page=2))
    assert outs[0].extraction is None and outs[0].attempts == 2
    assert len(fake.calls) == 3  # type: ignore[attr-defined]  # never a third request
    schema = build_schema(prepared, outs, provider=fake, dpi=72)  # type: ignore[arg-type]
    assert {"partial_extraction", "invalid_output_page_1"} <= set(schema.flags)
    assert [s.page_start for s in schema.sections] == [2]
    assert schema.provenance is not None and schema.provenance.validation_errors


def test_provider_errors_propagate(prepared: Prepared) -> None:
    with pytest.raises(ProviderRateLimited):
        run_pages(prepared, ProviderRateLimited("quota"))


# --- mapping to FormSchema --------------------------------------------------------------


def test_mapping_corroboration_markers_and_provenance(prepared: Prepared) -> None:
    outs, fake = run_pages(
        prepared,
        ex("*Customer Name", "SURNAME", "Mother's Maiden Name", "Name*"),
        ex("Signature", page=2, declaration=True),
    )
    s = build_schema(prepared, outs, provider=fake, dpi=72)  # type: ignore[arg-type]
    assert (s.extraction_method, s.extractor_version) == ("gemini_multimodal", 1)
    fields = {f.printed_label: f for sec in s.sections for f in sec.fields}
    cn = fields["*Customer Name"]
    assert (cn.label, cn.required_as_shown, cn.chunk_id) == ("Customer Name", True, "chunk-p1")
    assert cn.verification_status == "corroborated" and cn.text_reliability == "native"
    assert fields["SURNAME"].required_as_shown is False
    mm = fields["Mother's Maiden Name"]
    assert (mm.verification_status, mm.chunk_id, mm.text_reliability) == (
        "uncorroborated",
        None,
        "model_output",
    )
    assert fields["Name*"].required_as_shown is None and fields["Name*"].marker_issue
    sig = fields["Signature"]
    assert sig.verification_status == "corroborated" and sig.text_reliability == "ocr_reliable"
    assert s.sections[1].is_declaration and s.sections[0].chunk_ids == ["chunk-p1"]
    for f in fields.values():
        assert f.appears_on_form is True and f.mandatory_under_policy is None
        assert f.field_type == "text"
    assert (s.field_count, s.required_as_shown_count, s.text_block_count) == (5, 1, 0)
    assert {
        "multimodal_model_output",
        "uncorroborated_fields",
        "ambiguous_required_markers",
    } <= set(s.flags)
    assert "partial_extraction" not in s.flags
    p = s.provenance
    assert p is not None and (p.provider, p.model_id, p.render_dpi, p.attempts) == (
        "fake",
        "fake-multimodal-1",
        72,
        2,
    )
    assert [pg.page_number for pg in p.pages] == [1, 2]
    data = s.model_dump(mode="json")
    assert data["sections"][0]["fields"][0]["field_type"] == "text"
