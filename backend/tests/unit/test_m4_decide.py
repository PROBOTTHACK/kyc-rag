"""M4 S2: the deterministic decision table, driven by FakeNLI (no real model).

Covers the window -> claim -> requirement table, every downgrade path, the separation of
form requests from obligations, failure handling (no scorer, scorer failure, pair cap),
and a seeded property sweep of the invariants: never upgrade, MANDATORY only when
`mandatory_established`, form evidence never judged for an obligation, determinism.
"""

import hashlib
import json
import random
from typing import Any

import pytest

from app.graph.state import (
    APPLICABILITY_STRICTNESS,
    ClaimType,
    EvidenceRole,
    RequirementVerification,
    mandatory_established,
    never_upgraded,
)
from app.ingestion.chunking import ChunkRecord
from app.verification.decide import Thresholds, requirement_verdict
from app.verification.nli import CONTRADICT, ENTAIL, FakeNLI, NLIPair, when
from tests.unit.m4_fixtures import dated, form, ocr_primary, primary, run

DEED_OBLIGATION = "For opening an account of a trust, the trust deed shall be obtained."


def entail_all() -> FakeNLI:
    return FakeNLI(rules=[lambda _pair: ENTAIL])


def verdicts(result: Any) -> dict[str, str]:
    return {v.claim_id: v.verdict for v in result.verification.verdicts}


# --- the supported path and its provenance -----------------------------------------------------


def test_a_primary_affirmative_obligation_naming_the_item_keeps_mandatory() -> None:
    r = primary(DEED_OBLIGATION)
    nli = FakeNLI(rules=[when("trust deed shall be obtained", "Trust deed must", ENTAIL)])
    out = run("Trust deed", [r], nli=nli).verification
    assert out.requirement_verdict == "supported"
    assert out.applicability_after == "MANDATORY" and out.review_flags == []
    j = out.judgements[0]
    assert j.evidence_id == r.metadata.chunk_id and j.page == r.metadata.page_number
    assert r.metadata.content[j.window_start : j.window_end] == DEED_OBLIGATION
    assert j.probs is not None and abs(sum(j.probs) - 1) < 1e-9
    assert j.score_semantics == "softmax_of_logits_uncalibrated"


def test_pairs_are_premise_window_then_claim_and_forms_never_meet_obligations() -> None:
    nli = entail_all()
    run("Trust deed", [primary(DEED_OBLIGATION), form("Trust deed copy attached.")], nli=nli)
    pairs = [p for call in nli.calls for p in call]
    assert pairs and all(isinstance(p, NLIPair) for p in pairs)
    obligation = [p for p in pairs if p.hypothesis.endswith("for a trust customer.")]
    assert obligation and all("copy attached" not in p.premise for p in obligation)
    asks = [p for p in pairs if p.hypothesis.startswith("The form asks for")]
    assert asks and all(p.premise in "Trust deed copy attached." for p in asks)


# --- form requests are not obligations ----------------------------------------------------


@pytest.mark.parametrize("applicability", ["MANDATORY", "CONDITIONAL"])
def test_form_evidence_never_establishes_an_obligation(applicability: str) -> None:
    out = run("Trust deed", [form("Attach the trust deed copy.")], nli=entail_all(),
              applicability=applicability, proposed="MANDATORY").verification  # fmt: skip
    v = {c.claim_type: out.verdicts[i].verdict for i, c in enumerate(out.claims)}
    assert v[ClaimType.POLICY_OBLIGATION] == "insufficient"
    assert v[ClaimType.FORM_REQUEST] == "supported"
    assert out.requirement_verdict == "supported_weaker_than_asserted"
    assert out.applicability_after == "CONDITIONAL"
    if applicability == "MANDATORY":  # defence in depth if M3's gate were ever bypassed
        assert "mandatory_not_established" in out.review_flags
    core = next(x for x in out.verdicts if x.claim_id.endswith("C0"))
    assert "no_primary_citation" in core.reason_codes


def test_a_dated_source_supports_only_a_supporting_statement() -> None:
    out = run("Trust deed", [dated("Q9. A trust deed is required for trusts.")],
              nli=entail_all(), applicability="CONDITIONAL",
              proposed="MANDATORY").verification  # fmt: skip
    assert out.requirement_verdict == "supported_weaker_than_asserted"
    assert out.applicability_after == "CONDITIONAL"
    assert {j.evidence_role for j in out.judgements} == {EvidenceRole.SUPPORTING_REFERENCE}


