"""M3 S4 on the real ingested corpus (throwaway Postgres + Qdrant from `make up`).

The corpus fixture embeds with `FakeEmbedder`, so dense rankings mean nothing there: these
tests run the real BM25 retriever (`retrieval_mode="bm25"`) over the real stored chunks,
with deterministic m1 form schemas extracted into the throwaway database. The intake LLM
is the scripted fake; no live Groq or Gemini call is possible (spied).
"""

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.forms import extract_and_save, load_form_schema
from app.forms.extract import extractable_document_keys
from app.graph.builder import GraphDeps, build_graph, run_config
from app.graph.corpus import PostgresCorpusReader
from app.graph.inputs import resume_command, start_input
from app.graph.intake import ExtractedValue, IntakeExtraction
from app.graph.nodes.form_analysis import form_analysis
from app.graph.nodes.policy_retrieval import policy_retrieval
from app.graph.state import EntityProfile, ProfileValue, dump, validate_state
from app.graph.synthesis import ChecklistDraft
from app.llm.fake import scripted_llm
from app.retrieval.factory import retriever_from_settings
from app.retrieval.qdrant_store import ChunkStore
from tests.unit.test_indexing import FakeEmbedder

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def world(
    corpus_db: sessionmaker[Session], corpus_store: ChunkStore
) -> Iterator[tuple[Any, PostgresCorpusReader]]:
    with corpus_db() as session:
        for key in extractable_document_keys(session):
            extract_and_save(session, key)
        session.commit()
    retriever = retriever_from_settings(
        get_settings(), sessions=corpus_db, embedder=FakeEmbedder(), store=corpus_store
    )
    yield retriever, PostgresCorpusReader(corpus_db, cache=None)


@pytest.fixture(autouse=True)
def no_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.llm.gemini as gemini

    def forbidden(*a: Any, **k: Any) -> Any:
        raise AssertionError("the workflow must never call Gemini")

    monkeypatch.setattr(gemini, "multimodal_from_settings", forbidden)
    monkeypatch.setattr(gemini.GeminiProvider, "analyze_pages", forbidden)


def bm25(world: tuple[Any, PostgresCorpusReader], **kw: Any) -> GraphDeps:
    retriever, corpus = world
    return GraphDeps(retriever=retriever, corpus=corpus, retrieval_mode="bm25", **kw)


def profile(entity: str, residency: str | None = None) -> EntityProfile:
    return EntityProfile(
        entity_type=ProfileValue(value=entity, source="user_answered"),
        residency=(
            ProfileValue(value=residency, source="user_answered") if residency else ProfileValue()
        ),
        account_type=ProfileValue(value="CURRENT_ACCOUNT", source="user_answered"),
    )


def state(query: str, p: EntityProfile) -> dict[str, Any]:
    return {
        "query": query,
        "security_status": "safe",
        "intake_complete": True,
        "entity_profile": dump(p),
    }


def test_the_reader_lists_only_kyc_forms(world: tuple[Any, PostgresCorpusReader]) -> None:
    _, corpus = world
    keys = [d.document_key for d in corpus.form_documents()]
    assert keys == sorted(keys) and len(keys) == 5
    assert "hdfc_netbanking_corporate_application" not in keys  # reference, not a form
    rows = {(r.form_key, r.method) for r in corpus.form_schema_rows()}
    assert all((k, "m1_chunks") in rows for k in keys)


def test_a_trust_question_retrieves_rbi_para_35_with_its_page(
    world: tuple[Any, PostgresCorpusReader],
) -> None:
    _, corpus = world
    out = policy_retrieval(
        state("documents required to open an account of a trust", profile("TRUST")), bm25(world)
    )
    meta = out["retrieval_meta"]
    assert meta["evidence_status"] == "found" and meta["mode"] == "bm25"
    cited = [(e["document_key"], e["paragraph"], e["page"]) for e in out["evidence"]]
    assert ("rbi_kyc_directions_2025", "35", 38) in cited
    # every item traces to a stored chunk and shows that chunk's (or its parent's) text
    stored = corpus.evidence(e["evidence_id"] for e in out["evidence"])
    for e in out["evidence"]:
        ctx = stored[e["evidence_id"]]
        assert e["document_key"] == ctx.retrieved.metadata.document_key
        assert ctx.context.metadata.content.startswith(e["text"])


def test_form_selection_follows_the_approved_residency_rule(
    world: tuple[Any, PostgresCorpusReader],
) -> None:
    def forms(p: EntityProfile) -> list[str]:
        return [f["form_key"] for f in form_analysis(state("q", p), bm25(world))["forms"]]

    assert forms(profile("LLP")) == ["hdfc_non_individual_account_opening"]
    assert forms(profile("INDIVIDUAL", "RESIDENT")) == ["hdfc_resident_rekyc_dormant"]
    assert forms(profile("INDIVIDUAL", "NRI")) == [
        "hdfc_nri_account_opening",
        "hdfc_nri_rekyc",
        "hdfc_resident_to_nro_conversion",
    ]


def test_llp_end_to_end_on_the_real_corpus(
    world: tuple[Any, PostgresCorpusReader], corpus_db: sessionmaker[Session]
) -> None:
    extraction = IntakeExtraction(
        entity_type=ExtractedValue(value="LLP", evidence_span="an LLP"),
        residency=ExtractedValue(),
        account_type=ExtractedValue(),
    )
    llm = scripted_llm(extraction, ChecklistDraft())  # S5 synthesis: an empty draft
    deps = bm25(world, llm=llm)
    graph = build_graph(deps, checkpointer=InMemorySaver())
    config = run_config(deps, thread_id="llp-corpus")
    question = "What KYC documents does an LLP need to open an account?"
    asyncio.run(graph.ainvoke(start_input(question, session_id="s", run_id="r"), config))
    final = asyncio.run(
        graph.ainvoke(resume_command({"answers": {"account_type": "CURRENT_ACCOUNT"}}), config)
    )
    validate_state(final)
    meta = final["retrieval_meta"]
    assert meta["evidence_status"] in ("found", "partial")
    if meta["evidence_status"] == "partial":
        assert "no_entity_specific_primary_evidence" in meta["gaps"]
    # LLP is only tagged on the HDFC non-individual form: any primary RBI text arrives
    # through the entity-group pass and is labelled so.
    for e in final["evidence"]:
        if e["can_support_mandatory"]:
            assert e["match_scope"] == "entity_group"
    [form] = final["forms"]
    assert (form["form_key"], form["method"]) == (
        "hdfc_non_individual_account_opening",
        "m1_chunks",
    )
    with corpus_db() as session:
        stored = load_form_schema(session, form["form_key"])
    assert stored is not None
    stored_fields = {(f.printed_label, f.chunk_id) for s in stored.sections for f in s.fields}
    assert all((f["printed_label"], f["chunk_id"]) in stored_fields for f in form["fields"])
    assert all(a["relationship_only"] is True for a in final["associations"])
    assert "mandatory_under_policy" not in json.dumps(final)
    # S5: the empty draft supports nothing, so the honest outcome is INSUFFICIENT_EVIDENCE
    assert final["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert len(llm.requests) == 2  # intake + synthesis; the S4 nodes make none
