"""M4 S5: the `claim_verification` node in the graph, with NLI DISABLED by default.

S4 (`m4-s4-v1`) selected no NLI configuration, so the production dependencies carry no
scorer: every claim is `not_verified`, applicability can only fall (MANDATORY is never
established by an unverified claim), the run is DEGRADED with the M3 checklist, citations
and gaps kept, and the report (v2) says so without calling anything false or not required.

The enabled path is exercised only with the scripted `FakeNLI` (no model is downloaded,
loaded or called anywhere in this file). Fixtures: the synthetic corpus of test_graph_s4.
"""

import json
from typing import Any

import pytest

import app.verification.nli as nli_module
from app.config import Settings
from app.graph.builder import GraphDeps
from app.graph.nodes.claim_verification import (
    CORPUS_UNAVAILABLE,
    NLI_NOT_CONFIGURED,
    claim_verification,
)
from app.graph.report import DEGRADED_NOTE, VERIFICATION_OFF_NOTE, build_report
from app.graph.routing import decide_outcome
from app.graph.state import (
    APPLICABILITY_STRICTNESS,
    DISCLAIMER,
    NOTICE,
    NOTICE_V2,
    ComplianceReport,
    Outcome,
    RequirementVerification,
    validate_state,
)
from app.graph.synthesis import DraftRequirement
from app.verification.decide import Thresholds, not_verified_requirement
from app.verification.nli import FakeNLI
from app.workflow.acceptance import StoredChunk, check_report
from tests.unit.test_graph_s4 import NI_FIELDS, RBI_LEGAL, RBI_TRUST, FakeCorpus, cid
from tests.unit.test_graph_s5 import (
    LLP_INTAKE,
    LLP_Q,
    TRUST_INTAKE,
    TRUST_Q,
    assert_message_rules,
    draft,
    run_graph,
)

TRUST_DEED = "Certified copy of the trust deed"
ENTAIL = (8.0, 0.0, 0.0)
FAKE_T = Thresholds(0.9, 0.9, "test-only")


def trust_req(applicability: str = "MANDATORY", explanation: str = "") -> DraftRequirement:
    return DraftRequirement(
        name=TRUST_DEED,
        explanation=explanation or "The cited paragraph lists the trust deed.",
        applicability=applicability,
        evidence_ids=[cid(RBI_TRUST)],
    )


def trust_run(*reqs: DraftRequirement, **deps_kw: Any) -> dict[str, Any]:
    return run_graph(
        {"TRUST": [RBI_TRUST, NI_FIELDS]}, draft(*reqs or (trust_req(),)), TRUST_INTAKE,
        TRUST_Q, **deps_kw,
    )  # fmt: skip


def results(out: dict[str, Any]) -> list[RequirementVerification]:
    return [RequirementVerification.model_validate(r) for r in out["claim_results"]]


def strict(a: str) -> int:
    return APPLICABILITY_STRICTNESS[a]  # type: ignore[index]


# --- NLI disabled (the default) -------------------------------------------------------------


def test_every_claim_is_not_verified_and_nothing_is_scored() -> None:
    out = trust_run()
    validate_state(out)
    [r] = results(out)
    assert r.claims and r.judgements == []  # claims listed; no window judged
    assert {v.verdict for v in r.verdicts} == {"not_verified"}
    assert all(v.reason_codes == [NLI_NOT_CONFIGURED] and v.best_evidence_id is None
               for v in r.verdicts)  # fmt: skip
    assert r.requirement_verdict == "not_verified" and r.review_flags == ["not_verified"]
    meta = out["verification_meta"]
    assert meta["status"] == "not_configured" and meta["pairs_scored"] == 0
    assert meta["model_id"] is None and meta["thresholds_version"] is None