# --- deontic, negation, scope and subject --------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "proposed", "verdict", "after"),
    [
        (DEED_OBLIGATION, "MANDATORY", "supported", "MANDATORY"),
        ("The trust deed may be obtained from a trust.", "MANDATORY", "unsupported", "UNCLEAR"),
        ("Where applicable, the trust deed of a trust shall be obtained.", "MANDATORY",
         "unsupported", "UNCLEAR"),
        ("Where applicable, the trust deed of a trust shall be obtained.", "CONDITIONAL",
         "supported", "CONDITIONAL"),
        ("The trust deed of a trust shall be obtained except for small trusts.", "MANDATORY",
         "unsupported", "UNCLEAR"),
        ("For a partnership firm, the deed shall be obtained.", "MANDATORY", "unsupported",
         "UNCLEAR"),
        ("The PAN of the trust shall be obtained.", "MANDATORY", "unsupported", "UNCLEAR"),
        ("Trust: trust deed, PAN, registration certificate.", "MANDATORY", "unsupported",
         "UNCLEAR"),
    ],  # fmt: skip
)
def test_entailment_alone_never_supports_an_obligation(
    text: str, proposed: str, verdict: str, after: str
) -> None:
    """The fake model entails EVERYTHING: only the deterministic reading separates cases."""
    out = run("Trust deed", [primary(text)], nli=entail_all(), applicability=proposed).verification
    assert (out.requirement_verdict, out.applicability_after) == (verdict, after)


@pytest.mark.parametrize(
    "text",
    [
        "The trust deed shall not be obtained for a trust.",
        "A trust need not submit the trust deed.",
        "The trust deed is not required for a trust.",
    ],
)
def test_a_negated_obligation_naming_the_item_contradicts_it(text: str) -> None:
    out = run("Trust deed", [primary(text)], nli=FakeNLI()).verification  # model: neutral
    assert out.requirement_verdict == "contradicted"
    assert out.applicability_after == "UNCLEAR" and "contradicted" in out.review_flags
    assert out.verdicts[0].reason_codes == ["negated_obligation"]


def test_a_model_contradiction_about_another_item_is_not_a_contradiction() -> None:
    nli = FakeNLI(rules=[lambda _p: CONTRADICT])
    out = run("Trust deed", [primary("The PAN of the trust shall be obtained.")], nli=nli)
    assert out.verification.requirement_verdict == "unsupported"


def test_support_and_contradiction_together_are_conflicting_and_go_to_review() -> None:
    good, bad = primary(DEED_OBLIGATION), primary("The trust deed shall not be obtained.")
    out = run("Trust deed", [good, bad], nli=FakeNLI(rules=[
        when("deed shall be obtained", "must", ENTAIL)])).verification  # fmt: skip
    assert out.requirement_verdict == "conflicting"
    assert out.applicability_after == "UNCLEAR" and out.review_flags == ["conflicting_sources"]
    assert {j.evidence_id for j in out.judgements} == {
        good.metadata.chunk_id,
        bad.metadata.chunk_id,
    }


def test_the_docs05_example_a_different_person_and_item_is_never_supported() -> None:
    """Evidence "designated partners must be verified" vs claim "every employee ...",
    with a model that entails everything."""
    out = run(
        "Identity documents",
        [primary("Designated partners of the LLP must be verified.")],
        nli=entail_all(),
        entity="LLP",
        applicability="CONDITIONAL",
        explanation="Every employee of the LLP must submit identity documents.",
    ).verification
    expl = next(v for v in out.verdicts if v.claim_id.endswith("C1"))
    assert expl.verdict == "insufficient"
    assert {"subject_mismatch", "anchors_missing"} & set(expl.reason_codes)
    assert out.applicability_after == "UNCLEAR"


# --- eligibility and scope gates on MANDATORY --------------------------------------------------


@pytest.mark.parametrize(
    ("record", "scope"),
    [
        (lambda: primary(DEED_OBLIGATION), "entity_group"),
        (lambda: ocr_primary(DEED_OBLIGATION), "entity"),
    ],
    ids=["entity_group_scope", "ocr_uncertain"],
)
def test_supported_but_not_mandatory_capable_evidence_is_downgraded(
    record: Any, scope: str
) -> None:
    out = run("Trust deed", [record()], nli=entail_all(), scope=scope).verification
    assert out.requirement_verdict == "supported"
    assert out.applicability_after == "CONDITIONAL"
    assert out.review_flags == ["mandatory_not_established"]


