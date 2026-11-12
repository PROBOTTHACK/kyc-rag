"""M3 S5 end to end on the real ingested corpus, with the report stored in Postgres.

BM25 over the real chunks (the fixture's embedder is fake), m1 form schemas, scripted
intake and synthesis drafts (no live LLM), the real output validation, and the real
insert-only report store. Gemini is spied and must never be called.
"""

import asyncio
import json
import re
from datetime import timedelta
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.orm import Session, sessionmaker

from app.db.workflow import PostgresReportStore, create_session
from app.graph.builder import GraphDeps, build_graph, run_config
from app.graph.inputs import start_input
from app.graph.intake import ExtractedValue, IntakeExtraction
from app.graph.report import FALLBACK
from app.graph.state import DISCLAIMER
from app.graph.synthesis import ChecklistDraft, DraftRequirement
from app.llm.fake import FakeLLMProvider
from app.llm.provider import LLMRequest, LLMResult
from tests.integration.test_graph_s4_corpus import no_gemini, world  # noqa: F401 (fixtures)

pytestmark = pytest.mark.integration


class DraftCitingSupplied(FakeLLMProvider):
    """Intake answer first; then a draft proposing MANDATORY for each of the first
    supplied evidence ids (the adversarial case: the validator must cut it down)."""

    def __init__(self, intake: IntakeExtraction, cite: int = 3) -> None:
        super().__init__([intake.model_dump_json()])
        self.cite = cite

    async def generate_structured(self, request: LLMRequest, schema: Any) -> LLMResult[Any]:
        if schema is ChecklistDraft:
            ids = re.findall(r'<evidence id="([^"]+)"', request.user)[: self.cite]
            items = [
                DraftRequirement(
                    name=f"Item {i + 1}",
                    explanation="As cited.",
                    applicability="MANDATORY",
                    evidence_ids=[x],
                )
                for i, x in enumerate(ids)
            ]
            self.responses.append(ChecklistDraft(requirements=items).model_dump_json())
        return await super().generate_structured(request, schema)


def run(
    stores: tuple[Any, Any],
    corpus_db: sessionmaker[Session],
    query: str,
    intake: IntakeExtraction,
) -> tuple[dict[str, Any], PostgresReportStore]:
    retriever, corpus = stores
    with corpus_db() as s:
        session_id = str(create_session(s, ttl=timedelta(hours=1), data_class="synthetic_demo").id)
        s.commit()
    store = PostgresReportStore(corpus_db)
    deps = GraphDeps(
        retriever=retriever,
        corpus=corpus,
        reports=store,
        retrieval_mode="bm25",
        llm=DraftCitingSupplied(intake),
    )
    graph = build_graph(deps, checkpointer=InMemorySaver())
    out = asyncio.run(
        graph.ainvoke(
            start_input(query, session_id=session_id, run_id="run-1"),
            run_config(deps, thread_id=session_id),
        )
    )
    return dict(out), store


def value(v: str, span: str) -> ExtractedValue:
    return ExtractedValue(value=v, evidence_span=span)


def test_trust_report_is_stored_with_rbi_para_35_as_the_only_mandatory_basis(
    world: tuple[Any, Any], corpus_db: sessionmaker[Session]  # noqa: F811 (pytest fixture)
) -> None:
    out, store = run(
        world,
        corpus_db,
        "documents required to open a current account of a trust",
        IntakeExtraction(
            entity_type=value("TRUST", "a trust"),
            residency=ExtractedValue(),
            account_type=value("CURRENT_ACCOUNT", "a current account"),
        ),
    )
    stored = store.get(out["report_id"])
    assert stored is not None and stored.model_dump(mode="json") == out["report"]
    # M4 S5: NLI is disabled, so claims are not verified: DEGRADED, nothing shown MANDATORY.
    # The M3 gate's own result is kept as `applicability_before` and checked as before.
    assert stored.outcome == "DEGRADED" and stored.human_review_required
    assert [d.component for d in stored.degradations] == ["verification"]
    # M4 S6: no conflict scan without NLI, and none is claimed
    assert stored.verification_meta is not None
    assert stored.verification_meta.contradiction_scan == "not_run"
    assert stored.verification_meta.contradiction_scan_size == 0 and not stored.source_conflicts
    m3 = {r.requirement_id: r.applicability_before for r in stored.claim_verification or []}
    for item in stored.checklist or []:
        cited = item.citations[0]
        assert item.applicability != "MANDATORY"
        if m3[item.requirement_id] == "MANDATORY":
            assert (cited.document_key, cited.evidence_eligibility, cited.match_scope) == (
                "rbi_kyc_directions_2025",
                "primary",
                "entity",
            )
        else:
            assert item.proposed_applicability == "MANDATORY"  # downgraded, never invented
    assert any(
        c.document_key == "rbi_kyc_directions_2025" and c.paragraph == "35" and c.page == 38
        for item in stored.checklist or []
        for c in item.citations
    )
    assert stored.user_message.endswith(DISCLAIMER)


def test_llp_report_has_no_mandatory_item(
    world: tuple[Any, Any], corpus_db: sessionmaker[Session]  # noqa: F811 (pytest fixture)
) -> None:
    out, store = run(
        world,
        corpus_db,
        "What KYC documents does an LLP need to open a current account?",
        IntakeExtraction(
            entity_type=value("LLP", "an LLP"),
            residency=ExtractedValue(),
            account_type=value("CURRENT_ACCOUNT", "a current account"),
        ),
    )
    stored = store.get(out["report_id"])
    assert stored is not None
    assert all(i.applicability != "MANDATORY" for i in stored.checklist or [])
    assert all(r.applicability_before != "MANDATORY" for r in stored.claim_verification or [])
    # M4 S5: a checklist means claim verification ran with NLI disabled -> DEGRADED
    assert stored.outcome in ("DEGRADED", "INSUFFICIENT_EVIDENCE")
    if stored.outcome == "DEGRADED":
        assert [d.component for d in stored.degradations] == ["verification"]
    if stored.outcome == "INSUFFICIENT_EVIDENCE":
        assert stored.user_message.startswith(FALLBACK)
    assert "mandatory_under_policy" not in json.dumps(out)
