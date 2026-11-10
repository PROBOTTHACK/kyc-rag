"""M2 A3: the stopword tokenizer variant, the registered candidate matrix, the exact
selection rule with its tie-breaks, the bootstrap CI, and `evaluate --select` isolation.
No development or held-out experiment is run here: all reports are synthetic."""

import json
from collections.abc import Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from app.config import REPO_ROOT, get_settings
from app.ingestion import cli
from app.retrieval import keyword
from app.retrieval.evaluation import CaseResult, EvalReport, RetrievalCase, Timing
from app.retrieval.keyword import STOPWORDS_LUCENE_EN, KeywordIndex, KeywordIndexManager, tokenize
from app.retrieval.selection import (
    CANDIDATES,
    FIXED_PARAMETERS,
    RECALL_MARGIN,
    bootstrap_ci,
    decide,
    exact_metrics,
    outcome,
    render_selection,
    selection_record,
)
from tests.unit.test_keyword import corpus, fake_session

LUCENE = (
    "a an and are as at be but by for if in into is it no not of on or such that the their "
    "then there these they this to was will with"
)

# --- the registered stopword variant -----------------------------------------------------------


def test_stopword_list_is_exactly_the_registered_lucene_list() -> None:
    assert sorted(LUCENE.split()) == sorted(STOPWORDS_LUCENE_EN)
    assert len(STOPWORDS_LUCENE_EN) == 33


def test_stopwords_remove_negation_and_plain_is_unchanged() -> None:
    text = "KYC is not mandatory if no OVD is held by the customer"
    assert tokenize(text) == [
        "kyc",
        "is",
        "not",
        "mandatory",
        "if",
        "no",
        "ovd",
        "is",
        "held",
        "by",
        "the",
        "customer",
    ]
    assert tokenize(text, stopwords=True) == ["kyc", "mandatory", "ovd", "held", "customer"]
    assert "not" not in tokenize("not", stopwords=True) and "no" not in tokenize(
        "no", stopwords=True
    )
    assert tokenize("V-CIP Form 60", stopwords=True) == ["v-cip", "form", "60"]


def test_stopword_index_applies_the_variant_to_corpus_and_queries() -> None:
    plain, stop = KeywordIndex.build(corpus()), KeywordIndex.build(corpus(), stopwords=True)
    assert not plain.stopwords and stop.stopwords
    assert [h.chunk_id for h in stop.search("the trust deed of a trust")] == [
        h.chunk_id for h in stop.search("trust deed trust")
    ]
    assert stop.search("the of and") == []  # only stopwords: no query terms left


def test_manager_fingerprint_names_the_variant_and_plain_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(keyword, "chunk_corpus_version", lambda s: "V")
    monkeypatch.setattr(keyword, "eligible_chunk_payloads", lambda s: corpus())
    plain = KeywordIndexManager(fake_session).get()  # type: ignore[arg-type]
    stop = KeywordIndexManager(fake_session, stopwords=True).get()  # type: ignore[arg-type]
    assert plain.fingerprint == "t1:x:V" and not plain.stopwords  # the A2 value
    assert stop.fingerprint == "t1s:x:V" and stop.stopwords


def test_tokenizer_default_is_plain_in_code() -> None:
    # The library defaults stay plain; the deployed choice comes from the settings.
    assert KeywordIndex.build(corpus()).stopwords is False
    assert KeywordIndexManager(fake_session).stopwords is False  # type: ignore[arg-type]


def test_default_settings_match_the_committed_a3_decision_record() -> None:
    record = json.loads(
        (REPO_ROOT / "data" / "eval" / "m2_a3_selection.json").read_text(encoding="utf-8")
    )
    s = get_settings()
    chosen = record["selected"]["settings"]
    assert (
        chosen["RETRIEVAL_MODE"],
        chosen["BM25_STOPWORDS"],
        chosen["RRF_K"],
    ) == (s.RETRIEVAL_MODE, s.BM25_STOPWORDS, s.RRF_K)
    assert record["fixed_parameters"]["candidates_per_retriever"] == s.RETRIEVAL_CANDIDATES
    assert record["selected"]["id"] == "C6" and record["qualifiers"] == ["C6", "C4"]


