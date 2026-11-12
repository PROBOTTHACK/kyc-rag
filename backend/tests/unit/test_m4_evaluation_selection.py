"""M4 S4.1: dev-only evaluation harness and the pre-registered selection rule (no model).

Metric arithmetic is checked on hand-made predictions; every gate, the false-support cap
and each tie-break of the registered rule are checked on synthetic outcomes; the runtime
threshold loader must return exactly what the committed record says."""

import json
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from app.graph.state import ClaimType, EvidenceRole
from app.verification.decide import Thresholds
from app.verification.evaluation import (
    DEV_PATH,
    Case,
    EvaluationError,
    Prediction,
    claim_for,
    compute_metrics,
    load_dev,
    metrics_json,
    predict,
)
from app.verification.nli import ENTAIL, FakeNLI
from app.verification.selection import (
    CANDIDATES,
    FSR_CAP,
    GRID,
    PROTOCOL_VERSION,
    Candidate,
    Outcome,
    decide,
    load_selected,
    outcome_json,
    threshold_grid,
    write_record,
)
from tests.unit.m4_fixtures import T, form, premise, primary

# --- the dev set only ----------------------------------------------------------------------------


def test_the_dev_set_loads_with_its_hash() -> None:
    cases, sha = load_dev()
    assert len(cases) == 55 and len(sha) == 64
    assert {c.gold for c in cases} == {"entailed", "contradicted", "insufficient"}


@pytest.mark.parametrize(
    "path",
    [
        DEV_PATH.with_name("m4_claims_heldout.jsonl"),
        DEV_PATH.with_name("m1_retrieval.jsonl"),
        Path("m4_claims_dev.jsonl"),
    ],
)
def test_anything_but_the_dev_file_is_refused(path: Path) -> None:
    with pytest.raises(EvaluationError, match="dev set only"):
        load_dev(path)


def test_every_dev_case_becomes_a_valid_atomic_claim() -> None:
    cases, _ = load_dev()
    for c in cases:
        claim = claim_for(c)  # the model's own validators run here
        assert claim.text == c.claim and claim.claim_type == c.claim_type
        if c.claim_type in (ClaimType.FORM_REQUEST, ClaimType.SUPPORTING_STATEMENT):
            assert claim.asserted_strength == "none"
        if c.claim_type == ClaimType.POLICY_OBLIGATION:
            assert claim.asserted_strength in ("unconditional", "conditional")


# --- predictions on the S2 fixtures (FakeNLI) -----------------------------------------

DEED = "For opening an account of a trust, the trust deed shall be obtained."


def case(gold: str = "entailed", ctype: str = "policy_obligation", **kw: Any) -> Case:
    base: dict[str, Any] = {
        "id": "x-1",
        "claim": "The trust deed must be obtained for a trust.",
        "claim_type": ClaimType(ctype),
        "entity": None,
        "match_scope": "entity",
        "gold": gold,
        "expected_obligation": False,
        "perturbations": ("paraphrase",),
        "chunk_ids": ("c",),
    }
    return Case(**(base | kw))


def entail_all() -> FakeNLI:
    return FakeNLI(rules=[lambda _p: ENTAIL])


def test_rules_only_never_supports_anything() -> None:
    p = predict(case(), [premise(primary(DEED))], scorer=entail_all(), thresholds=T,
                max_pairs=100, mode="rules_only")  # fmt: skip
    assert p.label == "insufficient" and p.verdict == "not_verified" and p.pairs_scored == 0


def test_combined_supports_and_reports_mandatory_capability() -> None:
    from app.schemas.taxonomy import EntityType

    c = case(entity=EntityType.TRUST)
    p = predict(c, [premise(primary(DEED))], scorer=entail_all(), thresholds=T, max_pairs=100)
    assert p.label == "entailed" and p.would_be_mandatory


def test_nli_only_ignores_the_gates_and_never_claims_mandatory() -> None:
    perm = "The trust deed may be obtained."  # the gates block this; the model alone does not
    p_comb = predict(case(), [premise(primary(perm))], scorer=entail_all(), thresholds=T,
                     max_pairs=100)  # fmt: skip
    p_nli = predict(case(), [premise(primary(perm))], scorer=entail_all(), thresholds=T,
                    max_pairs=100, mode="nli_only")  # fmt: skip
    assert p_comb.label == "insufficient" and p_nli.label == "entailed"
    assert not p_nli.would_be_mandatory


