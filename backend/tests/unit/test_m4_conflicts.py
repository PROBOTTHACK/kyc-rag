"""M4 S6: the bounded source-conflict scan and the supersession rule (D10).

NLI stays disabled in production: the scan is `not_run`, scores nothing and reports nothing,
and the report says no automated conflict check was performed. Every scored path here uses
the scripted `FakeNLI` only (no model is downloaded, loaded or called).
"""

import asyncio
import copy
import itertools
import random
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from app.config import Settings
from app.db.repository import DocumentRelationRow
from app.graph.builder import GraphDeps, build_graph, run_config
from app.graph.inputs import start_input
from app.graph.report import (
    CONFLICT_SCAN_FAILED_NOTE,
    CONFLICT_SCAN_OFF_NOTE,
    VERIFICATION_OFF_NOTE,
)
from app.graph.state import (
    ComplianceReport,
    ConflictSide,
    RequirementVerification,
    SourceConflict,
    validate_state,
)
from app.schemas.taxonomy import (
    DocumentType,
    EvidenceEligibility,
    SourceStatus,
    TextReliability,
)
from app.verification.conflicts import (
    Candidate,
    ScanItem,
    candidate_pairs,
    resolve,
    scan,
    source_conflicts,
)
from app.verification.nli import FakeNLI, NLIPair
from app.verification.premises import Premise, Window
from app.workflow.acceptance import check_report
from tests.unit.test_forms_extract import rec
from tests.unit.test_graph_s4 import ALL, FAQ, RBI_TRUST, FakeCorpus, FakeRetriever, cid
from tests.unit.test_graph_s5 import TRUST_INTAKE, TRUST_Q, assert_message_rules, draft
from tests.unit.test_m4_graph_s5 import ENTAIL, FAKE_T, trust_req

CONTRA = (0.0, 0.0, 8.0)
PARA = RBI_TRUST.metadata.content_kind
# A dated FAQ passage naming the same item and saying the opposite (synthetic).
FAQ_TRUST = rec(FAQ, 7, "Q9. A copy of the trust deed need not be obtained for a trust.",
                section="Q", kind=PARA)  # fmt: skip
NO_SCAN_PHRASES = ("no conflict", "no conflicts", "conflict-free", "consistent")


def negation_disagrees(pair: NLIPair) -> tuple[float, float, float] | None:
    """Scripted reading: passages disagree when exactly one of them says "need not"."""
    return CONTRA if ("need not" in pair.premise) != ("need not" in pair.hypothesis) else None


def conflict_nli() -> FakeNLI:
    return FakeNLI(rules=[negation_disagrees], default=ENTAIL)


class RelationsCorpus(FakeCorpus):
    """The synthetic corpus plus supersession facts (default: no relation recorded)."""

    def __init__(self, relations: dict[str, DocumentRelationRow] | None = None,
                 fail: bool = False) -> None:  # fmt: skip
        super().__init__(records=[*ALL, FAQ_TRUST])
        self.relations = relations
        self.fail = fail
        self.relation_calls: list[list[str]] = []

    def document_relations(self, keys: Any) -> dict[str, DocumentRelationRow]:
        keys = list(keys)
        self.relation_calls.append(keys)
        if self.fail:
            raise ConnectionError("relations down: secret-detail")
        if self.relations is not None:
            return {k: v for k, v in self.relations.items() if k in keys}
        return {k: row(k) for k in keys}


def row(key: str, supersedes: tuple[str, ...] = (), status: str = "active",
        review: str = "not_required") -> DocumentRelationRow:  # fmt: skip
    return DocumentRelationRow(key, supersedes, status, review)


def run(corpus: Any = None, **deps_kw: Any) -> dict[str, Any]:
    from app.llm.fake import scripted_llm

    d = GraphDeps(
        retriever=FakeRetriever({"TRUST": [RBI_TRUST, FAQ_TRUST]}),
        corpus=corpus if corpus is not None else RelationsCorpus(),
        llm=scripted_llm(TRUST_INTAKE, draft(trust_req())),
        **deps_kw,
    )
    g = build_graph(d, checkpointer=InMemorySaver())
    out = asyncio.run(g.ainvoke(start_input(TRUST_Q, session_id="s-1", run_id="r-1"),
                                run_config(d, thread_id="t")))  # fmt: skip
    validate_state(out)
    return dict(out)


