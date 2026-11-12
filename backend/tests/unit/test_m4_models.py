"""M4 S1: claim-verification models, report v2, and strict compatibility with M3 (v1) reports.

No graph behaviour changes in S1: these tests pin the data contracts that S2-S7 build on.
- Every committed M3 report fixture (captured from the unchanged M3 code before S1) and any
  local M3 demo record still validate as report v1, unchanged.
- The never-upgrade invariant holds at every layer that carries an applicability.
- MANDATORY can only survive verification on a supported unconditional obligation claim,
  judged against eligible, active, entity-scope primary evidence that reads as an
  affirmative obligation naming the item (M4_PLAN §E). Form evidence never establishes it.
"""

import itertools
import json
import math
import pathlib
from typing import Any

import pytest
from pydantic import ValidationError

from app.graph.state import (
    ALLOWED_ROLES,
    APPLICABILITY_STRICTNESS,
    DISCLAIMER,
    INPUT_KEYS,
    NLI_LABELS,
    NOTICE,
    NOTICE_V2,
    STATE_KEYS,
    AtomicClaim,
    Citation,
    ClaimType,
    ClaimVerdict,
    ComplianceReport,
    Degradation,
    EvidenceJudgement,
    EvidenceRole,
    RequirementVerification,
    StatementAnalysis,
    UnverifiedItem,
    ValidatedRequirement,
    VerificationMeta,
    dump,
    dump_list,
    evidence_role,
    never_upgraded,
    validate_state,
)
from app.graph.validation import STRICTNESS
from app.schemas.taxonomy import Applicability, DocumentType, EvidenceEligibility

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "m3_reports_v1"
LOCAL_DEMO = pathlib.Path(__file__).resolve().parents[3] / "data" / "processed" / "m3_demo"
A = Applicability
ALL_APPLICABILITIES = list(Applicability)

# --- builders (synthetic ids; no corpus text) -------------------------------------------------


def claim(**kw: Any) -> AtomicClaim:
    base: dict[str, Any] = {
        "claim_id": "REQ_001-C0",
        "requirement_id": "REQ_001",
        "claim_type": "policy_obligation",
        "asserted_strength": "unconditional",
        "text": "The trust deed must be obtained for a trust customer.",
        "template_version": "t1",
    }
    return AtomicClaim(**(base | kw))


def analysis(**kw: Any) -> StatementAnalysis:
    base: dict[str, Any] = {
        "modality": "obligation",
        "polarity": "affirmative",
        "object_match": True,
        "subject": "regulated entity",
        "scope": "match",
        "cues": ["shall"],
    }
    return StatementAnalysis(**(base | kw))


def probs_of(entail: float, contra: float) -> list[float]:
    return [entail, 1.0 - entail - contra, contra]  # NLI_LABELS order


PRIMARY: dict[str, Any] = {
    "evidence_id": "c-rbi",
    "document_key": "rbi_kyc_directions_2025",
    "document_type": "regulatory",
    "page": 38,
    "window_start": 0,
    "window_end": 120,
    "evidence_role": "primary_policy",
    "evidence_eligibility": "primary",
    "match_scope": "entity",
    "text_reliability": "native",
    "source_status": "active",
    "can_support_mandatory": True,
}
FORM: dict[str, Any] = PRIMARY | {
    "evidence_id": "c-form",
    "document_key": "hdfc_non_individual_account_opening",
    "document_type": "form",
    "page": 1,
    "evidence_role": "form",
    "evidence_eligibility": "supporting",
    "source_status": "unknown",
    "can_support_mandatory": False,
}


def judgement(source: dict[str, Any] = PRIMARY, **kw: Any) -> EvidenceJudgement:
    base: dict[str, Any] = source | {
        "claim_id": "REQ_001-C0",
        "analysis": analysis(),
        "logits": [3.1, -0.4, -2.2],
        "probs": probs_of(0.96, 0.01),
    }
    return EvidenceJudgement(**(base | kw))


def verdict(**kw: Any) -> ClaimVerdict:
    base: dict[str, Any] = {
        "claim_id": "REQ_001-C0",
        "verdict": "supported",
        "best_evidence_id": "c-rbi",
    }
    return ClaimVerdict(**(base | kw))


