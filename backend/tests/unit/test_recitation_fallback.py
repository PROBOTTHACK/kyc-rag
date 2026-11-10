"""M2 G3 follow-up: RECITATION refusals -> local OCR fallback (no network, no key).

Covers: diagnostics capture without keeping the partial text; RECITATION is never retried
or sent to another model; SAFETY/SPII unchanged; per-page OCR fallback with provenance;
the all-pages-refused schema; and that OCR text can never become fields or carry a
policy obligation.
"""

import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pymupdf
import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.db.forms import schema_sha256
from app.db.models import Document
from app.forms.approval import ApprovedForm
from app.forms.ocr_fallback import ocr_fallback_page
from app.forms.pipeline import Prepared, build_schema, extract_pages
from app.llm.fake import FakeMultimodalProvider, scripted
from app.llm.fallback import FallbackMultimodalProvider
from app.llm.gemini import GeminiProvider
from app.llm.multimodal import (
    Citation,
    MultimodalRequest,
    ProviderRecitation,
    ProviderRefused,
    RecitationDiagnostics,
)
from app.schemas.forms import (
    ExtractedField,
    ExtractedSection,
    FormExtraction,
    FormSchema,
    OcrFallbackLine,
    OcrFallbackPage,
)
from tests.unit.test_gemini_provider import EXAMPLE_ENV, KEY, VALID, FakeTime, response

PARTIAL = "RECITED PARTIAL OUTPUT THAT MUST NEVER BE STORED"


def recitation_response(finish: str = "RECITATION") -> Any:
    citation = SimpleNamespace(
        uri="https://example.org/source",
        title="Some source",
        license="",
        publication_date=None,
        start_index=0,
        end_index=42,
    )
    candidate = SimpleNamespace(
        finish_reason=SimpleNamespace(name=finish),
        finish_message="Recitation detected",
        citation_metadata=SimpleNamespace(citations=[citation]),
        content=SimpleNamespace(parts=[SimpleNamespace(text=PARTIAL)]),
    )
    return SimpleNamespace(
        text=PARTIAL, candidates=[candidate], prompt_feedback=None, usage_metadata=None
    )


def gemini(script: list[Any], model: str = "gemini-3.8-flash") -> GeminiProvider:
    t = FakeTime()

    async def generate(images: list[bytes], req: Any, schema: Any, timeout_s: float) -> Any:
        nxt = script.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    return GeminiProvider(
        model_id=model, api_key=SecretStr(KEY), rpm=10, sleep=t.sleep, clock=t.clock,
        generate=generate,
    )  # fmt: skip


def call(provider: Any) -> Any:
    req = MultimodalRequest(prompt_id="p", prompt_version="1", instruction="go", pages=[])
    return asyncio.run(provider.analyze_pages([b"png"], req, FormExtraction))


def diag(model: str = "gemini-3.8-flash") -> RecitationDiagnostics:
    return RecitationDiagnostics(
        provider="gemini",
        model_id=model,
        finish_reason="RECITATION",
        finish_message="Recitation detected",
        citations=[Citation(uri="https://example.org/source", start_index=0, end_index=42)],
        partial_output_chars=len(PARTIAL),
        partial_output_sha256=hashlib.sha256(PARTIAL.encode()).hexdigest(),
    )


# --- provider: capture, no retry, no other model ----------------------------------------


@pytest.mark.parametrize("finish", ["RECITATION", "IMAGE_RECITATION"])
def test_recitation_is_captured_without_the_partial_text(finish: str) -> None:
    p = gemini([recitation_response(finish), response(VALID.model_dump_json())])
    with pytest.raises(ProviderRecitation) as err:
        call(p)
    d = err.value.diagnostics
    assert (d.finish_reason, d.model_id, d.finish_message) == (
        finish,
        "gemini-3.8-flash",
        "Recitation detected",
    )
    assert d.citations[0].uri == "https://example.org/source" and d.citations[0].end_index == 42
    assert d.partial_output_chars == len(PARTIAL)
    assert d.partial_output_sha256 == hashlib.sha256(PARTIAL.encode()).hexdigest()
    assert PARTIAL not in d.model_dump_json() and PARTIAL not in str(err.value)
    assert p.attempts_made == 1  # a refusal is never retried
    assert err.value.transient is False and isinstance(err.value, ProviderRefused)