def test_explanation_overstating_a_form_is_flagged() -> None:
    out = run(
        "PAN",
        [form("PAN of the trust must be attached.")],
        nli=entail_all(),
        applicability="CONDITIONAL",
        proposed="MANDATORY",
        explanation="The PAN must be attached for the trust.",
    ).verification
    assert "explanation_overstates_source" in out.review_flags
    assert out.applicability_after == "CONDITIONAL"


# --- failures are not_verified, never supported -----------------------------------------------


def test_without_a_scorer_nothing_is_verified_and_mandatory_cannot_stay() -> None:
    res = run("Trust deed", [primary(DEED_OBLIGATION)], nli=None)
    out = res.verification
    assert out.requirement_verdict == "not_verified" and out.applicability_after == "CONDITIONAL"
    assert out.review_flags == ["not_verified"]
    assert res.pairs_scored == 0 and res.pairs_unscored > 0
    assert all(j.not_scored_reason == "no_scorer" and j.probs is None for j in out.judgements)


def test_a_scorer_failure_is_not_verified() -> None:
    out = run("Trust deed", [primary(DEED_OBLIGATION)], nli=FakeNLI(fail=True)).verification
    assert out.requirement_verdict == "not_verified"
    assert out.verdicts[0].reason_codes == ["scorer_failed"]


def test_the_pair_cap_leaves_claims_not_verified_rather_than_insufficient() -> None:
    res = run("Trust deed", [primary(DEED_OBLIGATION + " The RE shall keep a record.")],
              nli=entail_all(), max_pairs=1)  # fmt: skip
    assert res.verification.requirement_verdict == "not_verified"
    assert res.verification.verdicts[0].reason_codes == ["pair_cap"]
    assert res.pairs_scored == 1


def test_a_scorer_returning_the_wrong_number_of_scores_counts_as_failure() -> None:
    class Short(FakeNLI):
        def score(self, pairs):  # type: ignore[no-untyped-def]
            return super().score(pairs)[:-1]

    out = run("Trust deed", [primary(DEED_OBLIGATION)], nli=Short(rules=[lambda _p: ENTAIL]))
    assert out.verification.requirement_verdict == "not_verified"


def test_an_item_asserting_nothing_checkable_is_not_verified() -> None:
    out = run("Trust deed", [primary(DEED_OBLIGATION)], nli=entail_all(),
              applicability="UNCLEAR").verification  # fmt: skip
    assert out.claims == [] and out.requirement_verdict == "not_verified"
    assert out.applicability_after == "UNCLEAR"


@pytest.mark.parametrize(("entail", "contradict"), [(0.0, 0.9), (1.0, 0.9), (0.5, 0.5), (0.4, 0.6)])
def test_thresholds_are_validated(entail: float, contradict: float) -> None:
    with pytest.raises(ValueError):
        Thresholds(entail, contradict, "v")
    with pytest.raises(ValueError, match="version"):
        Thresholds(0.8, 0.8, "")


def test_requirement_verdict_table() -> None:
    from app.graph.state import AtomicClaim, ClaimVerdict

    def c(i: int, t: str, s: str = "none") -> AtomicClaim:
        tmpl = None if t == "explanatory_claim" else "t1"
        idx = 0 if t == "explanatory_claim" else None
        return AtomicClaim(claim_id=f"R-C{i}", requirement_id="R", claim_type=t,
                           asserted_strength=s, text="x", template_version=tmpl,
                           source_sentence_index=idx)  # fmt: skip

    def v(i: int, label: str) -> ClaimVerdict:
        best = "e" if label in ("supported", "conflicting") else None
        return ClaimVerdict(claim_id=f"R-C{i}", verdict=label, best_evidence_id=best)

    core, fr, ex = c(0, "policy_obligation", "unconditional"), c(1, "form_request"), c(
        2, "explanatory_claim")  # fmt: skip
    cases = [
        ([core], ["supported"], "supported"),
        ([core, ex], ["supported", "insufficient"], "partially_supported"),
        ([core, ex], ["supported", "not_verified"], "partially_supported"),
        ([core, fr], ["insufficient", "supported"], "supported_weaker_than_asserted"),
        ([core, fr], ["insufficient", "insufficient"], "unsupported"),
        ([core, fr], ["not_verified", "supported"], "not_verified"),
        ([core, ex], ["supported", "contradicted"], "conflicting"),
        ([core], ["contradicted"], "contradicted"),
        ([core], ["conflicting"], "conflicting"),
        ([fr, ex], ["supported", "supported"], "supported"),
        ([fr, ex], ["supported", "insufficient"], "partially_supported"),
        ([fr, ex], ["insufficient", "not_verified"], "not_verified"),
        ([fr, ex], ["insufficient", "insufficient"], "unsupported"),
        ([], [], "not_verified"),
    ]
    for claims, labels, expected in cases:
        scripted = [v(int(cl.claim_id[-1]), lab) for cl, lab in zip(claims, labels, strict=True)]
        got = requirement_verdict(claims, scripted)
        assert got == expected, (labels, got)


