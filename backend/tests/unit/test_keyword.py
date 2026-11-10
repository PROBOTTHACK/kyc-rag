"""M2 A1: BM25 keyword retrieval (tokenizer, filter-before-scoring index, freshness manager),
`SearchFilters.matches`, `evaluate_with`, and the `--mode` / held-out CLI guards."""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from app.config import get_settings
from app.ingestion import cli
from app.retrieval import keyword
from app.retrieval.embeddings import embedding_input
from app.retrieval.evaluation import (
    EvalReport,
    RetrievalCase,
    evaluate_with,
    render_markdown,
    score_case,
)
from app.retrieval.keyword import (
    KeywordIndex,
    KeywordIndexManager,
    KeywordIndexUnavailable,
    index_text,
    tokenize,
)
from app.retrieval.search import SearchFilters, SearchHit
from app.schemas.taxonomy import ChunkRole
from tests.unit.test_indexing import chunk, doc

# --- tokenizer ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("V-CIP and Form No. 60", ["v-cip", "and", "form", "no", "60"]),
        ("Section 51A of UAPA; UNSCR 1718.", ["section", "51a", "of", "uapa", "unscr", "1718"]),
        ("k1=1.5, (iii) ₹50,000", ["k1", "1.5", "iii", "50", "000"]),
        ("ＰＡＮ / e-PAN", ["pan", "e-pan"]),  # NFKC folds full-width letters
        ("?? -- ..", []),
    ],
)
def test_tokenizer(text: str, tokens: list[str]) -> None:
    assert tokenize(text) == tokens


def test_fingerprint_sql_covers_every_document_level_payload_column() -> None:
    from app.db import repository
    from app.db.corpus import DOCUMENT_LEVEL_PAYLOAD_FIELDS

    sql = str(repository._FINGERPRINT_SQL)
    row = re.search(r"ROW\(([^)]*)\)", sql)
    assert row is not None
    listed = [c.strip().removeprefix("d.") for c in row.group(1).split(",")]
    assert listed == list(repository._FINGERPRINT_DOCUMENT_COLUMNS)
    assert set(listed) == {"id", *DOCUMENT_LEVEL_PAYLOAD_FIELDS.values()}
    assert "to_jsonb(c) - 'created_at' - 'updated_at'" in sql  # every other chunk column


def test_version_sql_is_one_statement_over_both_tables_with_counts_and_order() -> None:
    from app.db import repository

    sql = " ".join(str(repository._VERSION_SQL).split())
    assert sql.count("SELECT") == 5 and ";" not in sql  # one statement = one snapshot
    for table in ("chunks", "documents"):
        assert f"(SELECT count(*) FROM {table})" in sql  # noqa: S608 - expected text
        assert f"string_agg(id::text || '@' || xmin::text, ',' ORDER BY id) FROM {table}" in sql


def test_index_text_is_the_dense_embedding_input() -> None:
    d = doc()
    for rec in (chunk(d, 1, "(1) First part.", context="7. Lead"), chunk(d, 2, "8. Text.")):
        payload = rec.metadata.to_qdrant_payload()
        assert index_text(payload) == embedding_input(rec.metadata)


# --- SearchFilters.matches (exact M1 semantics; Qdrant parity is an integration test) ----------


def payload(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "evidence_eligibility": "primary",
        "can_support_mandatory": True,
        "bank": "RBI",
        "document_key": "rbi_doc",
        "entity_types": ["COMPANY", "TRUST"],
    }
    return base | kw


