"""M4 S7: open review cases derived from a report (pure function, no I/O).

Reports come from real graph runs with the synthetic corpus: NLI disabled (production) and
the scripted `FakeNLI` paths of S5/S6 (tests only; no model is loaded or called).
"""

import copy
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.graph.review import (
    REVIEW_CASE_NAMESPACE,
    REVIEW_REASONS,
    ReviewCase,
    review_case_id,
    review_cases,
)
from app.graph.state import ComplianceReport
from app.verification.nli import FakeNLI
from tests.unit.test_m4_conflicts import CONTRA, conflict_nli
from tests.unit.test_m4_conflicts import run as conflict_run
from tests.unit.test_m4_graph_s5 import ENTAIL, FAKE_T, trust_req, trust_run

V1_FIXTURES = sorted((Path(__file__).parents[1] / "fixtures" / "m3_reports_v1").glob("*.json"))


def model(report: dict[str, Any]) -> ComplianceReport:
    return ComplianceReport.model_validate(report)


def flags(report: dict[str, Any]) -> set[tuple[str, str]]:
    return {(r["requirement_id"], f) for r in report["claim_verification"] or []
            for f in r["review_flags"]}  # fmt: skip


def test_the_reasons_are_exactly_the_planned_codes() -> None:
    assert set(REVIEW_REASONS) == {
        "contradicted", "conflicting_sources", "unsupported", "mandatory_not_established",
        "explanation_overstates_source", "not_verified",
    }  # fmt: skip


# --- NLI disabled (production) ------------------------------------------------------------------


def test_every_not_verified_requirement_gets_its_own_case() -> None:
    out = trust_run(trust_req(), trust_req("CONDITIONAL"))
    report = model(out["report"])
    cases = review_cases(report)
    items = report.checklist or []
    assert len(items) == 2 and len(cases) == 2
    assert [c.requirement_id for c in cases] == sorted(i.requirement_id for i in items)
    for c in cases:
        assert (c.reason, c.status, c.claim_id) == ("not_verified", "open", None)
        assert (c.report_id, c.session_id, c.run_id) == (report.report_id, "s-1", "r-1")


def test_cases_are_exactly_the_report_flags() -> None:
    report = trust_run()["report"]
    assert {(c.requirement_id, c.reason) for c in review_cases(model(report))} == flags(report)


# --- several reasons (scripted scorer only) -------------------------------------------------------


def test_distinct_flags_give_distinct_cases_for_one_requirement() -> None:
    out = conflict_run(nli=FakeNLI(default=CONTRA), nli_thresholds=FAKE_T)
    report = model(out["report"])
    cases = review_cases(report)
    [rid] = {c.requirement_id for c in cases}
    assert {c.reason for c in cases} == {"contradicted", "conflicting_sources"}
    assert len({c.id for c in cases}) == len(cases) == 2
    assert {(c.requirement_id, c.reason) for c in cases} == flags(out["report"])
    assert rid == report.claim_verification[0].requirement_id  # type: ignore[index]


def test_a_conflict_alone_gives_one_conflicting_sources_case() -> None:
    report = conflict_run(nli=conflict_nli(), nli_thresholds=FAKE_T)["report"]
    reasons = [c.reason for c in review_cases(model(report))]
    assert "conflicting_sources" in reasons and reasons.count("conflicting_sources") == 1


def test_an_unsupported_item_gets_a_case_though_it_left_the_checklist() -> None:
    report = model(trust_run(nli=FakeNLI(), nli_thresholds=FAKE_T)["report"])
    assert report.checklist == [] and report.unverified_items
    [case] = review_cases(report)
    assert case.reason == "unsupported"
    assert case.requirement_id == report.unverified_items[0].requirement_id


def test_a_requirement_without_flags_gets_no_case() -> None:
    report = model(trust_run(nli=FakeNLI(default=ENTAIL), nli_thresholds=FAKE_T)["report"])
    assert all(not r.review_flags for r in report.claim_verification or [])
    assert review_cases(report) == []


def test_a_repeated_flag_never_creates_a_duplicate_case() -> None:
    report = model(trust_run()["report"])
    [r] = report.claim_verification or []
    doubled = r.model_construct(**(dict(r) | {"review_flags": ["not_verified", "not_verified"]}))
    raw = report.model_construct(**(dict(report) | {"claim_verification": [doubled, doubled]}))
    cases = review_cases(raw)
    assert len(cases) == 1 and cases[0].reason == "not_verified"


# --- reports without results ----------------------------------------------------------------------


@pytest.mark.parametrize("path", V1_FIXTURES, ids=lambda p: p.stem)
def test_v1_reports_have_no_cases(path: Path) -> None:
    assert review_cases(model(json.loads(path.read_text(encoding="utf-8")))) == []


def test_reports_where_verification_did_not_run_have_no_cases() -> None:
    from tests.unit.test_graph_s4 import RBI_TRUST
    from tests.unit.test_graph_s5 import TRUST_INTAKE, TRUST_Q, draft, req, run_graph

    nothing = run_graph({"TRUST": [RBI_TRUST]}, draft(req(["not-supplied"])), TRUST_INTAKE,
                        TRUST_Q)["report"]  # fmt: skip
    failed = run_graph({"TRUST": [RBI_TRUST]}, ["not json", '{"requirements": 1}'],
                       TRUST_INTAKE, TRUST_Q)["report"]  # fmt: skip
    for report in (nothing, failed):
        assert report["report_version"] == 1 and review_cases(model(report)) == []


# --- determinism and purity ----------------------------------------------------------------------


def test_ids_are_deterministic_and_order_is_stable() -> None:
    report = model(trust_run(trust_req(), trust_req("CONDITIONAL"))["report"])
    first, second = review_cases(report), review_cases(report)
    assert first == second
    for c in first:
        assert c.id == review_case_id(report.report_id, c.requirement_id, c.reason)
        name = f"{report.report_id}/{c.requirement_id}/{c.reason}"
        assert c.id == str(uuid.uuid5(REVIEW_CASE_NAMESPACE, name))
    assert [(c.requirement_id, c.reason) for c in first] == sorted(
        (c.requirement_id, c.reason) for c in first
    )


def test_derivation_never_changes_the_report() -> None:
    raw = trust_run()["report"]
    before = copy.deepcopy(raw)
    report = model(raw)
    dumped = report.model_dump(mode="json")
    review_cases(report)
    assert report.model_dump(mode="json") == dumped and raw == before


# --- the case model ------------------------------------------------------------------------------


def case(**kw: Any) -> dict[str, Any]:
    return {"id": "x", "report_id": "r", "session_id": "s", "run_id": "u",
            "requirement_id": "R1", "reason": "not_verified"} | kw  # fmt: skip


@pytest.mark.parametrize(
    "bad",
    [
        {"reason": "approved"},
        {"reason": "dismissed"},
        {"status": "closed"},
        {"status": "resolved"},
        {"claim_id": "R1-C0"},  # requirement-level in M4
        {"decision": "approve"},
        {"requirement_id": ""},
    ],
)
def test_a_case_can_only_be_open_and_carries_no_decision(bad: dict[str, Any]) -> None:
    ReviewCase.model_validate(case())
    with pytest.raises(ValidationError):
        ReviewCase.model_validate(case(**bad))


def test_a_case_is_immutable() -> None:
    c = ReviewCase.model_validate(case())
    with pytest.raises(ValidationError):
        c.status = "closed"  # type: ignore[assignment,misc]