# --- the registered matrix ---------------------------------------------------------------------


def test_candidate_matrix_is_exactly_the_seven_registered_configurations() -> None:
    assert [(c.id, c.mode, c.stopwords, c.rrf_k) for c in CANDIDATES] == [
        ("C0", "dense", None, None),
        ("C1", "bm25", False, None),
        ("C2", "bm25", True, None),
        ("C3", "hybrid", False, 60),
        ("C4", "hybrid", False, 20),
        ("C5", "hybrid", True, 60),
        ("C6", "hybrid", True, 20),
    ]
    assert [c.simplicity for c in CANDIDATES] == [0, 1, 2, 3, 3, 4, 4]
    assert FIXED_PARAMETERS["candidates_per_retriever"] == 50 and FIXED_PARAMETERS["top_k"] == 5
    assert Fraction(1, 20) == RECALL_MARGIN


# --- synthetic reports -------------------------------------------------------------------------


def case_result(i: int, matched: int, targets: int, rank: int | None) -> CaseResult:
    case = RetrievalCase.model_validate(
        {
            "id": f"q{i:02d}",
            "query": f"query {i}",
            "targets": [{"document_key": "d", "page": p + 1} for p in range(targets)],
        }
    )
    return CaseResult(
        case=case,
        recall=matched / targets,
        hit=rank is not None,
        reciprocal_rank=1 / rank if rank else 0.0,
        matched=[f"t{j}" for j in range(matched)],
        retrieved=[],
    )


def report(
    rows: Sequence[tuple[int, int, int | None]], p50: float = 10.0, mode: str = "x"
) -> EvalReport:
    results = [case_result(i, m, t, r) for i, (m, t, r) in enumerate(rows)]
    return EvalReport(
        dataset="dev",
        k=5,
        embedding_model=None,
        embedding_revision=None,
        collection=None,
        created_at="2026-11-10T00:00:00+00:00",
        results=results,
        mode=mode,
        timing=Timing(passes=3, p50_ms=p50, p95_ms=p50 + 1, mean_ms=p50),
    )


# 20 queries; dense: 16 full hits at rank 1, 4 misses -> Recall 16/20, Hit 16/20, MRR 16/20.
DENSE = [(1, 1, 1)] * 16 + [(0, 1, None)] * 4


def with_gain(gained: int, *, rank: int = 1, lose_hits: int = 0, p50: float = 10.0) -> EvalReport:
    rows = [(1, 1, rank)] * (16 - lose_hits) + [(0, 1, None)] * lose_hits
    rows += [(1, 1, rank)] * gained + [(0, 1, None)] * (4 - gained)
    return report(rows, p50=p50)


def decision_for(reports: dict[str, EvalReport]) -> Any:
    base = {"C0": report(DENSE)}
    default = report(DENSE)  # no gain -> never qualifies
    return decide([outcome(c, (base | reports).get(c.id, default)) for c in CANDIDATES])


def test_exact_fractions_avoid_float_rounding_at_the_boundary() -> None:
    # Gain of exactly 1/20 built from thirds and halves: float sums can land just below 0.05.
    base = report([(1, 3, 1), (1, 2, 1)] + [(1, 1, 1)] * 18)
    plus = report([(2, 3, 1), (2, 2, 1)] + [(1, 1, 1)] * 18)
    d = exact_metrics(plus).recall - exact_metrics(base).recall
    assert d == Fraction(1, 3 * 20) + Fraction(1, 2 * 20)
    m = exact_metrics(report(DENSE))
    assert (m.recall, m.hit, m.mrr) == (Fraction(4, 5), Fraction(4, 5), Fraction(4, 5))


