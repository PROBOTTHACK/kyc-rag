"""M4 S8: `check_report` rules for every verification state (ran, partial, failed,
not_configured), on genuine reports and on forged or inconsistent ones.

Genuine reports come from real graph runs with the synthetic corpus: NLI disabled (the
production default) and the scripted `FakeNLI` (tests only; no model is loaded or called).
"""

import copy
import json
from typing import Any

import pytest

from app.graph.report import (
    CONFLICT_SCAN_FAILED_NOTE,
    CONFLICT_SCAN_NOTE,
    CONFLICT_SCAN_OFF_NOTE,
)
from app.verification.nli import FakeNLI
from app.workflow.acceptance import SCHEMA_INVALID, check_report
from tests.unit.test_m4_acceptance import M3_RESULTS, V1_FIXTURES, lookup_matching, problems
from tests.unit.test_m4_graph_s5 import ENTAIL, FAKE_T, trust_req, trust_run


def _merge(state: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    from app.graph.report import build_report
    from app.graph.routing import decide_outcome

    merged = state | update
    return build_report(merged, decide_outcome(merged)).model_dump(mode="json")


def failed_report() -> dict[str, Any]:
    """A real `failed` check: a scorer exists but the corpus read fails (node level)."""
    from app.graph.builder import GraphDeps
    from app.graph.nodes.claim_verification import claim_verification
    from tests.unit.test_graph_s4 import FakeCorpus

    class Broken(FakeCorpus):
        def evidence(self, chunk_ids: Any) -> Any:
            raise ConnectionError("down")

    state = trust_run(trust_req("CONDITIONAL"))
    deps = GraphDeps(corpus=Broken(), nli=FakeNLI(default=ENTAIL), nli_thresholds=FAKE_T)
    return _merge(state, claim_verification(state, deps))  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def reports() -> dict[str, dict[str, Any]]:
    from tests.unit.test_m4_conflicts import CONTRA, conflict_nli
    from tests.unit.test_m4_conflicts import run as conflict_run

    on: dict[str, Any] = {"nli_thresholds": FAKE_T}
    conditional = trust_req("CONDITIONAL")
    return {
        "ran_mandatory": trust_run(nli=FakeNLI(default=ENTAIL), **on)["report"],
        "ran_conditional": trust_run(conditional, nli=FakeNLI(default=ENTAIL), **on)["report"],
        "partial": trust_run(nli=FakeNLI(default=ENTAIL), nli_max_pairs=0, **on)["report"],
        "failed": failed_report(),
        "unsupported": trust_run(nli=FakeNLI(), **on)["report"],
        "conflict": conflict_run(nli=conflict_nli(), **on)["report"],
        "contradicted_and_conflict": conflict_run(nli=FakeNLI(default=CONTRA), **on)["report"],
        "not_configured": trust_run()["report"],
    }


def forge(reports: dict[str, dict[str, Any]], name: str) -> dict[str, Any]:
    return copy.deepcopy(reports[name])


# --- genuine reports pass -------------------------------------------------------------------------


def test_the_genuine_reports_cover_every_state(reports: dict[str, dict[str, Any]]) -> None:
    status = {r["verification_meta"]["status"] for r in reports.values()}
    assert status == {"ran", "partial", "failed", "not_configured"}
    assert reports["ran_mandatory"]["checklist"][0]["applicability"] == "MANDATORY"
    assert reports["conflict"]["source_conflicts"]
    assert reports["unsupported"]["unverified_items"]
    assert reports["failed"]["verification_meta"]["contradiction_scan"] == "failed"
    assert reports["partial"]["verification_meta"]["pairs_capped"] > 0


@pytest.mark.parametrize(
    "name",
    ["ran_mandatory", "ran_conditional", "partial", "failed", "unsupported", "conflict",
     "contradicted_and_conflict", "not_configured"],
)  # fmt: skip
def test_every_genuine_report_passes(reports: dict[str, dict[str, Any]], name: str) -> None:
    report = reports[name]
    result = check_report(report, lookup_matching(report))
    assert result.ok, (name, result.problems)


def test_nli_off_and_v1_behaviour_is_unchanged(reports: dict[str, dict[str, Any]]) -> None:
    off = reports["not_configured"]
    assert check_report(off, lookup_matching(off)).ok
    for path in V1_FIXTURES:
        report = json.loads(path.read_text(encoding="utf-8"))
        assert problems(report) == M3_RESULTS.get(path.stem, [])


# --- forged or inconsistent reports are refused ---------------------------------------------------


def test_a_check_that_ran_must_name_its_model(reports: dict[str, dict[str, Any]]) -> None:
    r = forge(reports, "failed")
    r["verification_meta"]["model_id"] = None  # the model allows it for `failed`
    assert "verification_provenance_missing" in problems(r)


@pytest.mark.parametrize("name", ["partial", "failed"])
def test_partial_and_failed_must_be_degraded(reports: dict[str, dict[str, Any]], name: str) -> None:
    r = forge(reports, name)
    r["degradations"] = [d for d in r["degradations"] if d["component"] != "verification"]
    assert "verification_degradation_missing" in problems(r)


def test_an_item_never_looks_verified_without_a_supported_claim(
    reports: dict[str, dict[str, Any]],
) -> None:
    r = forge(reports, "ran_conditional")
    assert r["checklist"][0]["verification_status"] in ("supported", "partially_supported")
    for v in r["claim_verification"][0]["verdicts"]:
        v.update(verdict="insufficient", best_evidence_id=None)
    found = problems(r)
    assert not [p for p in found if p.startswith(SCHEMA_INVALID)]  # the model accepts it
    assert "item_0:verified_without_a_supported_claim" in found


def test_mandatory_without_support_is_refused(reports: dict[str, dict[str, Any]]) -> None:
    r = forge(reports, "ran_mandatory")
    for v in r["claim_verification"][0]["verdicts"]:
        v.update(verdict="insufficient", best_evidence_id=None)
    assert problems(r)[0] == SCHEMA_INVALID  # the model itself refuses an unestablished one


def test_mandatory_needs_a_primary_citation(reports: dict[str, dict[str, Any]]) -> None:
    r = forge(reports, "ran_mandatory")
    for c in r["checklist"][0]["citations"]:
        c["evidence_eligibility"] = "supporting"
    found = problems(r)
    assert not [p for p in found if p.startswith(SCHEMA_INVALID)]  # the model accepts it
    assert "item_0:mandatory_not_established" in found


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["checklist"][0].update(applicability="MANDATORY"),
        lambda r: r["claim_verification"][0].update(applicability_after="MANDATORY"),
        lambda r: r["claim_verification"][0].update(applicability_before="MANDATORY"),
    ],
    ids=["shown_above_verified", "after_above_before", "before_above_proposed"],
)
def test_applicability_can_never_rise(reports: dict[str, dict[str, Any]], mutate: Any) -> None:
    """Every rise is refused by the report model already (schema); `check_report` repeats the
    monotonicity rule as a second layer, so no rise can pass either way."""
    r = forge(reports, "ran_conditional")
    mutate(r)
    assert problems(r)[0] == SCHEMA_INVALID


