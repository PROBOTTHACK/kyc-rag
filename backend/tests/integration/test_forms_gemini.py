"""M2 G3 on real Postgres: the Gemini form pipeline with a scripted (fake) provider.

Uses the full-corpus throwaway database. Covers: insert with page-level provenance and
corroboration against the real stored M1 text; zero provider calls on rerun; refusal of
the NetBanking reference, unlisted and hash-mismatched documents before rendering; the
degraded (partial) path; provider errors storing nothing; method separation (m1_chunks
schemas unchanged, `forms show --method`); and the database guards on Gemini rows.
"""

import asyncio
import json
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.forms import load_form_schema
from app.db.models import FormSchemaRow
from app.forms.approval import load_approvals
from app.forms.pipeline import extract_with_provider, prepare
from app.ingestion import cli
from app.llm.fake import FakeMultimodalProvider, scripted
from app.llm.fallback import FallbackMultimodalProvider
from app.llm.multimodal import (
    Citation,
    ProviderRateLimited,
    ProviderRecitation,
    ProviderRefused,
    ProviderTimeout,
    RecitationDiagnostics,
)
from app.retrieval.qdrant_store import ChunkStore
from app.schemas.forms import ExtractedField, ExtractedSection, FormExtraction

pytestmark = pytest.mark.integration
KEY = "hdfc_resident_rekyc_dormant"  # 2 native-text pages
GEMINI = "gemini_multimodal"


def ex(*labels: str, page: int) -> FormExtraction:
    return FormExtraction(
        sections=[
            ExtractedSection(
                name="DETAILS",
                is_declaration=False,
                fields=[
                    ExtractedField(printed_label=lbl, field_type="text", page_number=page)
                    for lbl in labels
                ],
            )
        ]
    )


@pytest.fixture(scope="module")
def stored(corpus_db: sessionmaker[Session], corpus_store: ChunkStore) -> sessionmaker[Session]:
    assert cli._forms_extract(get_settings(), [], sessions=corpus_db) == 0  # m1_chunks
    return corpus_db