def test_exactly_plus_one_twentieth_qualifies_and_less_does_not() -> None:
    d = decision_for({"C3": with_gain(1), "C4": report(DENSE[:-1] + [(1, 2, 2)])})
    by = {o.candidate.id: o for o in d.outcomes}
    assert by["C3"].delta_recall == Fraction(1, 20) and by["C3"].qualifies
    assert by["C4"].delta_recall == Fraction(1, 40) and not by["C4"].qualifies
    assert d.selected.candidate.id == "C3"


def test_hit_drop_with_large_recall_gain_is_rejected() -> None:
    # Dense: 16 hits of 1 target each + 4 misses. Candidate: two fewer hits, but each hit
    # finds both of its two targets elsewhere... model directly with exact metrics instead.
    dense = report([(1, 2, 1)] * 16 + [(0, 2, None)] * 4)  # recall 8/20 = 0.40, hit 0.80
    cand = report([(2, 2, 1)] * 15 + [(0, 2, None)] * 5)  # recall 15/20 = 0.75, hit 0.75
    d = decide(
        [
            outcome(c, dense if c.id == "C0" else (cand if c.id == "C3" else dense))
            for c in CANDIDATES
        ]
    )
    c3 = next(o for o in d.outcomes if o.candidate.id == "C3")
    assert c3.delta_recall == Fraction(7, 20) and not c3.qualifies
    assert any("Hit@5" in r for r in c3.reasons) and d.selected.candidate.id == "C0"


def test_none_qualifying_keeps_dense() -> None:
    d = decision_for({})
    assert d.qualifiers == [] and d.selected.candidate.id == "C0"


def test_tie_breaks_recall_then_mrr_then_simplicity_then_latency_then_id() -> None:
    # Recall decides first.
    d = decision_for({"C3": with_gain(2), "C5": with_gain(3)})
    assert [o.candidate.id for o in d.qualifiers] == ["C5", "C3"]
    # Equal recall: higher MRR wins (rank 1 beats rank 2).
    d = decision_for({"C3": with_gain(2, rank=2), "C5": with_gain(2, rank=1)})
    assert d.selected.candidate.id == "C5"
    # Equal recall and MRR: simpler wins (bm25 plain C1 over hybrid C3), latency ignored.
    d = decision_for({"C1": with_gain(2, p50=99.0), "C3": with_gain(2, p50=1.0)})
    assert d.selected.candidate.id == "C1"
    # Equal recall, MRR and simplicity (C3 vs C4): lower p50 wins.
    d = decision_for({"C3": with_gain(2, p50=20.0), "C4": with_gain(2, p50=10.0)})
    assert d.selected.candidate.id == "C4"
    # Everything equal: the candidate id decides.
    d = decision_for({"C4": with_gain(2), "C3": with_gain(2)})
    assert d.selected.candidate.id == "C3"


def test_hit_is_a_condition_not_a_tie_break() -> None:
    # Both qualify with equal recall and MRR; C5 has a higher Hit@5 but is less simple.
    more_hits = report([(1, 1, 1)] * 17 + [(1, 2, 2)] * 2 + [(0, 1, None)])  # hit 19/20
    fewer_hits = report([(1, 1, 1)] * 18 + [(0, 1, None)] * 2)  # hit 18/20
    d = decision_for({"C3": fewer_hits, "C5": more_hits})
    by = {o.candidate.id: o for o in d.outcomes}
    assert by["C3"].metrics.recall == by["C5"].metrics.recall
    assert by["C5"].metrics.hit > by["C3"].metrics.hit
    assert by["C3"].metrics.mrr == by["C5"].metrics.mrr
    assert d.selected.candidate.id == "C3"  # simplicity, not Hit@5


def test_decide_requires_the_registered_candidates() -> None:
    with pytest.raises(ValueError, match="registered"):
        decide([outcome(CANDIDATES[0], report(DENSE))])