def test_a_form_never_yields_mandatory_even_when_entailed() -> None:
    c = case(ctype="form_request", claim="The form asks for the trust deed.")
    p = predict(c, [premise(form("Attach the trust deed copy."))], scorer=entail_all(),
                thresholds=T, max_pairs=100)  # fmt: skip
    assert p.label == "entailed" and not p.would_be_mandatory


def test_an_over_long_window_leaves_the_claim_not_verified() -> None:
    nli = FakeNLI(rules=[lambda _p: ENTAIL], too_long="trust deed")
    p = predict(case(), [premise(primary(DEED))], scorer=nli, thresholds=T, max_pairs=100)
    assert p.verdict == "not_verified" and p.reason_codes == ("window_too_long",)
    assert p.label == "insufficient"


# --- metrics --------------------------------------------------------------------------------------


def pred(
    cid: str, label: str, *, mandatory: bool = False, verdict: str | None = None
) -> Prediction:
    v = verdict or {"entailed": "supported", "contradicted": "contradicted"}.get(
        label, "insufficient"
    )
    return Prediction(cid, label, v, mandatory, 1, 0, ())  # type: ignore[arg-type]


def test_metric_arithmetic_is_exact() -> None:
    cases = [
        case("entailed", id="a", expected_obligation=True),
        case("entailed", id="b"),
        case("contradicted", id="c"),
        case("insufficient", id="d", perturbations=("negation",)),
        case("insufficient", id="e", perturbations=("negation", "form_only")),
    ]
    preds = [
        pred("a", "entailed", mandatory=True),
        pred("b", "insufficient", verdict="conflicting"),
        pred("c", "contradicted"),
        pred("d", "entailed", mandatory=True),  # a false support AND a MANDATORY false positive
        pred("e", "contradicted"),
    ]
    roles = {"a": {EvidenceRole.PRIMARY_POLICY}, "b": set(), "c": set(),
             "d": {EvidenceRole.FORM}, "e": set()}  # fmt: skip
    m = compute_metrics(cases, preds, roles)
    assert m.recall["entailed"] == Fraction(1, 2) and m.precision["entailed"] == Fraction(1, 2)
    assert m.false_support_rate == Fraction(1, 3)  # d among {c, d, e}
    assert m.false_contradiction_rate == Fraction(1, 4)  # e among {a, b, d, e}
    assert m.abstention_rate == Fraction(1, 5) and m.coverage == Fraction(4, 5)
    assert m.selective_accuracy == Fraction(2, 4)
    assert m.mandatory_false_positives == 1 and m.obligation_from_form_errors == 1
    assert m.mandatory_false_negatives == 0
    assert m.per_perturbation["negation"] == Fraction(0, 2)
    assert m.verdicts == {"conflicting": 1, "contradicted": 2, "supported": 2}
    as_json = metrics_json(m)
    assert as_json["false_support_rate"] == {"num": 1, "den": 3, "value": 0.333333}


def test_metrics_need_one_prediction_per_case() -> None:
    with pytest.raises(EvaluationError):
        compute_metrics([case(id="a")], [], {"a": set()})


# --- the registered rule --------------------------------------------------------------------------


def test_the_registered_grid_and_candidates() -> None:
    grid = threshold_grid("v")
    assert len(grid) == 35 and all(t.entail + t.contradict > 1 for t in grid)
    assert len({t.version for t in grid}) == 35
    assert GRID == (0.50, 0.60, 0.70, 0.80, 0.90, 0.95) and Fraction(2, 40) == FSR_CAP
    assert [c.id for c in CANDIDATES] == ["N1", "N2", "N3", "N4"]
    assert all(len(c.revision) == 40 for c in CANDIDATES)
    # the S0 pins (docs/milestones/M4_PLAN.md, S0 outcome)
    assert CANDIDATES[1].revision == "fa2804872c3b4bd748f38c0185cc85775361e735"


def metrics(
    *,
    recall: Fraction = Fraction(1, 2),
    fsr: Fraction = Fraction(0),
    fcr: Fraction = Fraction(0),
    macro: Fraction = Fraction(1, 2),
    mfp: int = 0,
    form: int = 0,
) -> Any:
    cases = [case(id="z")]
    m = compute_metrics(cases, [pred("z", "entailed")], {"z": set()})
    return replace(m, recall=m.recall | {"entailed": recall}, false_support_rate=fsr,
                   false_contradiction_rate=fcr, macro_f1=macro,
                   mandatory_false_positives=mfp, obligation_from_form_errors=form)  # fmt: skip