def results(out: dict[str, Any]) -> list[RequirementVerification]:
    return [RequirementVerification.model_validate(r) for r in out["claim_results"]]


# --- NLI disabled (production default) -----------------------------------------------------------


def test_with_nli_disabled_the_scan_does_not_run_and_says_so() -> None:
    out = run()
    meta = out["verification_meta"]
    assert meta["contradiction_scan"] == "not_run"
    assert meta["contradiction_scan_size"] == meta["contradiction_pairs_not_scanned"] == 0
    assert out["source_conflicts"] == []
    assert all("conflicting_sources" not in r.review_flags for r in results(out))
    report = out["report"]
    assert_message_rules(report)
    assert report["source_conflicts"] == [] and report["outcome"] == "DEGRADED"
    assert CONFLICT_SCAN_OFF_NOTE in report["user_message"]
    assert VERIFICATION_OFF_NOTE in report["user_message"]
    for phrase in NO_SCAN_PHRASES:
        assert phrase not in report["user_message"].lower()
    assert not any(u.startswith("conflicting_sources") for u in report["unresolved_items"])
    # with an empty chunk lookup only the citation checks fail: no schema or M4 problem
    assert all(p.startswith("item_") for p in check_report(report, lambda ids: {}).problems)


def test_the_disabled_path_reads_no_document_relations_and_no_other_evidence() -> None:
    corpus = RelationsCorpus()
    run(corpus)
    assert corpus.relation_calls == []


# --- the scan, scripted scorer only ---------------------------------------------------------------


def test_a_scripted_conflict_is_listed_with_both_sides_and_flagged() -> None:
    out = run(nli=conflict_nli(), nli_thresholds=FAKE_T)
    meta = out["verification_meta"]
    assert meta["contradiction_scan"] == "bounded" and meta["contradiction_scan_size"] == 1
    [c] = [SourceConflict.model_validate(x) for x in out["source_conflicts"]]
    sides = {c.side_a.evidence_id: c.side_a, c.side_b.evidence_id: c.side_b}
    assert set(sides) == {cid(RBI_TRUST), cid(FAQ_TRUST)}
    assert sides[cid(RBI_TRUST)].cited and not sides[cid(FAQ_TRUST)].cited
    assert sides[cid(FAQ_TRUST)].source_status == SourceStatus.DATED
    assert (c.resolution, c.resolution_reason) == ("unresolved", "no_supersession_relation")
    assert c.superseding_document_key is None and c.max_contradiction > 0.99
    [r] = results(out)
    assert "conflicting_sources" in r.review_flags
    report = out["report"]
    ComplianceReport.model_validate(report)
    assert report["source_conflicts"] == out["source_conflicts"]
    assert f"conflicting_sources:{r.requirement_id}" in report["unresolved_items"]
    assert "1 possible conflict(s) between sources" in report["user_message"]
    assert "does not prove that none exists" in report["user_message"]


def test_a_conflict_never_changes_applicability() -> None:
    with_conflict = results(run(nli=conflict_nli(), nli_thresholds=FAKE_T))[0]
    without = results(run(nli=FakeNLI(default=ENTAIL), nli_thresholds=FAKE_T))[0]
    assert with_conflict.applicability_after == without.applicability_after
    assert with_conflict.applicability_before == without.applicability_before
    assert with_conflict.requirement_verdict == without.requirement_verdict
    assert set(with_conflict.review_flags) - set(without.review_flags) == {"conflicting_sources"}


def test_no_conflict_found_is_reported_as_bounded_not_as_proof() -> None:
    report = run(nli=FakeNLI(default=ENTAIL), nli_thresholds=FAKE_T)["report"]
    assert report["source_conflicts"] == []
    assert "compared 1 pair(s)" in report["user_message"]
    assert "does not prove that none exists" in report["user_message"]
    assert CONFLICT_SCAN_OFF_NOTE not in report["user_message"]