def verification(**kw: Any) -> RequirementVerification:
    base: dict[str, Any] = {
        "requirement_id": "REQ_001",
        "claims": [claim()],
        "judgements": [judgement()],
        "verdicts": [verdict()],
        "requirement_verdict": "supported",
        "applicability_before": "MANDATORY",
        "applicability_after": "MANDATORY",
    }
    return RequirementVerification(**(base | kw))


def citation(source: dict[str, Any] = PRIMARY) -> Citation:
    return Citation(
        evidence_id=source["evidence_id"],
        document_key=source["document_key"],
        page=source["page"],
        match_scope=source["match_scope"],
        evidence_eligibility=source["evidence_eligibility"],
        text_reliability=source["text_reliability"],
        source_status=source["source_status"],
        can_support_mandatory=source["can_support_mandatory"],
    )


def item(req_id: str = "REQ_001", **kw: Any) -> ValidatedRequirement:
    base: dict[str, Any] = {
        "requirement_id": req_id,
        "name": "Trust deed",
        "explanation": "Paragraph 35 lists the trust deed.",
        "applicability": "MANDATORY",
        "proposed_applicability": "MANDATORY",
        "citations": [citation()],
        "uncertainty_label": "label",
    }
    return ValidatedRequirement(**(base | kw))


META_RAN = VerificationMeta(
    status="ran",
    model_id="cross-encoder/nli-deberta-v3-small",
    model_revision="fa2804872c3b4bd748f38c0185cc85775361e735",
    label_map={"0": "contradiction", "1": "entailment", "2": "neutral"},
    templates_version="t1",
    thresholds_version="th1",
    pairs_scored=3,
)


def v2_report(**kw: Any) -> ComplianceReport:
    """A v2 report: one verified MANDATORY item and one unsupported item set aside."""
    unsupported = verification(
        requirement_id="REQ_002",
        claims=[claim(claim_id="REQ_002-C0", requirement_id="REQ_002")],
        judgements=[judgement(claim_id="REQ_002-C0", probs=probs_of(0.1, 0.05))],
        verdicts=[verdict(claim_id="REQ_002-C0", verdict="insufficient", best_evidence_id=None)],
        requirement_verdict="unsupported",
        applicability_before="CONDITIONAL",
        applicability_after="UNCLEAR",
        review_flags=["unsupported"],
    )
    base: dict[str, Any] = {
        "report_version": 2,
        "report_id": "r-1",
        "outcome": "CHECKLIST_REVIEW_REQUIRED",
        "notice": NOTICE_V2,
        "user_message": "Checklist.\n\n" + DISCLAIMER,
        "checklist": [
            item(verification_status="supported", explanation_status="verified_supported")
        ],
        "claim_verification": [verification(), unsupported],
        "verification_meta": META_RAN,
        "verification": "claim_check_ran",
        "unverified_items": [
            UnverifiedItem(
                requirement_id="REQ_002",
                name="Item",
                applicability_before="CONDITIONAL",
                citations=[citation()],
                reason_codes=["not_entailed"],
            )
        ],
    }
    return ComplianceReport(**(base | kw))


# --- M3 (v1) compatibility ----------------------------------------------------------------------


def fixture_reports() -> list[pathlib.Path]:
    return sorted(FIXTURES.glob("*.json"))


def test_the_v1_fixtures_cover_every_outcome() -> None:
    outcomes = {json.loads(p.read_text(encoding="utf-8"))["outcome"] for p in fixture_reports()}
    assert outcomes == {
        "BLOCKED",
        "INSUFFICIENT_INFORMATION",
        "INSUFFICIENT_EVIDENCE",
        "DEGRADED",
        "CHECKLIST_REVIEW_REQUIRED",
    }