def test_a_conflict_flag_needs_its_sources(reports: dict[str, dict[str, Any]]) -> None:
    r = forge(reports, "ran_conditional")
    r["claim_verification"][0]["review_flags"].append("conflicting_sources")
    rid = r["claim_verification"][0]["requirement_id"]
    assert f"result_{rid}:conflict_flag_without_sources" in problems(r)


def test_a_conflict_stays_listed_for_review(reports: dict[str, dict[str, Any]]) -> None:
    r = forge(reports, "conflict")
    rid = r["source_conflicts"][0]["requirement_id"]
    r["unresolved_items"] = [u for u in r["unresolved_items"] if not u.startswith("conflicting")]
    assert f"conflict_{rid}:not_listed_for_review" in problems(r)


def test_a_conflict_must_show_two_sources(reports: dict[str, dict[str, Any]]) -> None:
    r = forge(reports, "conflict")
    c = r["source_conflicts"][0]
    c["side_b"] = c["side_a"] | {"document_key": c["side_b"]["document_key"], "cited": False}
    assert f"conflict_{c['requirement_id']}:one_sided" in problems(r)


def _notes(meta: dict[str, Any]) -> dict[str, str]:
    return {
        "OFF": CONFLICT_SCAN_OFF_NOTE,
        "FAILED": CONFLICT_SCAN_FAILED_NOTE,
        "BOUNDED": CONFLICT_SCAN_NOTE.format(
            pairs=meta["contradiction_scan_size"], skipped=meta["contradiction_pairs_not_scanned"]
        ),
    }


@pytest.mark.parametrize(
    ("name", "old", "new"),
    [("not_configured", "OFF", "FAILED"), ("conflict", "BOUNDED", "OFF"),
     ("failed", "FAILED", "OFF"), ("not_configured", "OFF", "BOUNDED")],
)  # fmt: skip
def test_the_scan_wording_must_match_the_recorded_status(
    reports: dict[str, dict[str, Any]], name: str, old: str, new: str
) -> None:
    r = forge(reports, name)
    notes = _notes(r["verification_meta"])
    assert notes[old] in r["user_message"]
    r["user_message"] = r["user_message"].replace(notes[old], notes[new])
    assert "conflict_scan_wording_mismatch" in problems(r)


def test_the_second_monotonicity_layer_catches_a_rise_the_model_did_not_see(
    reports: dict[str, dict[str, Any]],
) -> None:
    """Built without validation (model_construct), as a corrupted object could be."""
    from app.graph.state import ComplianceReport
    from app.workflow.acceptance import _v2_problems

    good = ComplianceReport.model_validate(reports["ran_conditional"])
    item = good.checklist[0]  # type: ignore[index]
    raised = item.model_copy(update={"applicability": "MANDATORY"})
    bad = good.model_copy(update={"checklist": [raised]})
    assert "item_0:applicability_above_verified" in _v2_problems(bad)
    result = good.claim_verification[0]  # type: ignore[index]
    rose = result.model_copy(update={"applicability_after": "MANDATORY"})
    bad = good.model_copy(update={"claim_verification": [rose]})
    assert f"result_{result.requirement_id}:applicability_upgraded" in _v2_problems(bad)