def test_the_scan_is_deterministic_for_identical_inputs() -> None:
    first = run(nli=conflict_nli(), nli_thresholds=FAKE_T)
    second = run(nli=conflict_nli(), nli_thresholds=FAKE_T)
    assert first["source_conflicts"] == second["source_conflicts"]
    assert first["report"]["source_conflicts"] == second["report"]["source_conflicts"]


def test_a_failed_relation_lookup_never_resolves_and_stores_no_error_text() -> None:
    out = run(RelationsCorpus(fail=True), nli=conflict_nli(), nli_thresholds=FAKE_T)
    [c] = out["source_conflicts"]
    assert (c["resolution"], c["resolution_reason"]) == ("unresolved", "relation_lookup_failed")
    assert "secret-detail" not in str(out) and "ConnectionError" not in str(out["report"])


def test_an_incomplete_relation_lookup_never_resolves() -> None:
    corpus = RelationsCorpus(relations={"rbi_directions": row("rbi_directions")})
    [c] = run(corpus, nli=conflict_nli(), nli_thresholds=FAKE_T)["source_conflicts"]
    assert (c["resolution"], c["resolution_reason"]) == ("unresolved", "relation_incomplete")


def test_a_confirmed_active_supersession_resolves_the_conflict_and_keeps_it_visible() -> None:
    corpus = RelationsCorpus(relations={
        "rbi_directions": row("rbi_directions", ("rbi_faq",), "active", "confirmed"),
        "rbi_faq": row("rbi_faq", (), "superseded"),
    })  # fmt: skip
    out = run(corpus, nli=conflict_nli(), nli_thresholds=FAKE_T)
    [c] = out["source_conflicts"]
    assert (c["resolution"], c["superseding_document_key"]) == ("superseded", "rbi_directions")
    assert "0 could not be resolved" in out["report"]["user_message"]
    assert "conflicting_sources" in results(out)[0].review_flags  # still flagged for review


def test_a_scorer_failure_fails_the_scan_closed() -> None:
    class Flaky(FakeNLI):
        def score(self, pairs: Any) -> Any:
            if any("need not" in p.premise for p in pairs):  # only the conflict scan batch
                self.fail = True
            try:
                return super().score(pairs)
            finally:
                self.fail = False

    out = run(nli=Flaky(rules=[negation_disagrees], default=ENTAIL), nli_thresholds=FAKE_T)
    meta = out["verification_meta"]
    assert meta["contradiction_scan"] == "failed" and meta["contradiction_scan_size"] == 0
    assert meta["contradiction_pairs_not_scanned"] == 1 and out["source_conflicts"] == []
    assert {"component": "verification", "reason": "conflict_scan_failed"} in out["degradations"]
    assert CONFLICT_SCAN_FAILED_NOTE in out["report"]["user_message"]
    assert out["report"]["outcome"] == "DEGRADED"


def test_the_scan_uses_only_evidence_already_retrieved() -> None:
    from app.llm.fake import scripted_llm

    def retrieval_calls(**deps_kw: Any) -> tuple[int, set[str], set[str]]:
        retriever = FakeRetriever({"TRUST": [RBI_TRUST, FAQ_TRUST]})
        d = GraphDeps(retriever=retriever, corpus=RelationsCorpus(),
                      llm=scripted_llm(TRUST_INTAKE, draft(trust_req())), **deps_kw)  # fmt: skip
        g = build_graph(d, checkpointer=InMemorySaver())
        out = asyncio.run(g.ainvoke(start_input(TRUST_Q, session_id="s", run_id="r"),
                                    run_config(d, thread_id="t")))  # fmt: skip
        retrieved = {e["evidence_id"] for e in out["evidence"]}
        compared = {s["evidence_id"] for c in out.get("source_conflicts") or []
                    for s in (c["side_a"], c["side_b"])}  # fmt: skip
        return len(retriever.calls), retrieved, compared

    off_calls, _, _ = retrieval_calls()
    on_calls, retrieved, compared = retrieval_calls(nli=conflict_nli(), nli_thresholds=FAKE_T)
    assert on_calls == off_calls  # the scan retrieves nothing
    assert compared and compared <= retrieved  # it compares only what the run retrieved