def outcome(cid: str = "N2", te: float = 0.9, tc: float = 0.9, size: int = 1, **kw: Any) -> Outcome:
    flags = {k: kw.pop(k) for k in ("sanity_ok", "deterministic", "p50_ms") if k in kw}
    return Outcome(
        Candidate(cid, f"m/{cid}", "a" * 40, size),
        Thresholds(te, tc, f"t:{cid}:{te}:{tc}"),
        metrics(**kw),
        sanity_ok=flags.get("sanity_ok", True),
        deterministic=flags.get("deterministic", True),
        p50_ms=flags.get("p50_ms", 10.0),
    )


@pytest.mark.parametrize(
    ("kw", "failure"),
    [
        ({"mfp": 1}, "mandatory_false_positive"),
        ({"form": 1}, "obligation_from_form"),
        ({"sanity_ok": False}, "sanity_triplet"),
        ({"deterministic": False}, "non_deterministic"),
    ],
)
def test_every_hard_gate_disqualifies(kw: dict[str, Any], failure: str) -> None:
    o = outcome(recall=Fraction(1), **kw)
    assert failure in o.gate_failures and not o.qualifies
    assert decide([o]).selected is None


def test_the_false_support_cap_boundary() -> None:
    assert outcome(fsr=Fraction(2, 40)).qualifies
    assert not outcome(fsr=Fraction(3, 40)).qualifies


def test_nothing_qualifying_selects_nothing() -> None:
    d = decide([outcome(fsr=Fraction(1, 2)), outcome(mfp=2)])
    assert d.selected is None and d.qualifiers == []


@pytest.mark.parametrize(
    ("better", "worse"),
    [
        ({"recall": Fraction(3, 4)}, {"recall": Fraction(1, 2)}),  # objective
        ({"fcr": Fraction(0)}, {"fcr": Fraction(1, 10)}),  # 1st tie-break
        ({"macro": Fraction(3, 4)}, {"macro": Fraction(1, 2)}),  # 2nd
        ({"size": 0}, {"size": 2}),  # 3rd: smaller model
        ({"te": 0.95}, {"te": 0.9}),  # 4th: higher tau_e
        ({"tc": 0.95}, {"tc": 0.9}),  # 5th: higher tau_c
        ({"p50_ms": 5.0}, {"p50_ms": 50.0}),  # 6th: lower latency
        ({"cid": "N1"}, {"cid": "N2"}),  # 7th: candidate id
    ],
)
def test_each_tie_break_in_registered_order(better: dict[str, Any], worse: dict[str, Any]) -> None:
    a, b = outcome(**better), outcome(**worse)
    assert decide([b, a]).selected is a
    assert decide([a, b]).selected is a


def test_an_earlier_criterion_beats_every_later_one() -> None:
    higher_recall = outcome(recall=Fraction(3, 4), fcr=Fraction(1, 10), size=2, te=0.5)
    lower_recall = outcome(recall=Fraction(1, 2), fcr=Fraction(0), size=0, te=0.95)
    assert decide([lower_recall, higher_recall]).selected is higher_recall


# --- the record and the runtime loader ----------------------------------------------------


def test_the_record_is_written_once(tmp_path: Path) -> None:
    path = tmp_path / "sel.json"
    write_record(path, {"protocol": PROTOCOL_VERSION, "selected": None})
    with pytest.raises(FileExistsError):
        write_record(path, {"protocol": PROTOCOL_VERSION, "selected": None})


def test_the_loader_returns_exactly_the_recorded_thresholds(tmp_path: Path) -> None:
    o = outcome(cid="N2", te=0.8, tc=0.95)
    o = replace(o, candidate=CANDIDATES[1])
    path = tmp_path / "sel.json"
    write_record(path, {"protocol": PROTOCOL_VERSION, "selected": outcome_json(o)})
    sel = load_selected(path)
    assert sel is not None
    assert (sel.model_id, sel.revision) == (CANDIDATES[1].model_id, CANDIDATES[1].revision)
    assert (sel.thresholds.entail, sel.thresholds.contradict) == (0.8, 0.95)
    assert sel.thresholds.version == o.thresholds.version


def test_no_record_or_no_selection_means_no_thresholds(tmp_path: Path) -> None:
    assert load_selected(tmp_path / "missing.json") is None
    path = tmp_path / "none.json"
    path.write_text(json.dumps({"protocol": PROTOCOL_VERSION, "selected": None}))
    assert load_selected(path) is None
    path.write_text(json.dumps({"protocol": "other", "selected": None}))
    with pytest.raises(ValueError, match="protocol"):
        load_selected(path)