@pytest.mark.parametrize(
    ("filters", "p", "expected"),
    [
        (SearchFilters(), payload(), True),
        (SearchFilters(), payload(evidence_eligibility="supporting"), True),
        (SearchFilters(), payload(evidence_eligibility="context_only"), False),
        (
            SearchFilters(include_context_only=True),
            payload(evidence_eligibility="context_only"),
            True,
        ),
        (SearchFilters(mandatory_evidence_only=True), payload(can_support_mandatory=False), False),
        (SearchFilters(mandatory_evidence_only=True), payload(can_support_mandatory=None), False),
        (SearchFilters(banks=("HDFC",)), payload(), False),
        (SearchFilters(banks=("RBI", "HDFC")), payload(), True),
        (SearchFilters(document_keys=("other",)), payload(), False),
        (SearchFilters(entity_terms=("TRUST",)), payload(), True),
        (SearchFilters(entity_terms=("INDIVIDUAL",)), payload(), False),
        (SearchFilters(entity_terms=("LEGAL_ENTITY",)), payload(), True),  # group expansion
        (SearchFilters(entity_terms=("TRUST",)), payload(entity_types=[]), False),
        (SearchFilters(entity_terms=("TRUST",)), payload(entity_types=None), False),
        # interactions: every condition must hold
        (SearchFilters(banks=("RBI",), entity_terms=("INDIVIDUAL",)), payload(), False),
        (
            SearchFilters(include_context_only=True, mandatory_evidence_only=True),
            payload(evidence_eligibility="context_only", can_support_mandatory=False),
            False,
        ),
    ],
)
def test_filter_matches(filters: SearchFilters, p: dict[str, Any], expected: bool) -> None:
    assert filters.matches(p) is expected


def test_unknown_entity_term_raises_like_build_filter() -> None:
    with pytest.raises(ValueError):
        SearchFilters(entity_terms=("BANK",)).matches(payload())


# --- KeywordIndex ------------------------------------------------------------------------------


def corpus() -> list[dict[str, Any]]:
    d = doc()
    hdfc = doc(
        "hdfc_form",
        bank="HDFC",
        document_type="form",
        source_authority=None,
        attribution_basis="filename_only",
        source_status="unknown",
        status_reason="undated",
    )
    recs = [
        chunk(
            d,
            1,
            "A trust shall submit the trust deed and registration certificate.",
            entity_types=["COMPANY"],
        ),
        chunk(d, 2, "Companies submit the certificate of incorporation."),
        chunk(d, 3, "Partnership firms submit the partnership deed."),
        chunk(hdfc, 4, "Trust deed trust deed trust deed: copy required for trust accounts."),
        chunk(d, 5, "Unrelated text about wire transfers."),
        chunk(d, 6, "Unrelated text about wire transfers."),  # identical twin of chunk 5
    ]
    return [r.metadata.to_qdrant_payload() for r in recs]


def test_build_rejects_parents_and_ineligible_chunks() -> None:
    d = doc()
    parent = chunk(
        d, 0, "7. Whole paragraph.", chunk_role=ChunkRole.PARENT, embedding_eligible=False
    )
    with pytest.raises(ValueError, match="not embedding-eligible"):
        KeywordIndex.build([parent.metadata.to_qdrant_payload()])


def test_filters_apply_before_scoring(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = corpus()
    idx = KeywordIndex.build(payloads)
    assert idx.search("trust deed")[0].payload["document_key"] == "hdfc_form"  # best overall
    scored: list[list[int]] = []
    real = idx._bm25.get_batch_scores  # type: ignore[union-attr]

    def spy(query: list[str], doc_ids: list[int]) -> Any:
        scored.append(list(doc_ids))
        return real(query, doc_ids)

    monkeypatch.setattr(idx._bm25, "get_batch_scores", spy)
    hits = idx.search("trust deed", filters=SearchFilters(banks=("RBI",)))
    rbi = [i for i, p in enumerate(payloads) if p["bank"] == "RBI"]
    assert scored == [rbi]  # the HDFC chunk was never scored
    assert hits and all(h.payload["bank"] == "RBI" for h in hits)


def test_idf_is_global_so_filters_do_not_change_scores() -> None:
    idx = KeywordIndex.build(corpus())
    unfiltered = {h.chunk_id: h.score for h in idx.search("trust deed", top_k=10)}
    filtered = idx.search("trust deed", top_k=10, filters=SearchFilters(banks=("RBI",)))
    assert filtered and all(unfiltered[h.chunk_id] == h.score for h in filtered)


def test_ranking_rules() -> None:
    payloads = corpus()
    idx = KeywordIndex.build(payloads)
    hits = idx.search("wire transfers", top_k=10)
    assert [h.rank for h in hits] == list(range(1, len(hits) + 1))
    assert all(h.score > 0 for h in hits)  # zero-score chunks are dropped
    twins = [h.chunk_id for h in hits if "wire" in h.payload["content"]]
    assert twins == sorted(twins)  # equal scores: deterministic chunk_id order
    assert len(idx.search("trust deed", top_k=2)) == 2
    assert idx.search("zebra") == []
    hit = idx.search("incorporation")[0]
    assert hit.payload == payloads[1] and hit.citation.startswith("synthetic_doc, p.")


def test_edge_cases() -> None:
    idx = KeywordIndex.build(corpus())
    with pytest.raises(ValueError, match="empty"):
        idx.search("   ")
    assert idx.search("?? !!") == []  # no tokens
    assert idx.search("trust", filters=SearchFilters(document_keys=("nope",))) == []
    empty = KeywordIndex.build([])
    assert len(empty) == 0 and empty.search("trust") == []


# --- KeywordIndexManager -----------------------------------------------------------------------


class FakeSession:
    def connection(self, **_: Any) -> None:
        return None


@contextmanager
def fake_session() -> Iterator[FakeSession]:
    yield FakeSession()


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"fp": "A", "payloads": corpus(), "fail": False}

    def fingerprint(_: Any) -> str:
        if state["fail"]:
            raise ConnectionError("postgres down")
        return str(state["fp"])

    monkeypatch.setattr(keyword, "chunk_corpus_version", fingerprint)
    monkeypatch.setattr(keyword, "eligible_chunk_payloads", lambda _: list(state["payloads"]))
    return state