# --- candidate pairs, cap and ordering (unit) ---------------------------------------------


def prem(eid: str, doc: str, text: str, *, role_doc: DocumentType = DocumentType.REGULATORY,
         status: SourceStatus = SourceStatus.ACTIVE) -> Premise:  # fmt: skip
    from app.graph.state import evidence_role

    eligibility = EvidenceEligibility.PRIMARY
    return Premise(
        evidence_id=eid, window_chunk_id=None, document_key=doc, document_type=role_doc,
        page=1, page_end=None, section=None,
        evidence_role=evidence_role(role_doc, eligibility), evidence_eligibility=eligibility,
        match_scope="entity", text_reliability=TextReliability.NATIVE, source_status=status,
        can_support_mandatory=True, windows=(Window(0, len(text), text),), windows_capped=False,
    )  # fmt: skip


DEED = "trust deed"


def pool(n_docs: int) -> list[Premise]:
    return [prem(f"e{i}", f"doc{i}", f"Clause {i}: the trust deed is needed.")
            for i in range(n_docs)]  # fmt: skip


def test_pairs_are_cross_document_name_the_item_and_involve_a_citation() -> None:
    same_doc = [prem("a", "d1", "the trust deed is needed"),
                prem("b", "d1", "the trust deed is not needed")]  # fmt: skip
    assert candidate_pairs([ScanItem("R1", DEED, frozenset({"a"}))], same_doc) == []
    unnamed = [prem("a", "d1", "the trust deed is needed"), prem("b", "d2", "PAN of the firm")]
    assert candidate_pairs([ScanItem("R1", DEED, frozenset({"a"}))], unnamed) == []
    uncited = pool(3)
    pairs = candidate_pairs([ScanItem("R1", DEED, frozenset({"e0"}))], uncited)
    assert [(c.a.premise.evidence_id, c.b.premise.evidence_id) for c in pairs] == [
        ("e0", "e1"),
        ("e0", "e2"),
    ]  # e1-e2 has no cited side
    assert candidate_pairs([ScanItem("R1", "", frozenset({"e0"}))], uncited) == []


def test_a_pair_shared_by_two_requirements_is_scored_once() -> None:
    items = [ScanItem("R1", DEED, frozenset({"e0"})), ScanItem("R2", DEED, frozenset({"e1"}))]
    pairs = candidate_pairs(items, pool(2))
    assert len(pairs) == 1 and [r.requirement_id for r in pairs[0].requirements] == ["R1", "R2"]
    fake = FakeNLI(default=CONTRA)
    result = scan(pairs, fake, FAKE_T, max_pairs=10)
    assert result.pairs_scored == 1 and len(fake.calls[0]) == 2
    conflicts = source_conflicts(result, {})
    assert [c.requirement_id for c in conflicts] == ["R1", "R2"]


def test_the_order_is_deterministic_whatever_the_input_order() -> None:
    items = [ScanItem("R1", DEED, frozenset({"e0", "e3"}))]
    expected = candidate_pairs(items, pool(5))
    for seed in range(5):
        shuffled = pool(5)
        random.Random(seed).shuffle(shuffled)  # noqa: S311 (test order only)
        got = candidate_pairs(items, shuffled)
        assert [(c.a.key, c.b.key) for c in got] == [(c.a.key, c.b.key) for c in expected]


@pytest.mark.parametrize(("cap", "scored", "skipped"), [(0, 0, 7), (3, 3, 4), (7, 7, 0),
                                                         (50, 7, 0)])  # fmt: skip