# --- property sweep -----------------------------------------------------------------------------

POOL = [
    lambda: primary(DEED_OBLIGATION),
    lambda: primary("The trust deed shall not be obtained for a trust."),
    lambda: primary("The trust deed may be obtained from a trust."),
    lambda: primary("Where applicable, the trust deed of a trust shall be obtained."),
    lambda: primary("For a partnership firm, the deed shall be obtained."),
    lambda: primary("The PAN of the trust shall be obtained: (a) trust deed; (b) PAN."),
    lambda: ocr_primary(DEED_OBLIGATION),
    lambda: dated("Q9. A trust deed is required for trusts."),
    lambda: form("Attach the trust deed copy."),
    lambda: form("Fields: *Name of Trust | PAN of Trust"),
]
LEVELS = ["UNCLEAR", "OPTIONAL", "CONDITIONAL", "MANDATORY"]


def hashed_nli(seed: int) -> FakeNLI:
    def rule(pair: NLIPair) -> tuple[float, float, float]:
        h = hashlib.sha256(f"{seed}|{pair.premise}|{pair.hypothesis}".encode()).digest()
        return tuple((b - 128) / 32 for b in h[:3])  # type: ignore[return-value]

    return FakeNLI(rules=[rule])


def test_invariants_hold_for_random_evidence_scores_and_failures() -> None:
    rng = random.Random(20261112)  # noqa: S311 - seeded test inputs, not security
    for seed in range(300):
        records: list[ChunkRecord] = [rng.choice(POOL)() for _ in range(rng.randint(1, 3))]
        proposed = rng.choice(LEVELS)
        applicability = rng.choice(LEVELS[: LEVELS.index(proposed) + 1])
        mode = rng.choice(["score", "score", "score", "none", "fail", "cap"])
        nli = None if mode == "none" else hashed_nli(seed)
        if mode == "fail" and nli is not None:
            nli.fail = True
        kw: dict[str, Any] = {
            "nli": nli,
            "applicability": applicability,
            "proposed": proposed,
            "scope": rng.choice(["entity", "entity", "entity_group"]),
            "max_pairs": rng.randint(0, 6) if mode == "cap" else 500,
            "explanation": rng.choice(["", "The trust deed shall be obtained.",
                                       "Form No. 60 must be attached. It is optional."]),
        }  # fmt: skip
        res = run("Trust deed", records, **kw)
        out = res.verification
        context = (seed, mode, proposed, applicability, [r.metadata.content for r in records])
        # never upgrade
        assert never_upgraded(out.applicability_before, out.applicability_after), context
        assert APPLICABILITY_STRICTNESS[out.applicability_before] <= APPLICABILITY_STRICTNESS[
            out.applicability_before]  # fmt: skip
        # MANDATORY only when established, and only on primary entity-scope evidence
        if out.applicability_after == "MANDATORY":
            by_id = {c.claim_id: c for c in out.claims}
            assert mandatory_established(by_id, out.verdicts, out.judgements), context
            assert kw["scope"] == "entity" and nli is not None and not nli.fail, context
        # forms never judged for obligations; only-form evidence is never MANDATORY
        claim_type = {c.claim_id: c.claim_type for c in out.claims}
        for j in out.judgements:
            if claim_type[j.claim_id] == ClaimType.POLICY_OBLIGATION:
                assert j.evidence_role == EvidenceRole.PRIMARY_POLICY, context
        if all(r.metadata.document_type == "form" for r in records):
            assert out.applicability_after != "MANDATORY", context
        # without real scores nothing is supported
        if mode in ("none", "fail"):
            assert all(v.verdict != "supported" for v in out.verdicts), context
        # serialisable, re-validates, deterministic
        dumped = json.loads(json.dumps(out.model_dump(mode="json")))
        assert RequirementVerification.model_validate(dumped) == out
        again = run("Trust deed", records, **(kw | {"nli": hashed_nli(seed) if nli else None}))
        if mode != "fail":
            assert again.verification == out, context