def test_mandatory_is_never_established_by_an_unverified_claim() -> None:
    out = trust_run()
    assert out["checklist"][0]["applicability"] == "MANDATORY"  # the M3 gate, kept in state
    [r] = results(out)
    assert (r.applicability_before, r.applicability_after) == ("MANDATORY", "CONDITIONAL")
    report = out["report"]
    assert all(i["applicability"] != "MANDATORY" for i in report["checklist"])


@pytest.mark.parametrize("proposed", ["MANDATORY", "CONDITIONAL", "OPTIONAL", "UNCLEAR"])
def test_applicability_is_never_upgraded(proposed: str) -> None:
    out = trust_run(trust_req(proposed))
    m3 = {i["requirement_id"]: i["applicability"] for i in out["checklist"]}
    for r in results(out):
        assert r.applicability_before == m3[r.requirement_id]
        assert strict(r.applicability_after) <= strict(r.applicability_before)
    for item in out["report"]["checklist"]:
        assert strict(item["applicability"]) <= strict(m3[item["requirement_id"]])


def test_a_negated_explanation_is_never_read_as_a_contradiction_without_nli() -> None:
    """With a scorer absent, the S2 rule that reads "need not ..." as a contradiction is NOT
    applied: a missing check is never turned into a finding that a claim is false."""
    req = trust_req(explanation="The bank need not obtain the trust deed of a trust.")
    out = trust_run(req)
    [r] = results(out)
    assert {v.verdict for v in r.verdicts} == {"not_verified"}
    assert r.requirement_verdict == "not_verified"
    assert out["report"]["unverified_items"] == []  # nothing moved out as unsupported


def test_the_run_is_degraded_with_the_m3_results_kept() -> None:
    out = trust_run()
    report = out["report"]
    assert_message_rules(report)
    assert report["outcome"] == Outcome.DEGRADED == decide_outcome(out)
    assert [d for d in report["degradations"]] == [
        {"component": "verification", "reason": NLI_NOT_CONFIGURED}
    ]
    m3_item, item = out["checklist"][0], report["checklist"][0]
    # citations (document, page, provenance), name, explanation, form links: as M3 made them
    for key in ("requirement_id", "name", "explanation", "citations", "related_form_fields",
                "proposed_applicability", "uncertainty_label"):  # fmt: skip
        assert item[key] == m3_item[key], key
    assert item["verification_status"] == "not_verified"
    assert item["explanation_status"] == "model_drafted_not_verified"
    assert report["evidence_status"] == "found" and report["forms"]


def test_the_v2_report_says_verification_was_unavailable_honestly() -> None:
    report = trust_run()["report"]
    ComplianceReport.model_validate(report)
    assert report["report_version"] == 2 and report["notice"] == NOTICE_V2
    assert report["verification"] == "not_claim_verified"
    assert report["verification_meta"]["status"] == "not_configured"
    assert report["human_review_required"] is True and report["disclaimer"] == DISCLAIMER
    message = report["user_message"]
    assert VERIFICATION_OFF_NOTE in message
    assert DEGRADED_NOTE.format(components="verification") in message
    assert message.endswith(DISCLAIMER)
    for word in ("contradict", "false", "not required", "approved", "rejected"):
        assert word not in message.lower()
    rid = report["checklist"][0]["requirement_id"]
    assert report["unresolved_items"][-2:] == [
        "claims_not_verified:1",
        f"applicability_lowered_by_verification:{rid}",
    ]


def test_partial_m3_evidence_gaps_stay_visible() -> None:
    from tests.unit.test_graph_s5 import req

    out = run_graph(
        {"LLP": [NI_FIELDS], "LEGAL_ENTITY": [RBI_LEGAL]},
        draft(req([cid(RBI_LEGAL)]), req([cid(NI_FIELDS)])),
        LLP_INTAKE,
        LLP_Q,
    )
    report = out["report"]
    assert_message_rules(report)
    assert report["evidence_status"] == "partial"
    assert "no_entity_specific_primary_evidence" in report["unresolved_items"]
    assert len(report["claim_verification"]) == len(report["checklist"]) == 2


