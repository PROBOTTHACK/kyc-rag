"""M4 S8 end to end on the real ingested corpus (Postgres + the M1 chunks), stored reports.

1. NLI disabled (production): DEGRADED, report v2, every claim `not_verified`, conflict scan
   `not_run`, one open `not_verified` review case per checklist item, `check_report` and
   `check-workflow` clean.
2. The enabled path with the scripted `FakeNLI` (tests only; no model is built or loaded):
   premises from the real stored chunks, a real bounded scan over the retrieved passages,
   and a report that passes every M4 acceptance rule.
3. An indicative NLI-off latency sample (printed, never asserted: no budget gate, S8-3).

Scripted intake/synthesis (no live LLM); Gemini is spied and must never be called.

Known pre-existing M3 issue found here (S8; recorded, not fixed): a citation of a retrieved
CHILD chunk carries the page range of its expanded PARENT paragraph, while `check_report`
looks the child row up, so such an item fails `citation_mismatch`. The M3 checklist already
holds that citation before verification runs. The tests require that these are the ONLY
acceptance problems, so any M4 problem still fails.
"""

import asyncio
import json
import statistics
import time
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.orm import Session, sessionmaker

import app.verification.nli as nli_module
from app.config import get_settings
from app.db.workflow import PostgresReportStore, create_session
from app.graph.builder import GraphDeps, build_graph, run_config
from app.graph.inputs import start_input
from app.graph.intake import ExtractedValue, IntakeExtraction
from app.graph.review import review_cases
from app.graph.state import ComplianceReport
from app.verification.decide import Thresholds
from app.verification.nli import FakeNLI
from app.workflow.acceptance import check_report, db_chunk_lookup
from app.workflow.retention import integrity
from tests.integration.test_graph_s4_corpus import no_gemini, world  # noqa: F401 (fixtures)
from tests.integration.test_graph_s5_corpus import DraftCitingSupplied

pytestmark = pytest.mark.integration

TRUST_QUERY = "documents required to open a current account of a trust"
TRUST = IntakeExtraction(
    entity_type=ExtractedValue(value="TRUST", evidence_span="a trust"),
    residency=ExtractedValue(),
    account_type=ExtractedValue(value="CURRENT_ACCOUNT", evidence_span="a current account"),
)
SCRIPTED_T = Thresholds(0.9, 0.9, "s8-scripted-test-only")
ENTAIL = (8.0, 0.0, 0.0)


@pytest.fixture(autouse=True)
def no_real_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*a: Any, **k: Any) -> Any:
        raise AssertionError("S8 must never build or load an NLI model")

    monkeypatch.setattr(nli_module.CrossEncoderNLI, "__init__", refuse)


def run(
    stores: tuple[Any, Any], corpus_db: sessionmaker[Session], **deps_kw: Any
) -> tuple[dict[str, Any], PostgresReportStore, float]:
    retriever, corpus = stores
    with corpus_db() as s:
        sid = str(create_session(s, ttl=timedelta(hours=1), data_class="synthetic_demo").id)
        s.commit()
    store = PostgresReportStore(corpus_db)
    deps = GraphDeps(retriever=retriever, corpus=corpus, reports=store, retrieval_mode="bm25",
                     llm=DraftCitingSupplied(TRUST), **deps_kw)  # fmt: skip
    graph = build_graph(deps, checkpointer=InMemorySaver())
    started = time.perf_counter()
    out = asyncio.run(graph.ainvoke(start_input(TRUST_QUERY, session_id=sid, run_id="run-1"),
                                    run_config(deps, thread_id=sid)))  # fmt: skip
    return dict(out), store, (time.perf_counter() - started) * 1000


def accepted(report: dict[str, Any], corpus_db: sessionmaker[Session]) -> list[str]:
    with corpus_db() as db:
        return check_report(report, db_chunk_lookup(db)).problems


def m3_parent_page_citations(
    items: list[dict[str, Any]], corpus_db: sessionmaker[Session]
) -> list[str]:
    """The known M3 issue, computed independently: items whose cited page is outside the
    cited chunk's own stored pages (the page came from the expanded parent)."""
    out = []
    with corpus_db() as db:
        for n, item in enumerate(items):
            for c in item["citations"]:
                row = db.execute(sa.text(
                    "SELECT page_number, coalesce(page_end, page_number) FROM chunks "
                    "WHERE id = :i"), {"i": c["evidence_id"]}).first()  # fmt: skip
                assert row is not None
                if not row[0] <= c["page"] <= row[1]:
                    out.append(f"item_{n}:citation_mismatch")
    return out