def manager() -> KeywordIndexManager:
    return KeywordIndexManager(fake_session)  # type: ignore[arg-type]


def test_manager_rebuilds_only_when_the_fingerprint_changes(db: dict[str, Any]) -> None:
    m = manager()
    first = m.get()
    assert m.get() is first and m.rebuilds == 1
    db["fp"], db["payloads"] = "B", db["payloads"][:2]
    second = m.get()
    assert second is not first and len(second) == 2 and m.rebuilds == 2
    assert second.fingerprint.endswith(":B")


def test_manager_never_serves_a_stale_index_after_a_read_failure(db: dict[str, Any]) -> None:
    m = manager()
    m.get()
    db["fail"] = True
    with pytest.raises(KeywordIndexUnavailable, match="postgres down"):
        m.get()
    with pytest.raises(KeywordIndexUnavailable):
        m.search("trust")
    db["fail"] = False  # recovered, same fingerprint: still rebuilt, not reused
    m.get()
    assert m.rebuilds == 2


# --- evaluate_with -----------------------------------------------------------------------------


def test_evaluate_with_scores_like_evaluate_and_times_passes() -> None:
    payloads = corpus()
    idx = KeywordIndex.build(payloads)
    case = RetrievalCase.model_validate(
        {"id": "c1", "query": "trust deed", "targets": [{"document_key": "hdfc_form", "page": 5}]}
    )
    calls: list[str] = []

    def retriever(q: str, f: SearchFilters, k: int) -> list[SearchHit]:
        calls.append(q)
        return idx.search(q, top_k=k, filters=f)

    report = evaluate_with(
        [case],
        retriever,
        mode="bm25",
        description="BM25 test",
        timed_passes=3,
        index_build_s=0.01,
        dataset="d.jsonl",
    )
    assert len(calls) == 4  # scored pass (= warm-up) + 3 timed passes
    assert report.results[0] == score_case(case, idx.search("trust deed"), 5)
    assert report.timing is not None and report.timing.passes == 3
    text = render_markdown(report)
    assert text.startswith("# Retrieval benchmark (bm25): Recall@5")
    assert "- Retrieval: BM25 test; default eligibility filters" in text
    assert "p95" in text and "index build 0.010 s" in text


def test_dense_report_text_is_unchanged() -> None:
    report = EvalReport(
        dataset="data/eval/m1_retrieval.jsonl",
        k=5,
        embedding_model="m",
        embedding_revision="r",
        collection="policy_chunks",
        created_at="2026-11-10T00:00:00+00:00",
        results=[],
    )
    # The exact M1 header (mode defaults to dense; no latency line).
    lines = [
        "# M1 retrieval benchmark: Recall@5",
        "",
        "- Dataset: `data/eval/m1_retrieval.jsonl` (1 queries)",
        "- Model: `m` @ `r`; collection `policy_chunks`; dense search, default eligibility filters",
        "- Run: 2026-11-10T00:00:00+00:00",
        "",
    ]
    case = RetrievalCase.model_validate(
        {"id": "c1", "query": "query", "targets": [{"document_key": "x", "page": 1}]}
    )
    one = report.model_copy(update={"results": [score_case(case, [], 5)]})
    assert render_markdown(one).splitlines()[:6] == lines