@pytest.mark.parametrize("finish", ["SAFETY", "SPII", "PROHIBITED_CONTENT"])
def test_safety_refusals_are_unchanged(finish: str) -> None:
    with pytest.raises(ProviderRefused) as err:
        call(gemini([response("{}", finish=finish)]))
    assert not isinstance(err.value, ProviderRecitation)


def test_recitation_is_never_sent_to_the_fallback_model() -> None:
    primary = gemini([recitation_response()])
    fallback = gemini([response(VALID.model_dump_json())], model="gemini-3.5-flash-lite")
    with pytest.raises(ProviderRecitation):
        call(FallbackMultimodalProvider(primary, fallback))
    assert fallback.attempts_made == 0


# --- pipeline: per-page OCR fallback ----------------------------------------------------


@pytest.fixture
def prepared(tmp_path: Path) -> Prepared:
    path = tmp_path / "form.pdf"
    with pymupdf.open() as doc:
        for _ in range(2):
            doc.new_page().insert_text((72, 72), "SURNAME")
        doc.save(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    document = Document(
        id=uuid.uuid4(), document_key="synthetic_form", sha256=sha, title="Synthetic blank form",
        version=None, document_type="form", evidence_eligibility="supporting", quality="scanned",
    )  # fmt: skip
    approval = ApprovedForm(
        document_key="synthetic_form", sha256=sha, attestation="public_blank_form",
        source="synthetic", approved_by="project owner", approved_on="2026-11-10",  # type: ignore[arg-type]
    )  # fmt: skip
    return Prepared(document, approval, path, {})


def fake_ocr(calls: list[tuple[int, str]]) -> Any:
    def fn(path: Path, page: int, d: RecitationDiagnostics, sha: str) -> OcrFallbackPage:
        calls.append((page, sha))
        return OcrFallbackPage(
            page_number=page, engine="tesseract test", dpi=300, psm=3, language="eng",
            lines=[OcrFallbackLine(region="full", text="*Name of Entity", confidence=91.5,
                                   low_confidence=False)],
            recitation=d,
        )  # fmt: skip

    return fn


def page_extraction(page: int) -> FormExtraction:
    return FormExtraction(
        sections=[
            ExtractedSection(
                name="A",
                is_declaration=False,
                fields=[
                    ExtractedField(printed_label="SURNAME", field_type="text", page_number=page)
                ],
            )
        ]
    )


def test_refused_page_gets_ocr_fallback_and_other_pages_continue(prepared: Prepared) -> None:
    calls: list[tuple[int, str]] = []
    fake = scripted(ProviderRecitation("Gemini stopped generation: RECITATION", diag()),
                    page_extraction(2))  # fmt: skip
    outs = asyncio.run(extract_pages(prepared, fake, dpi=72, ocr_fallback=fake_ocr(calls)))
    assert [o.outcome for o in outs] == ["recitation_refused", "extracted"]
    assert len(fake.calls) == 2  # the refused page was not re-asked
    assert calls == [(1, prepared.approval.sha256)]
    s = build_schema(prepared, outs, provider=fake, dpi=72)
    assert s.field_count == 1 and {f.page_number for sec in s.sections for f in sec.fields} == {2}
    assert {"recitation_refused", "recitation_refused_page_1", "ocr_fallback_requires_review",
            "partial_extraction"} <= set(s.flags)  # fmt: skip
    assert "gemini_extraction_refused" not in s.flags
    assert s.provenance is not None
    assert [(p.page_number, p.outcome) for p in s.provenance.pages] == [
        (1, "recitation_refused"),
        (2, "extracted"),
    ]
    fb = s.ocr_fallback[0]
    assert (fb.page_number, fb.source, fb.reason) == (
        1,
        "local_tesseract_ocr",
        "recitation_refused",
    )
    assert (fb.verification_status, fb.review_status) == ("unverified_ocr", "requires_human_review")
    assert fb.lines[0].text == "*Name of Entity" and fb.lines[0].confidence == 91.5
    assert fb.recitation.citations[0].uri == "https://example.org/source"


def test_all_pages_refused_builds_a_zero_field_schema(prepared: Prepared) -> None:
    fake = scripted(ProviderRecitation("r", diag()), ProviderRecitation("r", diag()))
    outs = asyncio.run(extract_pages(prepared, fake, dpi=72, ocr_fallback=fake_ocr([])))
    s = build_schema(prepared, outs, provider=fake, dpi=72)
    assert s.sections == [] and s.field_count == 0 and s.required_as_shown_count == 0
    assert {"gemini_extraction_refused", "no_field_structure", "partial_extraction",
            "recitation_refused_page_1", "recitation_refused_page_2"} <= set(s.flags)  # fmt: skip
    assert [p.page_number for p in s.ocr_fallback] == [1, 2]
    data = s.model_dump(mode="json")
    assert data["sections"] == [] and len(data["ocr_fallback"]) == 2
    assert "*Name of Entity" not in json.dumps(data["sections"])  # OCR never becomes fields


def test_without_an_ocr_fallback_the_refusal_propagates(prepared: Prepared) -> None:
    with pytest.raises(ProviderRecitation):
        asyncio.run(extract_pages(prepared, scripted(ProviderRecitation("r", diag())), dpi=72))


def test_safety_refusal_still_aborts_even_with_ocr_fallback(prepared: Prepared) -> None:
    fake = scripted(ProviderRefused("Gemini stopped generation: SAFETY"))
    with pytest.raises(ProviderRefused):
        asyncio.run(extract_pages(prepared, fake, dpi=72, ocr_fallback=fake_ocr([])))


# --- no mandatory inference, stable hashes ----------------------------------------------


def test_ocr_fallback_cannot_carry_fields_or_obligations() -> None:
    base = OcrFallbackPage(page_number=1, engine="e", dpi=300, psm=3, language="eng",
                           recitation=diag()).model_dump(mode="json")  # fmt: skip
    for extra in ({"mandatory_under_policy": True}, {"fields": []}, {"required_as_shown": True}):
        with pytest.raises(ValidationError):
            OcrFallbackPage.model_validate(base | extra)
    for key, value in (("verification_status", "verified"), ("review_status", "approved")):
        with pytest.raises(ValidationError):
            OcrFallbackPage.model_validate(base | {key: value})
    with pytest.raises(ValidationError):
        OcrFallbackLine.model_validate(
            {"region": "full", "text": "x", "confidence": 1, "low_confidence": False,
             "mandatory_under_policy": None}  # fmt: skip
        )


def test_schemas_without_the_new_keys_hash_exactly_as_before(prepared: Prepared) -> None:
    outs = asyncio.run(
        extract_pages(prepared, scripted(page_extraction(1), page_extraction(2)), dpi=72)
    )
    s = build_schema(prepared, outs, provider=FakeMultimodalProvider([]), dpi=72)
    data = s.model_dump(mode="json")
    assert "ocr_fallback" not in data  # omitted when empty
    legacy = json.loads(json.dumps(data))
    for p in legacy["provenance"]["pages"]:
        p.pop("outcome")  # a row stored before page outcomes existed
    reloaded = FormSchema.model_validate(legacy)
    assert reloaded.model_dump(mode="json") == legacy  # no key reappears on re-dump
    assert schema_sha256(reloaded) == schema_sha256(FormSchema.model_validate(legacy))


# --- OCR runner: failures are recorded, never hidden ------------------------------------


def test_ocr_setup_failure_is_recorded(prepared: Prepared, tmp_path: Path) -> None:
    settings = Settings(_env_file=EXAMPLE_ENV, TESSERACT_CMD=tmp_path / "missing.exe")  # type: ignore[call-arg]
    page = ocr_fallback_page(
        prepared.pdf_path, 1, diag(), prepared.approval.sha256, settings=settings
    )
    assert page.lines == [] and page.errors and page.engine == "tesseract (unavailable)"
    assert page.review_status == "requires_human_review"