def test_bootstrap_ci_is_deterministic_and_brackets_the_mean() -> None:
    half = Fraction(1, 2)
    a = (Fraction(1), Fraction(1), Fraction(0), Fraction(1), half) * 4
    b = (Fraction(1), Fraction(0), Fraction(0), Fraction(1), half) * 4
    ci = bootstrap_ci(a, b)
    assert ci == bootstrap_ci(a, b) and ci[0] <= 0.2 <= ci[1]
    with pytest.raises(ValueError):
        bootstrap_ci(a, b[:-1])


def test_report_and_record_state_the_rule_and_decision() -> None:
    d = decision_for({"C4": with_gain(2)})
    text = render_selection(d, dataset="data/eval/m1_retrieval.jsonl", dataset_sha256="abc")
    for needle in ("C0", "C6", "Qualifies?", "Selected: C4", "held-out set was not read"):
        assert needle in text
    rec = selection_record(
        d, dataset="data/eval/m1_retrieval.jsonl", dataset_sha256="abc", code_commit="deadbeef"
    )
    assert rec["selected"]["id"] == "C4" and rec["selected"]["settings"] == {
        "RETRIEVAL_MODE": "hybrid",
        "BM25_STOPWORDS": False,
        "RRF_K": 20,
    }
    assert rec["candidates"][4]["delta_recall_vs_dense"]["exact"] == "1/10"
    assert "not read" in rec["heldout"] and json.dumps(rec)
    keep = selection_record(decision_for({}), dataset="d", dataset_sha256="x", code_commit="c")
    assert keep["selected"]["id"] == "C0" and keep["selected"]["dense_retained"] is True


# --- `evaluate --select`: development set only -------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["evaluate", "--select", "--dataset", "data/eval/m2_heldout.jsonl"],
        ["evaluate", "--select", "--dataset", "other.jsonl"],
        ["evaluate", "--select", "--mode", "hybrid"],
        ["evaluate", "--select", "--compare"],
        ["evaluate", "--record", "x.json"],
    ],
)
def test_select_rejects_other_datasets_and_options(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(argv)


def test_select_refuses_the_heldout_path(capsys: pytest.CaptureFixture[str]) -> None:
    def forbidden(*_: Any, **__: Any) -> Any:
        raise AssertionError("the held-out set must not be read")

    held = Path("data/eval/m2_heldout.jsonl")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "load_cases", forbidden)
        assert cli._select(get_settings(), report_path=None, record_path=None, dataset=held) == 2
    assert "development set only" in capsys.readouterr().out


def test_select_loads_only_the_development_set_and_writes_a_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.unit.test_hybrid import CASE, FakeEmbedder, FakeManager, dense_hits

    loaded: list[Path] = []

    def load(p: Path) -> list[RetrievalCase]:
        loaded.append(p)
        return [CASE]

    monkeypatch.setattr(cli, "load_cases", load)
    monkeypatch.setattr("app.retrieval.retrieve.search", lambda *a, **kw: dense_hits(kw["top_k"]))
    monkeypatch.setattr(
        cli,
        "KeywordIndexManager",
        lambda s, stopwords: FakeManager(KeywordIndex.build(corpus(), stopwords=stopwords)),
    )
    monkeypatch.setattr(cli, "embedder_from_settings", lambda s: FakeEmbedder())
    monkeypatch.setattr(cli, "store_from_settings", lambda s: object())
    record = tmp_path / "record.json"
    sessions: Any = object()
    assert cli._select(get_settings(), report_path=None, record_path=record, sessions=sessions) == 0
    assert loaded == [REPO_ROOT / cli.DEFAULT_DATASET]  # the development set, and only it
    rec = json.loads(record.read_text(encoding="utf-8"))
    assert [c["id"] for c in rec["candidates"]] == [c.id for c in CANDIDATES]
    assert rec["dataset"] == "data/eval/m1_retrieval.jsonl" and len(rec["dataset_sha256"]) == 64
    out = capsys.readouterr().out
    assert "## Decision" in out and "Selected:" in out