def test_form_evidence_never_bears_an_obligation_claim() -> None:
    from tests.unit.test_graph_s5 import req

    out = run_graph(
        {"LLP": [NI_FIELDS], "LEGAL_ENTITY": [RBI_LEGAL]},
        draft(req([cid(NI_FIELDS)], "CONDITIONAL", name="PAN of the LLP")),
        LLP_INTAKE,
        LLP_Q,
    )
    [r] = results(out)
    assert "form_request" in {c.claim_type for c in r.claims}


def test_no_nli_model_is_built_or_loaded_whatever_the_settings_say(monkeypatch: Any) -> None:
    def refuse(*a: Any, **k: Any) -> Any:
        raise AssertionError("S5 must never build or load an NLI model")

    monkeypatch.setattr(nli_module.CrossEncoderNLI, "__init__", refuse)
    monkeypatch.setattr(nli_module, "nli_from_settings", refuse)
    settings = Settings(NLI_MODEL="cross-encoder/nli-deberta-v3-xsmall",
                        NLI_MODEL_REVISION="a" * 40)  # fmt: skip
    deps = GraphDeps.from_settings(settings)
    assert deps.nli is None and deps.nli_thresholds is None
    assert deps.nli_max_pairs == settings.NLI_MAX_PAIRS
    out = trust_run()  # the full graph, default dependencies
    assert out["verification_meta"]["status"] == "not_configured"


def test_a_scorer_needs_its_thresholds() -> None:
    with pytest.raises(ValueError, match="configured together"):
        GraphDeps(nli=FakeNLI())
    with pytest.raises(ValueError, match="configured together"):
        GraphDeps(nli_thresholds=FAKE_T)
    with pytest.raises(ValueError, match="nli_max_pairs"):
        GraphDeps(nli_max_pairs=-1)


# --- paths where the node does not run: M3 behaviour unchanged ---------------------------------


