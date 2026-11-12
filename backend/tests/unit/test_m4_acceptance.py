"""M4 S5 follow-up: `check_report` validates the whole report before any M3 rule, and a v2
report whose claim check was `not_configured` (NLI disabled) can never pass while looking
verified or MANDATORY. M3 v1 reports keep passing exactly as before."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app.graph.state import NOTICE, NOTICE_V2
from app.verification.nli import FakeNLI
from app.workflow.acceptance import SCHEMA_INVALID, StoredChunk, check_report
from tests.unit.test_m4_graph_s5 import ENTAIL, FAKE_T, trust_run

V1_FIXTURES = sorted((Path(__file__).parents[1] / "fixtures" / "m3_reports_v1").glob("*.json"))


def lookup(ids: Any) -> dict[str, StoredChunk]:
    """Every cited chunk exists and may support MANDATORY: isolates the M4 checks."""
    return {i: StoredChunk("", 1, 999, True) for i in ids}


def lookup_matching(report: dict[str, Any]) -> Any:
    keys = {
        c["evidence_id"]: c["document_key"]
        for i in report.get("checklist") or []
        for c in i["citations"]
    }

    def _lookup(ids: Any) -> dict[str, StoredChunk]:
        return {i: StoredChunk(keys[i], 1, 999, True) for i in ids if i in keys}

    return _lookup


@pytest.fixture(scope="module")
def nli_off() -> dict[str, Any]:
    return trust_run()["report"]  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def scripted() -> dict[str, Any]:
    """A v2 report whose claim check really ran (scripted scorer, no model)."""
    return trust_run(nli=FakeNLI(default=ENTAIL), nli_thresholds=FAKE_T)[  # type: ignore[no-any-return]
        "report"
    ]


def problems(report: dict[str, Any]) -> list[str]:
    return check_report(report, lookup_matching(report)).problems


# --- the real reports pass ----------------------------------------------------------------------


def test_the_nli_disabled_v2_report_passes(nli_off: dict[str, Any]) -> None:
    result = check_report(nli_off, lookup_matching(nli_off))
    assert result.ok, result.problems
    assert result.items == 1 and result.mandatory == 0


def test_a_v2_report_whose_check_ran_is_not_held_to_the_disabled_rules(
    scripted: dict[str, Any],
) -> None:
    assert check_report(scripted, lookup_matching(scripted)).ok


# What the M3 rules returned for each stored v1 report BEFORE this follow-up (verified
# against the previous check_report): unchanged. `fallback_text_missing` on the retrieval-
# failure report is a pre-existing M3 rule result, not introduced here.
M3_RESULTS = {"insufficient_evidence_retrieval_failed": ["fallback_text_missing"]}


@pytest.mark.parametrize("path", V1_FIXTURES, ids=lambda p: p.stem)
def test_every_stored_m3_v1_report_still_validates_with_no_m4_problem(path: Path) -> None:
    report = json.loads(path.read_text(encoding="utf-8"))
    assert problems(report) == M3_RESULTS.get(path.stem, [])


def test_v1_m3_rules_still_run_on_a_valid_v1_report() -> None:
    report = json.loads((V1_FIXTURES[0].parent / "checklist_mandatory.json").read_text("utf-8"))
    assert check_report(report, lambda ids: {}).problems == [
        "item_0:chunk_not_found",
        "item_0:mandatory_without_primary_citation",
        "item_1:chunk_not_found",
    ]


# --- malformed reports never pass ---------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda r: r.update(report_version=1), id="v2_fields_on_a_v1_report"),
        pytest.param(lambda r: r.update(notice=NOTICE), id="v2_with_the_m3_notice"),
        pytest.param(lambda r: r.update(verification_meta=None), id="results_without_meta"),
        pytest.param(lambda r: r.update(verification="claim_check_ran"), id="status_mismatch"),
        pytest.param(lambda r: r.update(claim_verification=None), id="items_without_results"),
        pytest.param(
            lambda r: r["checklist"][0].update(verification_status="not_claim_verified"),
            id="item_status_missing",
        ),
        pytest.param(
            lambda r: r["checklist"][0].update(applicability="MANDATORY"),
            id="item_not_matching_its_result",
        ),
        pytest.param(lambda r: r.update(unexpected_field=1), id="extra_field"),
        pytest.param(lambda r: r.pop("outcome"), id="missing_outcome"),
        pytest.param(lambda r: r.update(human_review_required=False), id="no_human_review"),
    ],
)
def test_a_malformed_report_fails_schema_validation_first(
    nli_off: dict[str, Any], mutate: Any
) -> None:
    report = copy.deepcopy(nli_off)
    mutate(report)
    result = check_report(report, lookup)
    assert result.problems[0] == SCHEMA_INVALID and not result.ok
    assert all(p.startswith(SCHEMA_INVALID) for p in result.problems)
    assert (result.items, result.mandatory, result.citations) == (0, 0, 0)


def test_schema_problems_name_locations_never_values(nli_off: dict[str, Any]) -> None:
    report = copy.deepcopy(nli_off)
    report["checklist"][0]["applicability"] = "SECRET-VALUE-123"
    found = check_report(report, lookup).problems
    assert f"{SCHEMA_INVALID}:checklist.0.applicability" in found
    assert "SECRET-VALUE-123" not in json.dumps(found)


def test_a_non_mapping_report_is_schema_invalid() -> None:
    assert check_report([], lookup).problems[0] == SCHEMA_INVALID  # type: ignore[arg-type]


# --- NLI disabled: nothing may look verified or MANDATORY ---------------------------------------


def forged_not_configured(scripted: dict[str, Any]) -> dict[str, Any]:
    """A report that passes every model invariant but claims `not_configured` while its items
    show a verified, MANDATORY result: only the explicit M4 acceptance rules catch it."""
    report = copy.deepcopy(scripted)
    report["verification"] = "not_claim_verified"
    report["verification_meta"] |= {
        "status": "not_configured",
        "pairs_scored": 0,
        "pairs_capped": 0,
    }
    report["degradations"] = []
    return report


def test_verified_looking_items_under_not_configured_are_refused(
    scripted: dict[str, Any],
) -> None:
    report = forged_not_configured(scripted)
    assert report["checklist"][0]["applicability"] == "MANDATORY"
    found = problems(report)
    assert not [p for p in found if p.startswith(SCHEMA_INVALID)]  # the model accepts it
    rid = report["checklist"][0]["requirement_id"]
    assert set(found) >= {
        "verification_meta_inconsistent",  # model id, revision, label map, thresholds
        "item_0:verification_status_not_not_verified",
        "item_0:mandatory_without_claim_verification",
        f"result_{rid}:verified_without_a_model",
        "verification_degradation_missing",
        "outcome_not_degraded_without_verification",
    }


def test_inconsistent_metadata_alone_is_refused(nli_off: dict[str, Any]) -> None:
    report = copy.deepcopy(nli_off)
    report["verification_meta"]["model_id"] = "some/model"
    assert problems(report) == ["verification_meta_inconsistent"]


def test_a_missing_verification_degradation_is_refused(nli_off: dict[str, Any]) -> None:
    report = copy.deepcopy(nli_off)
    report["degradations"] = []
    assert problems(report) == ["verification_degradation_missing"]


def test_a_not_configured_report_must_be_degraded(nli_off: dict[str, Any]) -> None:
    report = copy.deepcopy(nli_off)
    report["outcome"] = "CHECKLIST_REVIEW_REQUIRED"
    assert problems(report) == ["outcome_not_degraded_without_verification"]


def test_the_v2_notice_is_still_required(nli_off: dict[str, Any]) -> None:
    assert nli_off["notice"] == NOTICE_V2 and nli_off["report_version"] == 2
