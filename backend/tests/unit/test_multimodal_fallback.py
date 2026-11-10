"""M2 G3 follow-up: primary -> fallback multimodal provider (no network, no key)."""

import asyncio
import hashlib
import uuid
from pathlib import Path
from typing import Any

import pymupdf
import pytest
from pydantic import SecretStr

from app.db.models import Document
from app.forms.approval import ApprovedForm
from app.forms.pipeline import Prepared, build_schema, extract_pages
from app.llm.fake import FakeMultimodalProvider, scripted
from app.llm.fallback import FallbackMultimodalProvider
from app.llm.gemini import GeminiProvider, multimodal_from_settings
from app.llm.multimodal import (
    MultimodalRequest,
    ProviderRateLimited,
    ProviderRefused,
    ProviderUnavailable,
)
from app.schemas.forms import FormExtraction
from tests.unit.test_gemini_provider import KEY, VALID, FakeTime, api_error, response, settings

PRIMARY, FALLBACK = "gemini-3.8-flash", "gemini-3.5-flash-lite"


def gemini(model: str, script: list[Any], t: FakeTime, **kw: Any) -> GeminiProvider:
    async def generate(images: list[bytes], req: Any, schema: Any, timeout_s: float) -> Any:
        nxt = script.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    return GeminiProvider(
        model_id=model,
        api_key=SecretStr(KEY),
        rpm=10,
        sleep=t.sleep,
        clock=t.clock,
        generate=generate,
        **kw,
    )


def pair(primary: list[Any], fallback: list[Any]) -> tuple[FallbackMultimodalProvider, FakeTime]:
    t = FakeTime()
    p = gemini(PRIMARY, primary, t)
    f = gemini(FALLBACK, fallback, t, limiter=p._limiter)
    return FallbackMultimodalProvider(p, f), t


def call(provider: Any) -> Any:
    req = MultimodalRequest(prompt_id="p", prompt_version="1", instruction="go", pages=[])
    return asyncio.run(provider.analyze_pages([b"png"], req, FormExtraction))


def test_transient_exhaustion_falls_back_and_reports_the_fallback_model() -> None:
    fb, t = pair([api_error(429)] * 5, [response(VALID.model_dump_json())])
    r = call(fb)
    assert r.parsed == VALID and r.model_id == FALLBACK
    assert fb.attempts_made == 6 and fb.model_id == PRIMARY
    assert t.sleeps == [2.0, 4.0, 8.0, 16.0]  # the primary's waits; fallback answered at once


def test_both_models_exhausted_raise_a_typed_error() -> None:
    fb, t = pair([api_error(503)] * 5, [api_error(503)] * 5)
    with pytest.raises(ProviderUnavailable, match="after 5 attempts"):
        call(fb)
    assert fb.attempts_made == 10 and t.sleeps == [2.0, 4.0, 8.0, 16.0] * 2


@pytest.mark.parametrize(
    ("primary", "exc"),
    [
        ([api_error(400)], ProviderUnavailable),
        ([api_error(401)], ProviderUnavailable),
        ([api_error(403)], ProviderUnavailable),
        ([response("{}", finish="SAFETY")], ProviderRefused),
    ],
)
def test_no_fallback_on_refusal_or_rejected_request(
    primary: list[Any], exc: type[Exception]
) -> None:
    fb, _ = pair(primary, [response(VALID.model_dump_json())])
    with pytest.raises(exc):
        call(fb)
    assert fb.fallback.attempts_made == 0  # type: ignore[attr-defined]


def test_no_fallback_on_invalid_output() -> None:
    fb, _ = pair([response("not json")], [response(VALID.model_dump_json())])
    r = call(fb)
    assert r.parsed is None and r.model_id == PRIMARY
    assert fb.fallback.attempts_made == 0  # type: ignore[attr-defined]


def test_fallback_shares_the_rpm_limiter() -> None:
    t = FakeTime()
    p = gemini(PRIMARY, [api_error(429)] * 5, t)
    p.rpm = 2
    p._limiter.rpm = 2
    f = gemini(FALLBACK, [response(VALID.model_dump_json())], t, limiter=p._limiter)
    call(FallbackMultimodalProvider(p, f))
    assert f._limiter is p._limiter
    assert t.now >= 120.0  # 6 attempts at 2/min: the fallback attempt waited its turn


def test_factory_wraps_only_when_a_distinct_fallback_is_configured() -> None:
    wrapped = multimodal_from_settings(settings(MULTIMODAL_FALLBACK_MODEL=FALLBACK))
    assert isinstance(wrapped, FallbackMultimodalProvider)
    assert (wrapped.primary.model_id, wrapped.fallback.model_id) == (PRIMARY, FALLBACK)
    assert wrapped.fallback._limiter is wrapped.primary._limiter  # type: ignore[attr-defined]
    for value in ("", PRIMARY):
        alone = multimodal_from_settings(settings(MULTIMODAL_FALLBACK_MODEL=value))
        assert isinstance(alone, GeminiProvider)
    with pytest.raises(ProviderUnavailable, match="GEMINI_LIVE_ENABLED"):
        multimodal_from_settings(
            settings(GEMINI_LIVE_ENABLED=False, MULTIMODAL_FALLBACK_MODEL=FALLBACK)
        )


def test_page_provenance_records_the_model_that_answered(tmp_path: Path) -> None:
    path = tmp_path / "f.pdf"
    with pymupdf.open() as doc:
        for _ in range(2):
            doc.new_page().insert_text((72, 72), "SURNAME")
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
    prepared = Prepared(document, approval, path, {})
    exhausted = ProviderRateLimited("quota (after 5 attempts)")
    exhausted.transient = True
    page1 = FormExtraction.model_validate_json(VALID.model_dump_json())
    page2 = FormExtraction.model_validate_json(
        VALID.model_dump_json().replace('"page_number":1', '"page_number":2')
    )
    primary = scripted(page1, exhausted)
    primary.model_id = PRIMARY
    fallback = FakeMultimodalProvider([page2.model_dump_json()], model_id=FALLBACK)
    fb = FallbackMultimodalProvider(primary, fallback)
    outs = asyncio.run(extract_pages(prepared, fb, dpi=72))
    schema = build_schema(prepared, outs, provider=fb, dpi=72)
    assert schema.provenance is not None and schema.provenance.model_id == PRIMARY
    assert [(p.page_number, p.model_id) for p in schema.provenance.pages] == [
        (1, PRIMARY),
        (2, FALLBACK),
    ]