@pytest.mark.parametrize("path", fixture_reports(), ids=lambda p: p.stem)
def test_every_m3_report_fixture_still_validates_unchanged(path: pathlib.Path) -> None:
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert "report_version" not in stored  # captured from M3 itself
    report = ComplianceReport.model_validate(stored)
    assert report.report_version == 1 and report.notice == NOTICE
    assert report.verification == "not_claim_verified" and report.claim_verification is None
    dumped = report.model_dump(mode="json")
    # every stored value survives as-is; S1 only adds the v1 defaults of the new fields
    assert {k: dumped[k] for k in stored} == stored
    assert set(dumped) - set(stored) == {
        "report_version",
        "claim_verification",
        "verification_meta",
        "unverified_items",
    }
    assert ComplianceReport.model_validate(dumped) == report


def test_the_mandatory_fixture_really_exercises_the_m3_gate() -> None:
    stored = json.loads((FIXTURES / "checklist_mandatory.json").read_text(encoding="utf-8"))
    pairs = {(i["proposed_applicability"], i["applicability"]) for i in stored["checklist"]}
    assert ("MANDATORY", "MANDATORY") in pairs and ("MANDATORY", "CONDITIONAL") in pairs


def local_demo_reports() -> list[pathlib.Path]:
    return sorted(LOCAL_DEMO.glob("*.json")) if LOCAL_DEMO.is_dir() else []


@pytest.mark.skipif(not local_demo_reports(), reason="no local M3 demo records (gitignored)")
def test_local_m3_demo_and_live_reports_still_validate() -> None:
    for path in local_demo_reports():
        stored = json.loads(path.read_text(encoding="utf-8"))["report"]
        report = ComplianceReport.model_validate(stored)
        assert report.report_version == 1
        assert {k: report.model_dump(mode="json")[k] for k in stored} == stored