# --- CLI ---------------------------------------------------------------------------------------


def test_mode_flags_parse_and_default_to_dense() -> None:
    # Since M2 A2, an omitted --mode falls back to RETRIEVAL_MODE, whose default is dense.
    settings = get_settings()
    assert settings.RETRIEVAL_MODE == "dense"
    p = cli.build_parser(settings)
    assert p.parse_args(["search", "q"]).mode is None
    assert p.parse_args(["evaluate"]).mode is None
    for mode in ("dense", "bm25", "hybrid"):
        assert p.parse_args(["search", "q", "--mode", mode]).mode == mode
        assert p.parse_args(["evaluate", "--mode", mode]).mode == mode
    assert p.parse_args(["evaluate", "--compare"]).compare is True
    with pytest.raises(SystemExit):
        p.parse_args(["search", "q", "--mode", "sparse"])


@pytest.mark.parametrize("mode", ["dense", "bm25", "hybrid"])
@pytest.mark.parametrize(
    "dataset", [Path("data/eval/m2_heldout.jsonl"), Path("/elsewhere/m2_heldout.jsonl")]
)
def test_evaluate_refuses_the_frozen_heldout_set(
    mode: str, dataset: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*_: Any, **__: Any) -> Any:
        raise AssertionError("the held-out set must not be read")

    monkeypatch.setattr(cli, "load_cases", forbidden)
    monkeypatch.setattr(cli, "embedder_from_settings", forbidden)
    monkeypatch.setattr(cli, "KeywordIndexManager", forbidden)
    assert cli._evaluate(get_settings(), dataset, top_k=5, report_path=None, mode=mode) == 2
    assert "frozen held-out set" in capsys.readouterr().out


def test_bm25_search_loads_no_model_and_no_qdrant(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*_: Any, **__: Any) -> Any:
        raise AssertionError("bm25 mode must not load the model or contact Qdrant")

    monkeypatch.setattr(cli, "embedder_from_settings", forbidden)
    monkeypatch.setattr(cli, "store_from_settings", forbidden)
    monkeypatch.setattr(cli, "search", forbidden)
    idx = KeywordIndex.build(corpus())
    monkeypatch.setattr(KeywordIndexManager, "get", lambda self: idx)
    assert (
        cli._search(
            get_settings(),
            "trust deed",
            top_k=5,
            filters=SearchFilters(),
            expand=False,
            mode="bm25",
            sessions=fake_session,  # type: ignore[arg-type]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "primary+supporting; bm25" in out and "1. [" in out


def test_bm25_search_reports_an_unavailable_index(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def down(self: KeywordIndexManager) -> KeywordIndex:
        raise KeywordIndexUnavailable("keyword index unavailable: postgres down")

    monkeypatch.setattr(KeywordIndexManager, "get", down)
    assert (
        cli._search(
            get_settings(),
            "q",
            top_k=5,
            filters=SearchFilters(),
            expand=False,
            mode="bm25",
            sessions=fake_session,  # type: ignore[arg-type]
        )
        == 2
    )
    assert "postgres down" in capsys.readouterr().out


def test_dense_search_is_the_default_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    called: list[str] = []

    def dense(q: str, *_: Any, **__: Any) -> list[SearchHit]:
        called.append(q)
        return []

    monkeypatch.setattr(cli, "embedder_from_settings", lambda s: object())
    monkeypatch.setattr(cli, "store_from_settings", lambda s: object())
    monkeypatch.setattr(cli, "search", dense)
    monkeypatch.setattr(KeywordIndexManager, "get", lambda self: pytest.fail("bm25 used"))
    assert cli._search(get_settings(), "q", top_k=5, filters=SearchFilters(), expand=False) == 0
    assert called == ["q"]
    assert capsys.readouterr().out.startswith("query: 'q'  (top 5; primary+supporting)\n")
