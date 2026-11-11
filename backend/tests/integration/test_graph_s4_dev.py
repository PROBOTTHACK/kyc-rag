"""M3 S4 against the development stack (`make ingest`, `make forms`), read-only.

Real BGE dense retrieval over the dev Qdrant collection and the dev Postgres corpus,
including the stored Gemini form rows. Nothing is written (no Redis cache either). Like
`test_dense_golden.py`, a missing stack FAILS rather than skips. The intake LLM is the
scripted fake; no live Groq or Gemini call is possible (spied).
"""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.config import get_settings
from app.db.session import create_sync_engine, create_sync_sessionmaker
from app.graph.builder import GraphDeps, build_graph, run_config
from app.graph.corpus import PostgresCorpusReader
from app.graph.inputs import start_input
from app.graph.intake import ExtractedValue, IntakeExtraction
from app.llm.fake import scripted_llm
from app.retrieval.factory import retriever_from_settings
from app.retrieval.qdrant_store import store_from_settings

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def dev() -> Iterator[GraphDeps]:
    settings = get_settings()
    store = store_from_settings(settings)
    try:
        exists = store.client.collection_exists(store.collection)
    except Exception as exc:  # noqa: BLE001 - report any connection problem as a failure
        pytest.fail(f"Qdrant unreachable ({type(exc).__name__}); run `make up`")
    if not exists:
        pytest.fail(f"collection {store.collection!r} missing; run `make ingest`")
    engine = create_sync_engine(settings)
    sessions = create_sync_sessionmaker(engine)
    try:
        yield GraphDeps(
            retriever=retriever_from_settings(settings, sessions=sessions),
            corpus=PostgresCorpusReader(sessions, cache=None),
            retrieval_mode="dense",
        )
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def no_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.llm.gemini as gemini

    def forbidden(*a: Any, **k: Any) -> Any:
        raise AssertionError("the workflow must never call Gemini")

    monkeypatch.setattr(gemini, "multimodal_from_settings", forbidden)
    monkeypatch.setattr(gemini.GeminiProvider, "analyze_pages", forbidden)


def run(dev: GraphDeps, query: str, extraction: IntakeExtraction) -> dict[str, Any]:
    deps = GraphDeps(
        retriever=dev.retriever,
        corpus=dev.corpus,
        retrieval_mode="dense",
        llm=scripted_llm(extraction),
    )
    graph = build_graph(deps, checkpointer=InMemorySaver())
    out = asyncio.run(
        graph.ainvoke(
            start_input(query, session_id="s", run_id="r"), run_config(deps, thread_id="d")
        )
    )
    return dict(out)


def value(v: str, span: str) -> ExtractedValue:
    return ExtractedValue(value=v, evidence_span=span)


def test_trust_finds_rbi_para_35_and_uses_the_gemini_form_schema(dev: GraphDeps) -> None:
    out = run(
        dev,
        "What documents are required to open a current account for a trust?",
        IntakeExtraction(
            entity_type=value("TRUST", "a trust"),
            residency=ExtractedValue(),
            account_type=value("CURRENT_ACCOUNT", "current account"),
        ),
    )
    assert out["retrieval_meta"]["evidence_status"] == "found"
    top = out["evidence"][0]
    assert (top["document_key"], top["paragraph"], top["page"], top["match_scope"]) == (
        "rbi_kyc_directions_2025",
        "35",
        38,
        "entity",
    )
    [form] = out["forms"]
    assert (form["form_key"], form["method"], form["version"]) == (
        "hdfc_non_individual_account_opening",
        "gemini_multimodal",
        2,
    )
    # the scanned form's uncertainty stays visible
    assert "hdfc_non_individual_account_opening:scanned_document" in out["form_flags"]


def test_llp_is_partial_with_rbi_text_only_through_the_entity_group(dev: GraphDeps) -> None:
    out = run(
        dev,
        "What KYC documents does an LLP need to open a current account?",
        IntakeExtraction(
            entity_type=value("LLP", "an LLP"),
            residency=ExtractedValue(),
            account_type=value("CURRENT_ACCOUNT", "a current account"),
        ),
    )
    meta = out["retrieval_meta"]
    assert meta["evidence_status"] == "partial"
    assert meta["gaps"][:2] == [
        "no_entity_specific_primary_evidence",
        "primary_evidence_only_via_entity_group",
    ]
    primary = [e for e in out["evidence"] if e["can_support_mandatory"]]
    assert primary and all(e["match_scope"] == "entity_group" for e in primary)
    assert all(e["document_key"] == "rbi_kyc_directions_2025" for e in primary)