@pytest.mark.parametrize(
    "patch",
    [
        {"notice": NOTICE_V2},
        {"verification": "claim_check_ran"},
        {"verification_meta": dump(VerificationMeta(status="not_configured"))},
        {"claim_verification": []},
        {"unverified_items": [dump(UnverifiedItem(requirement_id="REQ_009", name="x",
                                                  applicability_before="UNCLEAR",
                                                  citations=[citation()]))]},  # fmt: skip
    ],
    ids=["notice", "verification", "meta", "results", "unverified"],
)
def test_a_v1_report_cannot_carry_m4_fields(patch: dict[str, Any]) -> None:
    stored = json.loads((FIXTURES / "checklist_mandatory.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="v1"):
        ComplianceReport.model_validate(stored | patch)


def test_a_v1_checklist_item_cannot_claim_verification() -> None:
    stored = json.loads((FIXTURES / "checklist_mandatory.json").read_text(encoding="utf-8"))
    stored["checklist"][0]["verification_status"] = "supported"
    with pytest.raises(ValidationError, match="v1 checklist"):
        ComplianceReport.model_validate(stored)


def test_m3_build_report_output_is_unchanged_v1() -> None:
    """Without the M4 stage's output, the report builder still yields the exact M3 v1 report
    (M4 S5: the graph now runs claim_verification, which makes the report v2)."""
    from app.graph.report import build_report
    from app.graph.routing import decide_outcome
    from tests.unit.test_graph_s4 import RBI_TRUST, cid
    from tests.unit.test_graph_s5 import TRUST_INTAKE, TRUST_Q, draft, req, run_graph

    out = run_graph({"TRUST": [RBI_TRUST]}, draft(req([cid(RBI_TRUST)])), TRUST_INTAKE, TRUST_Q)
    m3_state = {k: v for k, v in out.items()
                if k not in ("claim_results", "verification_meta", "unverified_items")}  # fmt: skip
    m3_state["degradations"] = [d for d in out["degradations"] if d["component"] != "verification"]
    report = build_report(m3_state, decide_outcome(m3_state)).model_dump(mode="json")
    assert report["report_version"] == 1 and report["notice"] == NOTICE
    assert report["outcome"] == "CHECKLIST_REVIEW_REQUIRED"
    assert report["checklist"][0]["verification_status"] == "not_claim_verified"
    assert report["checklist"][0]["applicability"] == "MANDATORY"
    assert out["report"]["report_version"] == 2  # the graph run itself


# --- report v2 --------------------------------------------------------------------------------


def test_a_consistent_v2_report_validates_and_round_trips() -> None:
    report = v2_report()
    text = json.dumps(report.model_dump(mode="json"))
    assert ComplianceReport.model_validate(json.loads(text)) == report


def test_a_v2_report_without_verification_is_honestly_unverified() -> None:
    for status, label in (
        ("not_configured", "not_claim_verified"),
        ("failed", "claim_check_failed"),
    ):
        r = v2_report(
            checklist=[item()],
            claim_verification=None,
            unverified_items=[],
            verification_meta=VerificationMeta(status=status),
            verification=label,
        )
        assert (
            r.checklist is not None and r.checklist[0].verification_status == "not_claim_verified"
        )
    # a terminal path that never reached verification (BLOCKED, INSUFFICIENT_*) has no meta
    v2_report(checklist=None, claim_verification=None, unverified_items=[],
              verification_meta=None, verification="not_claim_verified")  # fmt: skip


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"notice": NOTICE}, "M4 notice"),
        ({"verification": "not_claim_verified"}, "agree"),
        ({"verification_meta": None, "verification": "not_claim_verified"}, "need their"),
        ({"unverified_items": []}, "exactly one result"),
        ({"checklist": [item(), item("REQ_002")]}, "either in the checklist"),
        ({"checklist": [item(applicability="CONDITIONAL", verification_status="supported")]},
         "verified applicability"),
        ({"checklist": [item(verification_status="partially_supported")]}, "requirement verdict"),
        ({"checklist": [item(applicability="CONDITIONAL", proposed_applicability="CONDITIONAL",
                             verification_status="supported")]},
         "verified applicability"),
    ],  # fmt: skip
)
def test_inconsistent_v2_reports_are_rejected(patch: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        v2_report(**patch)


def test_only_unsupported_requirements_are_set_aside() -> None:
    contradicted = verification(
        requirement_id="REQ_002",
        claims=[claim(claim_id="REQ_002-C0", requirement_id="REQ_002")],
        judgements=[judgement(claim_id="REQ_002-C0", probs=probs_of(0.02, 0.9))],
        verdicts=[verdict(claim_id="REQ_002-C0", verdict="contradicted", best_evidence_id=None)],
        requirement_verdict="contradicted",
        applicability_before="CONDITIONAL",
        applicability_after="UNCLEAR",
        review_flags=["contradicted"],
    )
    with pytest.raises(ValidationError, match="only unsupported"):
        v2_report(claim_verification=[verification(), contradicted])


def test_a_v2_checklist_cannot_hold_an_unsupported_requirement() -> None:
    unsupported = verification(
        verdicts=[verdict(verdict="insufficient", best_evidence_id=None)],
        requirement_verdict="unsupported",
        applicability_after="UNCLEAR",
    )
    with pytest.raises(ValidationError, match="listed as unverified"):
        v2_report(
            checklist=[item(applicability="UNCLEAR", verification_status="unsupported")],
            claim_verification=[unsupported],
            unverified_items=[],
        )


# --- never upgrade --------------------------------------------------------------------------------


def test_one_strictness_order_is_shared_with_the_m3_validator() -> None:
    assert STRICTNESS is APPLICABILITY_STRICTNESS
    assert sorted(ALL_APPLICABILITIES, key=APPLICABILITY_STRICTNESS.__getitem__) == [
        A.UNCLEAR,
        A.OPTIONAL,
        A.CONDITIONAL,
        A.MANDATORY,
    ]


@pytest.mark.parametrize(
    ("before", "after"), list(itertools.product(ALL_APPLICABILITIES, repeat=2))
)
def test_verification_never_upgrades_applicability(before: A, after: A) -> None:
    # the "after" value is kept reachable for every other rule: weak verdicts, no MANDATORY
    kw: dict[str, Any] = {"applicability_before": before, "applicability_after": after}
    if after != A.MANDATORY:
        kw |= {"requirement_verdict": "partially_supported"}
    if never_upgraded(before, after):
        verification(**kw)
    else:
        with pytest.raises(ValidationError, match="never upgrade"):
            verification(**kw)


@pytest.mark.parametrize(
    ("proposed", "final"), list(itertools.product(ALL_APPLICABILITIES, repeat=2))
)
def test_a_validated_item_never_exceeds_its_proposal(proposed: A, final: A) -> None:
    if never_upgraded(proposed, final):
        item(applicability=final, proposed_applicability=proposed)
    else:
        with pytest.raises(ValidationError, match="exceed"):
            item(applicability=final, proposed_applicability=proposed)


# --- the MANDATORY rule inside RequirementVerification (M4_PLAN §E) -----------------------------


def test_a_fully_established_obligation_may_stay_mandatory() -> None:
    assert verification().applicability_after == A.MANDATORY


OCR_PRIMARY = PRIMARY | {"text_reliability": "ocr_uncertain", "can_support_mandatory": False}
DATED_PRIMARY = PRIMARY | {"source_status": "dated"}
GROUP_PRIMARY = PRIMARY | {"match_scope": "entity_group"}


@pytest.mark.parametrize(
    ("kw", "why"),
    [
        ({"judgements": [judgement(OCR_PRIMARY)]}, "ocr_uncertain primary"),
        ({"judgements": [judgement(DATED_PRIMARY)]}, "dated source"),
        ({"judgements": [judgement(GROUP_PRIMARY)]}, "entity-group scope"),
        ({"judgements": [judgement(analysis=None)]}, "no statement analysis"),
        ({"judgements": [judgement(analysis=analysis(polarity="negated"))]}, "negated"),
        ({"judgements": [judgement(analysis=analysis(polarity="ambiguous"))]}, "ambiguous"),
        ({"judgements": [judgement(analysis=analysis(modality="permission"))]}, "permission"),
        ({"judgements": [judgement(analysis=analysis(modality="conditional"))]}, "conditional"),
        ({"judgements": [judgement(analysis=analysis(modality="none"))]}, "no modality"),
        ({"judgements": [judgement(analysis=analysis(object_match=False))]}, "item not named"),
        ({"judgements": [judgement(analysis=analysis(scope="mismatch"))]}, "scope mismatch"),
        ({"judgements": [judgement(logits=None, probs=None, not_scored_reason="cap")]},
         "not scored"),
        ({"verdicts": [verdict(verdict="insufficient", best_evidence_id=None)]}, "not supported"),
        ({"claims": [claim(asserted_strength="conditional")]}, "conditional claim"),
        ({"requirement_verdict": "supported_weaker_than_asserted"}, "weaker verdict"),
        ({"requirement_verdict": "not_verified"}, "technical failure"),
    ],  # fmt: skip
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_mandatory_is_refused_unless_every_condition_holds(kw: dict[str, Any], why: str) -> None:
    with pytest.raises(ValidationError, match="MANDATORY"):
        verification(**kw)
    # the same evidence is acceptable once the item is downgraded
    verification(**kw | {"applicability_after": "CONDITIONAL"})


def test_an_obligation_keyword_alone_never_establishes_mandatory() -> None:
    """Strong deontic cues on a window whose NLI verdict is not `supported` change nothing."""
    j = judgement(analysis=analysis(cues=["shall", "must", "mandatory"]), probs=probs_of(0.2, 0.1))
    with pytest.raises(ValidationError, match="MANDATORY"):
        verification(
            judgements=[j], verdicts=[verdict(verdict="insufficient", best_evidence_id=None)]
        )


def test_form_evidence_can_never_be_judged_for_an_obligation() -> None:
    with pytest.raises(ValidationError, match="cannot be judged"):
        verification(judgements=[judgement(FORM)], verdicts=[verdict(best_evidence_id="c-form")])


def test_a_supported_form_request_never_establishes_mandatory() -> None:
    fr = claim(claim_type="form_request", asserted_strength="none",
               text="The form asks for the trust deed.")  # fmt: skip
    kw: dict[str, Any] = {
        "claims": [fr],
        "judgements": [judgement(FORM)],
        "verdicts": [verdict(best_evidence_id="c-form")],
        "requirement_verdict": "supported_weaker_than_asserted",
    }
    with pytest.raises(ValidationError, match="MANDATORY"):
        verification(**kw)
    assert verification(**kw | {"applicability_after": "CONDITIONAL"}).review_flags == []


def test_the_supporting_evidence_must_be_the_verdicts_best_evidence() -> None:
    other = judgement(PRIMARY | {"evidence_id": "c-other"}, analysis=analysis(polarity="negated"))
    with pytest.raises(ValidationError, match="MANDATORY"):
        verification(
            judgements=[judgement(), other], verdicts=[verdict(best_evidence_id="c-other")]
        )


# --- claim, role and judgement contracts ---------------------------------------------------


@pytest.mark.parametrize(
    ("document_type", "eligibility", "role"),
    [
        ("regulatory", "primary", "primary_policy"),
        ("policy", "primary", "primary_policy"),
        ("regulatory", "supporting", "supporting_reference"),  # dated RBI FAQ
        ("policy", "supporting", "supporting_reference"),  # dated HDFC FAQ
        ("annexure", "supporting", "supporting_reference"),
        ("form", "supporting", "form"),
        ("form", "primary", "form"),  # a form is never policy, whatever its eligibility
    ],
)
def test_evidence_role_follows_stored_metadata(
    document_type: str, eligibility: str, role: str
) -> None:
    assert evidence_role(DocumentType(document_type), EvidenceEligibility(eligibility)) == role


def test_a_judgement_role_must_match_its_metadata() -> None:
    with pytest.raises(ValidationError, match="evidence_role"):
        judgement(FORM | {"evidence_role": "primary_policy"})


@pytest.mark.parametrize(
    ("ctype", "role"), list(itertools.product(list(ClaimType), list(EvidenceRole)))
)
def test_claim_types_are_judged_only_against_allowed_evidence(
    ctype: ClaimType, role: EvidenceRole
) -> None:
    source = {
        EvidenceRole.PRIMARY_POLICY: PRIMARY,
        EvidenceRole.FORM: FORM,
        EvidenceRole.SUPPORTING_REFERENCE: PRIMARY | {
            "evidence_id": "c-faq", "document_key": "rbi_kyc_faqs",
            "evidence_eligibility": "supporting", "evidence_role": "supporting_reference",
            "source_status": "dated", "can_support_mandatory": False,
        },
    }[role]  # fmt: skip
    c = {
        ClaimType.POLICY_OBLIGATION: claim(),
        ClaimType.FORM_REQUEST: claim(claim_type="form_request", asserted_strength="none"),
        ClaimType.SUPPORTING_STATEMENT: claim(claim_type="supporting_statement",
                                              asserted_strength="none"),
        ClaimType.EXPLANATORY_CLAIM: claim(claim_type="explanatory_claim", template_version=None,
                                           source_sentence_index=0, asserted_strength="none"),
    }[ctype]  # fmt: skip
    kw: dict[str, Any] = {
        "claims": [c],
        "judgements": [judgement(source)],
        "verdicts": [verdict(best_evidence_id=source["evidence_id"])],
        "requirement_verdict": "partially_supported",
        "applicability_after": "CONDITIONAL",
    }
    if role in ALLOWED_ROLES[ctype]:
        verification(**kw)
    else:
        with pytest.raises(ValidationError, match="cannot be judged"):
            verification(**kw)


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"claim_type": "explanatory_claim"}, "source sentence index"),
        ({"source_sentence_index": 0}, "source sentence index"),
        ({"claim_type": "explanatory_claim", "source_sentence_index": 0}, "template version"),
        ({"template_version": None}, "template version"),
        ({"asserted_strength": "none"}, "unconditional or conditional"),
        ({"claim_type": "form_request"}, "assert no obligation"),
        ({"claim_type": "supporting_statement", "asserted_strength": "conditional"},
         "assert no obligation"),
        ({"text": ""}, "at least 1"),
        ({"text": "x" * 301}, "at most 300"),
    ],  # fmt: skip
)
def test_claim_shapes_are_enforced(kw: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        claim(**kw)


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"window_end": 0}, "non-empty|greater than or equal"),
        ({"window_start": 50, "window_end": 50}, "non-empty"),
        ({"page_end": 37}, "page_end"),
        ({"probs": None}, "together"),
        ({"logits": [1.0, 2.0]}, "exactly 3"),
        ({"logits": [math.nan, 0.0, 0.0]}, "finite"),
        ({"logits": [math.inf, 0.0, 0.0]}, "finite"),
        ({"probs": [0.5, 0.4, 0.2]}, "sum to 1"),
        ({"probs": [1.2, -0.1, -0.1]}, r"\[0, 1\]"),
        ({"not_scored_reason": "cap"}, "not_scored_reason"),
        ({"logits": None, "probs": None}, "not_scored_reason"),
        ({"can_support_mandatory": False}, "can_support_mandatory"),
        ({"score_semantics": "calibrated_confidence"}, "softmax_of_logits_uncalibrated"),
    ],
)
def test_judgement_contracts(kw: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        judgement(**kw)


def test_scores_are_stored_in_canonical_label_order() -> None:
    assert NLI_LABELS == ("entailment", "neutral", "contradiction")
    j = judgement()
    assert j.probs is not None and j.probs[0] == pytest.approx(0.96)


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"verdict": "supported", "best_evidence_id": None}, "best evidence"),
        ({"verdict": "conflicting", "best_evidence_id": None}, "best evidence"),
        ({"verdict": "not_verified"}, "no best evidence"),
        ({"verdict": "approved"}, "Input should be"),
    ],
)
def test_claim_verdict_contracts(kw: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        verdict(**kw)


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"claims": [claim(), claim()]}, "unique"),
        ({"claims": [claim(requirement_id="REQ_009")]}, "belongs"),
        ({"verdicts": []}, "one verdict per claim"),
        ({"claims": [], "verdicts": [], "judgements": []}, "no checkable claim"),
        ({"verdicts": [verdict(), verdict()]}, "one verdict per claim"),
        ({"judgements": [judgement(claim_id="REQ_001-C9")]}, "unknown claim"),
        ({"verdicts": [verdict(best_evidence_id="c-missing")]}, "one of its judgements"),
        ({"review_flags": ["unsupported", "unsupported"]}, "unique"),
        ({"review_flags": ["approved"]}, "Input should be"),
    ],
)
def test_requirement_verification_contracts(kw: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        verification(**kw)


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"model_revision": None}, "records model"),
        ({"label_map": {"0": "entailment", "1": "neutral", "2": "other"}}, "label map"),
        ({"label_map": {"0": "entailment", "1": "neutral"}}, "label map"),
    ],
)
def test_verification_meta_contracts(kw: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        VerificationMeta(**(META_RAN.model_dump() | kw))


def test_a_model_that_is_not_configured_scores_nothing() -> None:
    with pytest.raises(ValidationError, match="nothing is scored"):
        VerificationMeta(status="not_configured", pairs_scored=1)


def test_degradation_gains_only_the_verification_component() -> None:
    assert Degradation(component="verification", reason="not_configured")
    with pytest.raises(ValidationError):
        Degradation.model_validate({"component": "nli", "reason": "x"})


# --- state ----------------------------------------------------------------------------------------


def test_m4_state_keys_validate_round_trip_and_cannot_be_injected() -> None:
    keys = {"claim_results", "verification_meta", "unverified_items"}
    assert keys <= STATE_KEYS and not keys & INPUT_KEYS
    report = v2_report()
    state: dict[str, Any] = {
        "claim_results": dump_list(report.claim_verification or []),
        "verification_meta": dump(META_RAN),
        "unverified_items": dump_list(report.unverified_items),
        "report": dump(report),
    }
    validate_state(state)
    assert json.loads(json.dumps(state)) == state
    bad = state | {"claim_results": [dump(verification()) | {"applicability_before": "UNCLEAR"}]}
    with pytest.raises(ValueError, match="claim_results"):
        validate_state(bad)


def test_a_requirement_with_no_checkable_claim_is_only_ever_not_verified() -> None:
    """M4 S2: an UNCLEAR/OPTIONAL item citing only primary text asserts nothing checkable."""
    nothing = verification(
        claims=[],
        verdicts=[],
        judgements=[],
        requirement_verdict="not_verified",
        applicability_before="UNCLEAR",
        applicability_after="UNCLEAR",
        review_flags=["not_verified"],
    )
    assert nothing.claims == []
    with pytest.raises(ValidationError, match="MANDATORY|not_verified"):
        verification(claims=[], verdicts=[], judgements=[], requirement_verdict="not_verified")