def test_the_cap_counts_unique_pairs_and_the_rest_as_not_scanned(
    cap: int, scored: int, skipped: int
) -> None:
    pairs = candidate_pairs([ScanItem("R1", DEED, frozenset({"e0"}))], pool(8))
    assert len(pairs) == 7
    fake = FakeNLI(default=ENTAIL)
    result = scan(pairs, fake, FAKE_T, max_pairs=cap)
    assert (result.pairs_scored, result.pairs_not_scanned) == (scored, skipped)
    assert sum(len(c) for c in fake.calls) == 2 * scored  # both directions per unique pair
    if scored:
        sent = {(p.premise, p.hypothesis) for p in fake.calls[0]}
        for c in pairs[:scored]:
            assert {(c.a.window.text, c.b.window.text), (c.b.window.text, c.a.window.text)} <= sent


def test_a_contradiction_in_either_direction_is_a_conflict() -> None:
    pairs = candidate_pairs([ScanItem("R1", DEED, frozenset({"e0"}))], pool(2))
    backward_only = FakeNLI(
        rules=[lambda p: CONTRA if p.premise.startswith("Clause 1") else None], default=ENTAIL
    )
    assert len(scan(pairs, backward_only, FAKE_T, 10).conflicts) == 1
    below = FakeNLI(default=(0.0, 1.0, 1.5))  # contradiction prob < 0.9
    assert scan(pairs, below, FAKE_T, 10).conflicts == ()


def test_an_unscoreable_pair_is_not_scanned_never_truncated() -> None:
    long_text = "Clause 9: the trust deed is needed. " + "word " * 50
    premises = [prem("e0", "d0", "Clause 0: the trust deed is needed."),
                prem("e9", "d9", long_text)]  # fmt: skip
    pairs = candidate_pairs([ScanItem("R1", DEED, frozenset({"e0"}))], premises)
    result = scan(pairs, FakeNLI(default=CONTRA, too_long="word word"), FAKE_T, 10)
    assert (result.pairs_scored, result.pairs_not_scanned, result.conflicts) == (0, 1, ())


def test_scanning_rejects_a_negative_cap() -> None:
    with pytest.raises(ValueError):
        scan([], FakeNLI(), FAKE_T, -1)
    with pytest.raises(ValueError, match="max_conflict_pairs"):
        GraphDeps(max_conflict_pairs=-1)


def test_settings_drive_the_cap_and_never_enable_nli() -> None:
    deps = GraphDeps.from_settings(Settings(MAX_CONFLICT_PAIRS=17))
    assert deps.max_conflict_pairs == 17 and deps.nli is None


# --- supersession (D10) --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("recorded", "active", "confirmed"), list(itertools.product([True, False], repeat=3))
)
def test_only_all_three_conditions_resolve(recorded: bool, active: bool, confirmed: bool) -> None:
    relations = {
        "new": row("new", ("old",) if recorded else (), "active" if active else "dated",
                   "confirmed" if confirmed else "needs_review"),
        "old": row("old", (), "superseded"),
    }  # fmt: skip
    resolution, reason, key = resolve("new", "old", relations)
    assert (resolution == "superseded") == (recorded and active and confirmed)
    assert resolve("old", "new", relations)[0] == resolution  # side order never matters
    expected = (
        "no_supersession_relation" if not recorded
        else "superseding_not_active" if not active
        else "version_review_not_confirmed" if not confirmed
        else "supersession_confirmed"
    )  # fmt: skip
    assert reason == expected and (key == "new") == (resolution == "superseded")


def test_failed_incomplete_or_mutual_relations_never_resolve() -> None:
    assert resolve("a", "b", None) == ("unresolved", "relation_lookup_failed", None)
    assert resolve("a", "b", {"a": row("a")}) == ("unresolved", "relation_incomplete", None)
    mutual = {"a": row("a", ("b",), "active", "confirmed"),
              "b": row("b", ("a",), "active", "confirmed")}  # fmt: skip
    assert resolve("a", "b", mutual) == ("unresolved", "no_supersession_relation", None)


def test_a_newer_document_without_a_recorded_relation_resolves_nothing() -> None:
    """Dates are never consulted: the relation rows carry none, and an active newer document
    with a confirmed review but no recorded relation leaves the conflict unresolved."""
    assert "issue_date" not in DocumentRelationRow.__dataclass_fields__
    relations = {"directions_2025": row("directions_2025", (), "active", "confirmed"),
                 "faq_2016": row("faq_2016", (), "dated")}  # fmt: skip
    assert resolve("directions_2025", "faq_2016", relations)[0] == "unresolved"