def only_the_known_m3_issue(out: dict[str, Any], corpus_db: sessionmaker[Session]) -> None:
    report = out["report"]
    known = m3_parent_page_citations(report["checklist"] or [], corpus_db)
    assert accepted(report, corpus_db) == known  # no M4 (or other) acceptance problem
    # and it predates M4: the M3 checklist in state holds the same citations
    m3 = {i["requirement_id"]: i["citations"] for i in out["checklist"] or []}
    for item in report["checklist"] or []:
        assert item["citations"] == m3[item["requirement_id"]]


def test_nli_disabled_on_the_real_corpus(
    world: tuple[Any, Any], corpus_db: sessionmaker[Session]  # noqa: F811
) -> None:
    out, store, _ = run(world, corpus_db)
    stored = store.get(out["report_id"])
    assert stored is not None and stored.model_dump(mode="json") == out["report"]
    report = out["report"]
    assert report["outcome"] == "DEGRADED" and report["report_version"] == 2
    assert [d["component"] for d in report["degradations"]] == ["verification"]
    meta = report["verification_meta"]
    assert (meta["status"], meta["contradiction_scan"]) == ("not_configured", "not_run")
    assert meta["pairs_scored"] == meta["contradiction_scan_size"] == 0
    assert report["source_conflicts"] == [] and report["unverified_items"] == []
    items = report["checklist"]
    assert items and all(i["verification_status"] == "not_verified" for i in items)
    assert all(i["applicability"] != "MANDATORY" for i in items)
    # one open not_verified case per checklist item, exactly as the report implies
    cases = store.review_cases_for(out["report_id"])
    assert cases is not None and len(cases) == len(items)
    assert {(c["requirement_id"], c["reason"], c["status"]) for c in cases} == {
        (i["requirement_id"], "not_verified", "open") for i in items
    }
    assert [c["id"] for c in cases] == [c.id for c in review_cases(stored)]
    only_the_known_m3_issue(out, corpus_db)
    with corpus_db() as db:
        found = integrity(db, get_settings())
    assert found.review_case_problems == [] and found.review_cases >= len(items)


def test_the_enabled_path_with_a_scripted_scorer_on_the_real_corpus(
    world: tuple[Any, Any], corpus_db: sessionmaker[Session]  # noqa: F811
) -> None:
    scorer = FakeNLI(default=ENTAIL)
    out, store, _ = run(world, corpus_db, nli=scorer, nli_thresholds=SCRIPTED_T)
    report = out["report"]
    ComplianceReport.model_validate(report)
    meta = report["verification_meta"]
    assert meta["status"] in ("ran", "partial") and meta["model_id"] == "fake-nli"
    assert meta["contradiction_scan"] == "bounded" and meta["pairs_scored"] > 0
    # premises came from the real stored chunks: every scored premise is stored corpus text
    premises = sorted({p.premise for call in scorer.calls for p in call})
    assert premises
    with corpus_db() as db:
        for text in premises:
            assert db.execute(sa.text("SELECT count(*) FROM chunks WHERE strpos(content, :t) > 0"),
                              {"t": text}).scalar_one() > 0  # fmt: skip
    only_the_known_m3_issue(out, corpus_db)
    stored = store.get(out["report_id"])
    assert stored is not None
    cases = store.review_cases_for(out["report_id"])
    assert [c["id"] for c in cases or []] == [c.id for c in review_cases(stored)]
    with corpus_db() as db:
        assert integrity(db, get_settings()).review_case_problems == []


def test_nli_off_latency_sample(
    world: tuple[Any, Any], corpus_db: sessionmaker[Session]  # noqa: F811
) -> None:
    """Indicative only (laptop CPU; scripted LLM; BM25): printed, never a gate (S8-3)."""
    node_ms, run_ms = [], []
    for _ in range(7):
        out, _, wall = run(world, corpus_db)
        node_ms.append(float(out["verification_meta"]["timings_ms"]["total_ms"]))
        run_ms.append(wall)

    def summary(xs: list[float]) -> dict[str, float]:
        ordered = sorted(xs)
        return {"n": len(xs), "p50_ms": round(statistics.median(ordered), 1),
                "p95_ms": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 1),
                "max_ms": round(ordered[-1], 1)}  # fmt: skip

    sample = {"claim_verification_node": summary(node_ms), "whole_graph_run": summary(run_ms)}
    print("M4_S8_LATENCY " + json.dumps(sample))
    assert len(node_ms) == 7