def test_nothing_surviving_validation_skips_verification_and_stays_v1() -> None:
    from tests.unit.test_graph_s5 import req

    out = run_graph({"TRUST": [RBI_TRUST]}, draft(req(["not-a-supplied-id"])), TRUST_INTAKE,
                    TRUST_Q)  # fmt: skip
    assert out["checklist"] == [] and "verification_meta" not in out
    report = out["report"]
    assert report["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert report["report_version"] == 1 and report["notice"] == NOTICE


def test_failed_synthesis_skips_verification_and_stays_v1() -> None:
    out = run_graph({"TRUST": [RBI_TRUST]}, ["not json", "{\"requirements\": 1}"], TRUST_INTAKE,
                    TRUST_Q)  # fmt: skip
    assert out["checklist"] is None and "verification_meta" not in out
    assert out["report"]["outcome"] == "DEGRADED" and out["report"]["report_version"] == 1


def test_the_node_on_an_empty_checklist_does_nothing() -> None:
    assert claim_verification({"checklist": []}, GraphDeps()) == {}
    assert claim_verification({}, GraphDeps()) == {}


# --- the enabled path, scripted scorer only (no model) ------------------------------------------


def test_with_a_scripted_scorer_the_check_runs_and_is_not_degraded() -> None:
    fake = FakeNLI(default=ENTAIL)
    out = trust_run(nli=fake, nli_thresholds=FAKE_T)
    validate_state(out)
    assert fake.calls and all(p.premise and p.hypothesis for c in fake.calls for p in c)
    meta = out["verification_meta"]
    assert meta["status"] == "ran" and meta["model_id"] == "fake-nli"
    assert meta["thresholds_version"] == "test-only" and meta["pairs_scored"] > 0
    report = out["report"]
    ComplianceReport.model_validate(report)
    assert report["verification"] == "claim_check_ran"
    assert report["outcome"] == "CHECKLIST_REVIEW_REQUIRED" and not report["degradations"]
    assert VERIFICATION_OFF_NOTE not in report["user_message"]
    [r] = results(out)
    item = report["checklist"][0]
    assert item["verification_status"] == r.requirement_verdict != "not_verified"
    assert item["applicability"] == r.applicability_after


def test_a_scripted_contradiction_moves_nothing_up_and_flags_review() -> None:
    out = trust_run(nli=FakeNLI(default=(0.0, 0.0, 8.0)), nli_thresholds=FAKE_T)
    [r] = results(out)
    assert r.requirement_verdict == "contradicted" and "contradicted" in r.review_flags
    assert r.applicability_after == "UNCLEAR"
    assert out["report"]["checklist"][0]["applicability"] == "UNCLEAR"


def test_a_scripted_neutral_scorer_lists_the_item_as_unverified_not_as_not_required() -> None:
    out = trust_run(nli=FakeNLI(), nli_thresholds=FAKE_T)  # neutral everywhere
    report = out["report"]
    ComplianceReport.model_validate(report)
    assert report["checklist"] == []
    [u] = report["unverified_items"]
    assert u["requirement_id"] == out["checklist"][0]["requirement_id"]
    assert u["citations"] == out["checklist"][0]["citations"]
    assert "not required" not in report["user_message"].lower()


def test_a_corpus_failure_fails_the_stage_closed() -> None:
    class Broken(FakeCorpus):
        def evidence(self, chunk_ids: Any) -> Any:
            raise ConnectionError("db down")

    state = trust_run()
    deps = GraphDeps(corpus=Broken(), nli=FakeNLI(default=ENTAIL), nli_thresholds=FAKE_T)
    update = claim_verification(state, deps)  # type: ignore[arg-type]
    assert update["verification_meta"]["status"] == "failed"
    [d] = [d for d in update["degradations"] if d["reason"].startswith(CORPUS_UNAVAILABLE)]
    assert d == {"component": "verification", "reason": f"{CORPUS_UNAVAILABLE}:ConnectionError"}
    for r in update["claim_results"]:
        assert r["requirement_verdict"] == "not_verified"
        assert {v["reason_codes"][0] for v in r["verdicts"]} == {CORPUS_UNAVAILABLE}


def test_not_verified_requirement_only_caps_and_flags() -> None:
    from app.graph.state import ValidatedRequirement

    out = trust_run()
    item = ValidatedRequirement.model_validate(out["checklist"][0])
    r = not_verified_requirement(item, [], reason="x")
    assert r.claims == [] and r.requirement_verdict == "not_verified"
    assert r.applicability_after == "CONDITIONAL" and r.review_flags == ["not_verified"]


# --- acceptance and report compatibility --------------------------------------------------------


def lookup(ids: Any) -> dict[str, StoredChunk]:
    return {i: StoredChunk("rbi_directions", 1, 99, True) for i in ids}


def test_the_m3_acceptance_check_accepts_a_v2_report_and_its_notice_only_for_v2() -> None:
    report = trust_run()["report"]
    assert check_report(report, lookup).ok
    v1_with_v2_notice = dict(report, report_version=1)
    assert "notice_or_disclaimer_missing" in check_report(v1_with_v2_notice, lookup).problems
    v2_with_m3_notice = dict(report, notice=NOTICE)
    assert "notice_or_disclaimer_missing" in check_report(v2_with_m3_notice, lookup).problems


def test_build_report_without_the_stage_output_is_the_m3_v1_report() -> None:
    out = trust_run()
    m3_state = {k: v for k, v in out.items()
                if k not in ("claim_results", "verification_meta", "unverified_items")}  # fmt: skip
    m3_state["degradations"] = []
    report = build_report(m3_state, decide_outcome(m3_state))
    assert report.report_version == 1 and report.outcome == Outcome.CHECKLIST_REVIEW_REQUIRED
    assert report.checklist is not None and report.checklist[0].applicability == "MANDATORY"
    assert json.dumps(report.model_dump(mode="json"))  # serialisable as stored
