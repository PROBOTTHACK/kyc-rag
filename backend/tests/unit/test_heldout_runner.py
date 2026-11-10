"""The one-time held-out runner (app/retrieval/heldout.py, `evaluate --final-heldout`).

Synthetic data only: these tests never read the real held-out file. They check that only
C0 and the recorded selection run, one scored pass each, that alternative evidence never
enters the metrics, that the exact-term subgroup is reported, and that the run refuses a
wrong hash, an inconsistent decision record and a second run."""

import json
from pathlib import Path
from typing import Any

import pytest

from app.config import get_settings
from app.ingestion import cli
from app.retrieval.heldout import (
    EXACT_TERM_IDS,
    FROZEN_HELDOUT_SHA256,
    HeldoutProtocolError,
    alternative_ids,
    configurations,
    normalized_sha256,
    render_report,
    results_record,
    run_once,
)
from app.retrieval.keyword import KeywordIndex
from app.retrieval.search import SearchFilters
from app.retrieval.selection import CANDIDATES
from tests.unit.test_hybrid import PAYLOADS, FakeEmbedder, FakeManager, dense_hits
from tests.unit.test_keyword import corpus

C6_RECORD: dict[str, Any] = {
    "code_commit": "86d337e",
    "selected": {"id": "C6", "settings": {"RETRIEVAL_MODE": "hybrid", "BM25_STOPWORDS": True,
                                          "RRF_K": 20}},
    "candidates": [{"id": "C0", "recall_at_5": {"value": 0.825}, "hit_at_5": {"value": 0.9},
                    "mrr_at_5": {"value": 0.775}},
                   {"id": "C6", "recall_at_5": {"value": 0.9}, "hit_at_5": {"value": 0.95},
                    "mrr_at_5": {"value": 0.735}}],
}  # fmt: skip


def test_frozen_hash_matches_the_test_constant() -> None:
    from tests.unit.test_m2_heldout import HELDOUT_SHA256

    assert FROZEN_HELDOUT_SHA256 == HELDOUT_SHA256  # one frozen value everywhere


def test_configurations_are_the_reference_and_the_recorded_selection() -> None:
    assert [c.id for c in configurations(C6_RECORD)] == ["C0", "C6"]
    assert [c.id for c in configurations({"selected": {"id": "C0", "settings": {
        "RETRIEVAL_MODE": "dense", "BM25_STOPWORDS": False, "RRF_K": 60}}})] == ["C0"]  # fmt: skip
    bad = json.loads(json.dumps(C6_RECORD))
    bad["selected"]["settings"]["RRF_K"] = 60
    with pytest.raises(HeldoutProtocolError, match="do not match"):
        configurations(bad)
    with pytest.raises(HeldoutProtocolError, match="unknown"):
        configurations({"selected": {"id": "C9", "settings": {}}})


ALT = PAYLOADS[4]["chunk_id"]  # dense rank 1 in the fake dense ranking
TARGET_DOC = PAYLOADS[1]["document_key"]


def synthetic_cases() -> list[dict[str, Any]]:
    base = {"query": "trust deed documents", "targets": [{"document_key": "nowhere", "page": 1}]}
    cases = [
        {"id": i, **base, "notes": "[exact-term] synthetic"} for i in EXACT_TERM_IDS
    ]  # fmt: skip
    # The alternative chunk is retrieved, but the scoring target is not: must score 0.
    cases.append({"id": "m2h-99", **base, "notes": f"SCORING: x. {'ALTERNATIVE (non-scoring)'}: "
                  f"chunk {ALT}"})  # fmt: skip
    return cases


def test_alternative_ids_parse_only_after_the_marker() -> None:
    notes = f"SCORING: chunk {PAYLOADS[0]['chunk_id']}. ALTERNATIVE (non-scoring): chunk {ALT}"
    assert alternative_ids(notes) == {ALT}
    assert alternative_ids("SCORING: only") == set()


@pytest.fixture
def services(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"dense": 0}

    def fake_search(*a: Any, **kw: Any) -> Any:
        calls["dense"] += 1
        return dense_hits(kw["top_k"])

    monkeypatch.setattr("app.retrieval.retrieve.search", fake_search)
    return calls


def manager_for(stopwords: bool) -> Any:
    return lambda: FakeManager(KeywordIndex.build(corpus(), stopwords=stopwords))