# --- the record is structured, and reports stay compatible ----------------------------------------


def side(eid: str, doc: str, cited: bool = True) -> dict[str, Any]:
    return {"evidence_id": eid, "document_key": doc, "page": 1, "window_start": 0,
            "window_end": 10, "source_status": "active", "evidence_eligibility": "primary",
            "cited": cited}  # fmt: skip


def conflict(**kw: Any) -> dict[str, Any]:
    return {"requirement_id": "R1", "side_a": side("a", "d1"), "side_b": side("b", "d2", False),
            "max_contradiction": 0.97, "resolution": "unresolved",
            "resolution_reason": "no_supersession_relation"} | kw  # fmt: skip


@pytest.mark.parametrize(
    "bad",
    [
        {"side_b": side("b", "d1")},  # same document
        {"side_a": side("a", "d1", False)},  # neither side cited
        {"resolution": "superseded"},  # without the confirmed reason and key
        {
            "resolution": "superseded",
            "resolution_reason": "supersession_confirmed",
            "superseding_document_key": "elsewhere",
        },  # not one of its documents
        {"superseding_document_key": "d1"},  # unresolved with a key
        {"resolution_reason": "ConnectionError: db down"},  # free text is not a code
        {"note": "anything"},  # no extra fields
        {"max_contradiction": 1.5},
    ],
)
def test_a_source_conflict_is_structured(bad: dict[str, Any]) -> None:
    SourceConflict.model_validate(conflict())
    with pytest.raises(ValidationError):
        SourceConflict.model_validate(conflict(**bad))


def test_a_source_conflict_holds_no_text_field() -> None:
    assert set(SourceConflict.model_fields) == {
        "requirement_id", "side_a", "side_b", "detected_by", "max_contradiction",
        "resolution", "resolution_reason", "superseding_document_key",
    }  # fmt: skip
    assert "text" not in ConflictSide.model_fields


def test_report_rules_for_conflicts() -> None:
    report = run(nli=conflict_nli(), nli_thresholds=FAKE_T)["report"]
    # conflicts with a scan that did not run
    bad = copy.deepcopy(report)
    bad["verification_meta"] |= {"contradiction_scan": "not_run", "contradiction_scan_size": 0,
                                 "contradiction_pairs_not_scanned": 0}  # fmt: skip
    with pytest.raises(ValidationError, match="scan that ran"):
        ComplianceReport.model_validate(bad)
    # a conflict whose requirement is not flagged
    bad = copy.deepcopy(report)
    bad["claim_verification"][0]["review_flags"].remove("conflicting_sources")
    with pytest.raises(ValidationError, match="conflicting_sources"):
        ComplianceReport.model_validate(bad)
    # a v1 report can carry none
    bad = copy.deepcopy(report)
    v1 = {k: v for k, v in bad.items() if k not in ("claim_verification", "verification_meta",
                                                     "unverified_items")}  # fmt: skip
    with pytest.raises(ValidationError):
        ComplianceReport.model_validate(v1 | {"report_version": 1})


def test_not_configured_acceptance_refuses_any_conflict_trace() -> None:
    report = run()["report"]
    found = check_report(report, lambda ids: {}).problems
    assert not [p for p in found if not p.startswith("item_")]
    flagged = copy.deepcopy(report)
    flagged["claim_verification"][0]["review_flags"].append("conflicting_sources")
    assert "conflict_flag_without_a_model" in check_report(flagged, lambda ids: {}).problems
    scanned = copy.deepcopy(report)
    scanned["verification_meta"]["contradiction_scan"] = "bounded"
    assert check_report(scanned, lambda ids: {}).problems[0] == "report_schema_invalid"


def test_a_candidate_is_plain_data() -> None:
    [c] = candidate_pairs([ScanItem("R1", DEED, frozenset({"e0"}))], pool(2))
    assert isinstance(c, Candidate) and c.a.key < c.b.key