@pytest.fixture
def db(stored: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    yield stored
    with stored() as s:  # every test starts without Gemini rows
        s.execute(sa.delete(FormSchemaRow).where(FormSchemaRow.extraction_method == GEMINI))
        s.commit()


def rows(sessions: sessionmaker[Session], method: str) -> list[tuple[str, str]]:
    with sessions() as s:
        return [
            (r.form_key, r.schema_sha256)
            for r in s.scalars(
                sa.select(FormSchemaRow)
                .where(FormSchemaRow.extraction_method == method)
                .order_by(FormSchemaRow.form_key)
            )
        ]


def extract(sessions: sessionmaker[Session], keys: list[str], fake: FakeMultimodalProvider) -> int:
    return cli._forms_extract_gemini(get_settings(), keys, sessions=sessions, provider=fake)


def test_insert_corroborate_and_rerun_without_calls(
    db: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    m1_before = rows(db, "m1_chunks")
    fake = scripted(
        ex("*SURNAME", "Address Proof", "Mother's Maiden Name", page=1),
        ex("Customer Name:", page=2),
    )
    assert extract(db, [KEY], fake) == 0
    out = capsys.readouterr().out
    assert "inserted" in out and "corroborated 3, uncorroborated 1" in out
    assert len(fake.calls) == 2  # one request per page
    with db() as s:
        schema = load_form_schema(s, KEY, extraction_method=GEMINI)
    assert schema is not None and schema.extraction_method == GEMINI
    fields = {f.printed_label: f for sec in schema.sections for f in sec.fields}
    assert fields["*SURNAME"].required_as_shown is True
    assert fields["*SURNAME"].verification_status == "corroborated"
    assert fields["*SURNAME"].chunk_id and fields["*SURNAME"].page_number == 1
    assert fields["Mother's Maiden Name"].verification_status == "uncorroborated"
    assert fields["Customer Name:"].page_number == 2
    assert all(f.mandatory_under_policy is None for f in fields.values())
    assert schema.provenance is not None
    assert [p.page_number for p in schema.provenance.pages] == [1, 2]
    assert rows(db, "m1_chunks") == m1_before  # deterministic schemas untouched
    with db() as s:  # the default load is still the deterministic schema
        m1 = load_form_schema(s, KEY)
    assert m1 is not None and m1.extraction_method == "m1_chunks"

    stored_rows = rows(db, GEMINI)
    empty = FakeMultimodalProvider([])  # any call would fail the test
    assert extract(db, [KEY], empty) == 0
    assert "(stored; no provider call)" in capsys.readouterr().out
    assert empty.calls == [] and rows(db, GEMINI) == stored_rows

    assert cli._forms_show(get_settings(), KEY, as_json=True, version=None, sessions=db,
                           method=GEMINI) == 0  # fmt: skip
    raw = capsys.readouterr().out
    shown = json.loads(raw[raw.index("{") :])  # after the database status line
    assert shown["extraction_method"] == GEMINI and shown["provenance"]["model_id"]


@pytest.mark.parametrize(
    "keys",
    [
        ["hdfc_netbanking_corporate_application"],  # reference document: never sent
        ["rbi_kyc_directions_2025"],
        [KEY, "hdfc_netbanking_corporate_application"],  # one refusal blocks the batch
        ["no_such_document"],
    ],
)
def test_refusals_happen_before_any_render_or_call(
    db: sessionmaker[Session], keys: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeMultimodalProvider([])
    assert extract(db, keys, fake) == 2
    assert "nothing was sent" in capsys.readouterr().out
    assert fake.calls == [] and rows(db, GEMINI) == []


def test_hash_mismatch_in_the_registry_is_refused(db: sessionmaker[Session]) -> None:
    approvals = load_approvals()
    approvals[KEY] = approvals[KEY].model_copy(update={"sha256": "0" * 64})
    fake = FakeMultimodalProvider([])
    code = cli._forms_extract_gemini(
        get_settings(), [KEY], sessions=db, provider=fake, approvals=approvals
    )
    assert code == 2 and fake.calls == [] and rows(db, GEMINI) == []


def test_invalid_page_output_degrades_with_flags(
    db: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    fake = scripted(ex("SURNAME", page=1), "{bad", '{"sections": [{"name": 1}]}')
    assert extract(db, [KEY], fake) == 0
    assert len(fake.calls) == 3  # page 2: first request + one re-ask, never more
    with db() as s:
        schema = load_form_schema(s, KEY, extraction_method=GEMINI)
    assert schema is not None
    assert {"partial_extraction", "invalid_output_page_2"} <= set(schema.flags)
    assert {sec.page_start for sec in schema.sections} == {1}
    assert "pages ok 1/2" in capsys.readouterr().out


def test_no_valid_output_and_provider_errors_store_nothing(db: sessionmaker[Session]) -> None:
    assert extract(db, [KEY], scripted("{", "{", "{", "{")) == 1
    assert extract(db, [KEY], scripted(ex("SURNAME", page=1), ProviderTimeout("t"))) == 1
    assert rows(db, GEMINI) == []


def test_database_guards_apply_to_gemini_rows(db: sessionmaker[Session]) -> None:
    assert extract(db, [KEY], scripted(ex("SURNAME", page=1), ex("Customer Name", page=2))) == 0
    with db() as s:
        row = s.scalar(sa.select(FormSchemaRow).where(FormSchemaRow.extraction_method == GEMINI))
        assert row is not None
        data = json.loads(json.dumps(row.schema_json))
    data["sections"][0]["fields"][0]["mandatory_under_policy"] = True
    data["extractor_version"] = 3
    with db() as s, pytest.raises(sa.exc.IntegrityError, match="ck_form_schemas_field_structure"):
        s.execute(
            sa.insert(FormSchemaRow).values(
                id=sa.func.gen_random_uuid(),
                document_id=row.document_id,
                form_key=row.form_key,
                document_sha256=row.document_sha256,
                document_role=row.document_role,
                evidence_eligibility=row.evidence_eligibility,
                extraction_method=GEMINI,
                extractor_version=3,
                schema_sha256=row.schema_sha256,
                schema_json=data,
                section_count=row.section_count,
                field_count=row.field_count,
                required_as_shown_count=row.required_as_shown_count,
                text_block_count=0,
                quality_flags=row.quality_flags,
            )
        )
        s.commit()


def _exhausted() -> ProviderRateLimited:
    err = ProviderRateLimited("quota (after 5 attempts)")
    err.transient = True
    return err


def test_fallback_model_answers_and_is_recorded_per_page(
    db: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    primary = scripted(ex("SURNAME", page=1), _exhausted())
    primary.model_id = "primary-model"
    fallback = FakeMultimodalProvider(
        [ex("Customer Name", page=2).model_dump_json()], model_id="fallback-model"
    )
    assert extract(db, [KEY], FallbackMultimodalProvider(primary, fallback)) == 0
    assert "(fallback pages 1, recitation-refused pages 0" in capsys.readouterr().out
    with db() as s:
        schema = load_form_schema(s, KEY, extraction_method=GEMINI)
    assert schema is not None and schema.provenance is not None
    assert [(p.page_number, p.model_id) for p in schema.provenance.pages] == [
        (1, "primary-model"),
        (2, "fallback-model"),
    ]


def test_both_models_exhausted_store_nothing(db: sessionmaker[Session]) -> None:
    primary = scripted(_exhausted())
    fallback = scripted(_exhausted())
    assert extract(db, [KEY], FallbackMultimodalProvider(primary, fallback)) == 1
    assert rows(db, GEMINI) == []


SCANNED = "hdfc_non_individual_account_opening"  # 2 scanned fold-out pages


def _recitation(model: str = "fake-multimodal-1") -> ProviderRecitation:
    return ProviderRecitation(
        "Gemini stopped generation: RECITATION",
        RecitationDiagnostics(
            provider="gemini",
            model_id=model,
            finish_reason="RECITATION",
            citations=[Citation(uri="https://example.org/source", start_index=0, end_index=9)],
            partial_output_chars=12,
            partial_output_sha256="a" * 64,
        ),
    )


def test_all_pages_refused_is_persisted_with_local_ocr_for_review(
    db: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    m1_before = rows(db, "m1_chunks")
    fake = scripted(_recitation(), _recitation())
    assert extract(db, [SCANNED], fake) == 0
    out = capsys.readouterr().out
    assert "inserted" in out and "recitation-refused pages 2" in out
    assert len(fake.calls) == 2  # one request per page, never re-asked
    with db() as s:
        schema = load_form_schema(s, SCANNED, extraction_method=GEMINI)
        row = s.scalar(
            sa.select(FormSchemaRow).where(
                FormSchemaRow.form_key == SCANNED, FormSchemaRow.extraction_method == GEMINI
            )
        )
    assert schema is not None and row is not None
    assert schema.sections == [] and schema.field_count == 0 and row.field_count == 0
    expected_flags = {
        "gemini_extraction_refused",
        "no_field_structure",
        "partial_extraction",
        "recitation_refused",
        "ocr_fallback_requires_review",
        "scanned_document",
        "recitation_refused_page_1",
        "recitation_refused_page_2",
    }
    assert expected_flags <= set(schema.flags)
    assert schema.provenance is not None
    assert [p.outcome for p in schema.provenance.pages] == ["recitation_refused"] * 2
    assert [fb.page_number for fb in schema.ocr_fallback] == [1, 2]
    for fb in schema.ocr_fallback:  # real local Tesseract (M1's cache), verbatim
        assert fb.source == "local_tesseract_ocr" and fb.review_status == "requires_human_review"
        assert fb.engine.startswith("tesseract ") and not fb.errors
        assert {r.label for r in fb.regions} == {"left", "right"}  # fold-out halves apart
        assert all(r.from_cache for r in fb.regions)  # identical to M1's OCR
        assert fb.lines and all(0 <= ln.confidence <= 100 for ln in fb.lines)
        assert fb.recitation.citations[0].uri == "https://example.org/source"
    assert rows(db, "m1_chunks") == m1_before

    empty = FakeMultimodalProvider([])
    assert extract(db, [SCANNED], empty) == 0  # stored: no provider call on rerun
    assert empty.calls == [] and "(stored; no provider call)" in capsys.readouterr().out


def test_mixed_refused_and_extracted_pages(db: sessionmaker[Session]) -> None:
    fake = scripted(_recitation(), ex("Customer Name", page=2))
    assert extract(db, [KEY], fake) == 0
    with db() as s:
        schema = load_form_schema(s, KEY, extraction_method=GEMINI)
    assert schema is not None and schema.field_count == 1
    assert [fb.page_number for fb in schema.ocr_fallback] == [1]
    assert "gemini_extraction_refused" not in schema.flags
    assert schema.provenance is not None
    assert [p.outcome for p in schema.provenance.pages] == ["recitation_refused", "extracted"]


def test_safety_refusal_still_stores_nothing(db: sessionmaker[Session]) -> None:
    assert extract(db, [KEY], scripted(ProviderRefused("Gemini stopped generation: SAFETY"))) == 1
    assert rows(db, GEMINI) == []


def test_v1_and_v2_coexist_immutably_and_are_reported_separately(
    db: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    settings = get_settings()
    with db() as s:  # a v1 record, as stored before the v2 correction
        prepared = prepare(
            s,
            KEY,
            approvals=load_approvals(),
            corpus_dir=settings.CORPUS_DIR,
            manifest_path=settings.CORPUS_MANIFEST,
        )
        v1 = asyncio.run(
            extract_with_provider(
                s,
                prepared,
                scripted(ex("SURNAME", page=1), ex("Customer Name", page=2)),
                extractor_version=1,
            )
        )
        s.commit()
    assert v1.outcome is not None and v1.outcome.extractor_version == 1
    with db() as s:
        v1_row = s.scalar(
            sa.select(FormSchemaRow).where(
                FormSchemaRow.form_key == KEY,
                FormSchemaRow.extraction_method == GEMINI,
                FormSchemaRow.extractor_version == 1,
            )
        )
        assert v1_row is not None
        v1_snapshot = (v1_row.id, v1_row.schema_sha256, json.dumps(v1_row.schema_json))

    cli._forms_versions(settings, sessions=db)
    report = capsys.readouterr().out
    assert "gemini v1: stored" in report and "gemini v2 (current): NOT extracted" in report

    repeats = ex("Name", "Name", "Name", page=1)  # rejected under v1, valid under v2
    fake = scripted(repeats, ex("Customer Name", page=2))
    assert extract(db, [KEY], fake) == 0  # current version = v2: a new row
    assert "inserted  v2" in capsys.readouterr().out and len(fake.calls) == 2
    with db() as s:
        latest = load_form_schema(s, KEY, extraction_method=GEMINI)
        first = load_form_schema(s, KEY, extraction_method=GEMINI, extractor_version=1)
        rows_v = s.scalars(
            sa.select(FormSchemaRow).where(
                FormSchemaRow.form_key == KEY, FormSchemaRow.extraction_method == GEMINI
            )
        ).all()
        again = next(r for r in rows_v if r.extractor_version == 1)
        assert (again.id, again.schema_sha256, json.dumps(again.schema_json)) == v1_snapshot
    assert sorted(r.extractor_version for r in rows_v) == [1, 2]
    assert latest is not None and latest.extractor_version == 2
    assert [f.printed_label for f in latest.sections[0].fields] == ["Name"] * 3
    assert "repeated_labels" in latest.flags
    assert first is not None and first.extractor_version == 1

    cli._forms_versions(settings, sessions=db)
    report = capsys.readouterr().out
    line = next(ln for ln in report.splitlines() if KEY in ln)
    assert "gemini v1: stored" in line and "gemini v2: stored" in line and "NOT" not in line
    other = next(ln for ln in report.splitlines() if "hdfc_nri_rekyc" in ln)
    assert "gemini v2 (current): NOT extracted" in other  # never claimed without a row

    empty = FakeMultimodalProvider([])
    assert extract(db, [KEY], empty) == 0 and empty.calls == []  # idempotent at v2