def fake_runs(cases_n: int) -> Any:
    from app.retrieval.retrieve import Retriever

    by_id = {c.id: c for c in CANDIDATES}
    runs = {}
    for cid in ("C0", "C6"):
        c = by_id[cid]
        embedder: Any = FakeEmbedder
        store: Any = object
        keyword: Any = manager_for(bool(c.stopwords))
        r = Retriever(embedder=embedder, store=store, keyword=keyword, rrf_k=c.rrf_k or 60)

        def run(q: str, f: SearchFilters, k: int, r: Retriever = r, m: str = c.mode) -> Any:
            return r.retrieve(q, mode=m, top_k=k, filters=f)

        runs[c] = run
    return runs


def test_run_once_scores_once_and_never_counts_alternatives(services: dict[str, int]) -> None:
    from app.retrieval.evaluation import RetrievalCase

    cases = [RetrievalCase.model_validate(c) for c in synthetic_cases()]
    runs = run_once(cases, fake_runs(len(cases)))
    assert [r.candidate.id for r in runs] == ["C0", "C6"]
    assert services["dense"] == 2 * len(cases)  # exactly one retrieval per case and config
    for r in runs:
        assert len(r.latencies_ms) == len(cases)
        assert r.metrics.recall == 0 and r.metrics.hit == 0  # alternative never scores
        assert r.alternative_hits["m2h-99"] is True
    text = render_report(runs, record=C6_RECORD, dataset="synthetic", sha256="abc",
                         created_at="t")  # fmt: skip
    for needle in ("Exact-term subgroup", "Development vs held-out", "alternative evidence hit",
                   "| m2h-06 (exact-term) |", "Δ (rel)"):  # fmt: skip
        assert needle in text
    rec = results_record(runs, record=C6_RECORD, dataset="synthetic", sha256="abc",
                         code_commit="c", created_at="t")  # fmt: skip
    assert rec["selected_configuration"] == "C6" and rec["frozen_sha256_verified"] is False
    q = rec["configurations"][1]["per_query"][-1]
    assert q["alternative_evidence_hit"] is True and q["scoring_target_hit"] is False


def test_missing_exact_term_cases_fail_before_any_retrieval(services: dict[str, int]) -> None:
    from app.retrieval.evaluation import RetrievalCase

    cases = [RetrievalCase.model_validate(synthetic_cases()[-1])]
    with pytest.raises(HeldoutProtocolError, match="exact-term"):
        run_once(cases, fake_runs(1))
    assert services["dense"] == 0


# --- CLI ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["evaluate", "--final-heldout"],  # needs --record
        ["evaluate", "--final-heldout", "--record", "x.json", "--mode", "dense"],
        ["evaluate", "--final-heldout", "--record", "x.json", "--select"],
        ["evaluate", "--final-heldout", "--record", "x.json", "--dataset", "other.jsonl"],
    ],
)
def test_final_heldout_option_rules(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(argv)


def setup_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, str]:
    data = tmp_path / "heldout.jsonl"
    data.write_text("\n".join(json.dumps(c) for c in synthetic_cases()) + "\n", encoding="utf-8")
    decision = tmp_path / "decision.json"
    decision.write_text(json.dumps(C6_RECORD), encoding="utf-8")
    sha = normalized_sha256(data)  # the runner hashes LF-normalised bytes
    monkeypatch.setattr(cli, "KeywordIndexManager", lambda s, stopwords: FakeManager(
        KeywordIndex.build(corpus(), stopwords=stopwords)))  # fmt: skip
    return data, decision, sha


def final(tmp: Path, data: Path, decision: Path, sha: str, record: Path) -> int:
    sessions: Any = object()
    return cli._final_heldout(get_settings(), report_path=tmp / "report.md", record_path=record,
                              dataset=data, decision=decision, expected_sha256=sha,
                              embedder=FakeEmbedder(), store=object(),  # type: ignore[arg-type]
                              sessions=sessions)  # fmt: skip


def test_final_heldout_runs_once_and_refuses_a_second_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services: dict[str, int],
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    data, decision, sha = setup_cli(tmp_path, monkeypatch)
    record = tmp_path / "results.json"
    assert final(tmp_path, data, decision, sha, record) == 0
    rec = json.loads(record.read_text(encoding="utf-8"))
    assert [c["id"] for c in rec["configurations"]] == ["C0", "C6"]
    calls = services["dense"]
    assert final(tmp_path, data, decision, sha, record) == 2  # one-time
    assert services["dense"] == calls and "one-time" in capsys.readouterr().out


def test_final_heldout_refuses_a_wrong_hash_before_retrieval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services: dict[str, int],
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    data, decision, _ = setup_cli(tmp_path, monkeypatch)
    record = tmp_path / "results.json"
    assert final(tmp_path, data, decision, "0" * 64, record) == 2
    assert not record.exists() and services["dense"] == 0
    assert "does not match the frozen" in capsys.readouterr().out
