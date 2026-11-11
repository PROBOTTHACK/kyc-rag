"""M3 S2: `KYCState` is JSON-safe, round-trips losslessly and validates its nested models."""

import asyncio
import json
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from app.graph.builder import GraphDeps, build_graph, run_config
from app.graph.state import (
    STATE_KEYS,
    ClarificationAnswer,
    Degradation,
    EntityProfile,
    EvidenceItem,
    FieldEvidenceLink,
    FormFieldRef,
    FormRef,
    KYCState,
    LinkKind,
    LLMCallRecord,
    ProfileValue,
    RetrievalFilters,
    RetrievalMeta,
    ScreenRecord,
    ValidationFinding,
    dump,
    dump_list,
    read,
    read_list,
    validate_state,
)

SHA = "a" * 64


def evidence(**kw: Any) -> EvidenceItem:
    base: dict[str, Any] = {
        "evidence_id": "c-1",
        "document_id": "d-1",
        "document_key": "rbi_kyc_directions_2025",
        "bank": "RBI",
        "page": 38,
        "paragraph": "35",
        "section": "C. CDD Measures for Legal Entities",
        "source_status": "active",
        "evidence_eligibility": "primary",
        "text_reliability": "native",
        "can_support_mandatory": True,
        "rank": 1,
        "score_kind": "dense_cosine",
        "text": "35. For opening an account of a trust ...",
    }
    return EvidenceItem(**(base | kw))


def full_state() -> KYCState:
    """Every key populated, nested values as their JSON dumps."""
    profile = EntityProfile(
        entity_type=ProfileValue(value="TRUST", source="user_stated", evidence_span="trust"),
        residency=ProfileValue(value="RESIDENT", source="user_answered"),
    )
    form = FormRef(
        form_key="hdfc_non_individual_account_opening",
        method="gemini_multimodal",
        version=2,
        row_id="r-1",
        schema_sha256=SHA,
        fields=[
            FormFieldRef(
                printed_label="*Name of Trust",
                label="Name of Trust",
                page_number=1,
                chunk_id="c-9",
                required_as_shown=True,
                verification_status="corroborated",
            )
        ],
    )
    return {
        "session_id": "s-1",
        "run_id": "run-1",
        "data_class": "synthetic_demo",
        "query": "What does a synthetic trust need? [REDACTED_EMAIL]",
        "clarification_rounds": 1,
        "clarification_answers": dump_list(
            [ClarificationAnswer(field="residency", value="RESIDENT", round=1)]
        ),
        "screening": dump_list(
            [
                ScreenRecord(
                    stage="messages", action="redact", categories=["email"], counts={"email": 1}
                )
            ]
        ),
        "security_status": "safe",
        "security_reasons": [],
        "entity_profile": dump(profile),
        "missing_fields": [],
        "ambiguities": ["account_type not stated"],
        "intake_complete": True,
        "retrieval_query": "trust account documents",
        "filters": dump(RetrievalFilters(entity_terms=["TRUST"])),
        "evidence": dump_list([evidence()]),
        "retrieval_meta": dump(
            RetrievalMeta(mode="dense", timings_ms={"dense_ms": 41.5}, score_kind="dense_cosine")
        ),
        "form_analysis_required": True,
        "forms": dump_list([form]),
        "form_flags": ["ocr_uncertain_fields"],
        "associations": dump_list(
            [
                FieldEvidenceLink(
                    form_key=form.form_key,
                    field_label="Name of Trust",
                    field_chunk_id="c-9",
                    evidence_id="c-1",
                    link_kind=LinkKind.SAME_SECTION,
                )
            ]
        ),
        "draft": {"requirements": [{"name": "trust deed", "evidence_ids": ["c-1"]}]},
        "synthesis_attempts": 1,
        "llm_calls": dump_list(
            [
                LLMCallRecord(
                    node="compliance_synthesis",
                    provider="fake",
                    model_id="fake-llm-1",
                    latency_ms=0,
                    attempts=1,
                    result="ok",
                )
            ]
        ),
        "degradations": dump_list([Degradation(component="rerank", reason="OSError")]),
        "validation_findings": dump_list(
            [ValidationFinding(kind="downgraded", requirement_id="REQ_1", detail="form-only")]
        ),
        "outcome": "DEGRADED",
        "report_id": None,
        "checklist": None,
        "report": None,
    }


def test_the_full_state_covers_every_key_and_validates() -> None:
    state = full_state()
    assert set(state) == STATE_KEYS
    validate_state(state)


def test_json_round_trip_is_lossless() -> None:
    state = full_state()
    text = json.dumps(state, sort_keys=True)
    back = json.loads(text)
    assert back == state
    validate_state(back)
    assert json.dumps(back, sort_keys=True) == text


def test_nested_values_read_back_into_their_models() -> None:
    state = full_state()
    profile = read(state, "entity_profile", EntityProfile)
    assert profile is not None and profile.entity_type.value == "TRUST"
    assert read_list(state, "evidence", EvidenceItem) == [evidence()]
    assert read({}, "entity_profile", EntityProfile) is None
    assert read_list({}, "evidence", EvidenceItem) == []


# Keys the S3/S4 nodes recompute on every run (security, intake, retrieval, forms,
# association) and the outcome.
RECOMPUTED = {
    "screening",
    "security_status",
    "security_reasons",
    "entity_profile",
    "missing_fields",
    "ambiguities",
    "intake_complete",
    "outcome",
    "retrieval_query",
    "filters",
    "evidence",
    "retrieval_meta",
    "form_analysis_required",
    "forms",
    "form_flags",
    "associations",
    "degradations",
    "draft",
    "synthesis_attempts",
    "llm_calls",
    "validation_findings",
    "checklist",
    "report",
    "report_id",
}


def test_state_survives_a_checkpoint_round_trip() -> None:
    from tests.unit.test_graph_s4 import RBI_TRUST, FakeCorpus, FakeRetriever

    deps = GraphDeps(retriever=FakeRetriever({"TRUST": [RBI_TRUST]}), corpus=FakeCorpus())
    graph = build_graph(deps, checkpointer=InMemorySaver())
    config = run_config(deps, thread_id="s-1")
    state = full_state() | {
        "entity_profile": dump(
            EntityProfile(
                entity_type=ProfileValue(
                    value="TRUST", source="user_stated", evidence_span="trust"
                ),
                account_type=ProfileValue(value="CURRENT_ACCOUNT", source="user_answered"),
            )
        )
    }

    # The graph input accepts only start-of-run keys (S3.1 input_schema), so the full
    # state is written as if `clarify` had just finished; the run then continues through
    # input_security, intake (profile present: no LLM call), retrieval ... finalize.
    async def run() -> Any:
        await graph.aupdate_state(config, state, as_node="clarify")
        return await graph.ainvoke(None, config)

    final = asyncio.run(run())
    saved = graph.get_state(config).values
    assert saved == final
    assert {k: v for k, v in saved.items() if k not in RECOMPUTED} == {
        k: v for k, v in state.items() if k not in RECOMPUTED
    }
    assert saved["security_status"] == "safe" and saved["intake_complete"] is True
    assert saved["outcome"] == "DEGRADED"  # the fixture carries a degradation
    validate_state(saved)


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"surprise": 1}, "unknown state keys"),
        ({"clarification_rounds": "1"}, "clarification_rounds"),
        ({"intake_complete": 1}, "intake_complete"),
        ({"security_status": "approved"}, "security_status"),
        ({"outcome": "APPROVED"}, "outcome"),
        ({"evidence": [{"evidence_id": "x"}]}, "evidence"),
        ({"evidence": [dump(evidence()) | {"can_support_mandatory": False}]}, "evidence"),
        (
            {"entity_profile": {"entity_type": {"value": "LLP", "source": "unknown"}}},
            "entity_profile",
        ),
        (
            {
                "associations": [
                    dump(
                        FieldEvidenceLink(
                            form_key="f",
                            field_label="l",
                            evidence_id="c",
                            link_kind=LinkKind.TOPIC_MATCH,
                        )
                    )
                    | {"relationship_only": False}
                ]
            },
            "associations",
        ),
        ({"draft": {"when": {1, 2}}}, "not JSON-serialisable"),
        ({"draft": {"n": float("nan")}}, "round-trippable"),
    ],
)
def test_malformed_states_are_rejected(patch: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_state(full_state() | patch)  # type: ignore[operator]


def test_profile_values_are_never_inferred() -> None:
    with pytest.raises(ValidationError):
        ProfileValue(value="LLP")  # a value needs a user source
    with pytest.raises(ValidationError):
        ProfileValue(source="user_stated")  # a source needs a value
    with pytest.raises(ValidationError):
        EntityProfile(entity_type=ProfileValue(value="NRI", source="user_stated"))  # residency
    assert EntityProfile().entity_type.value is None


@pytest.mark.parametrize(
    ("eligibility", "reliability", "can_support"),
    [
        ("primary", "native", True),
        ("primary", "ocr_reliable", True),
        ("primary", "ocr_uncertain", False),
        ("supporting", "native", False),
        ("context_only", "native", False),
    ],
)
def test_evidence_mirrors_the_mandatory_capability_rule(
    eligibility: str, reliability: str, can_support: bool
) -> None:
    item = evidence(
        evidence_eligibility=eligibility,
        text_reliability=reliability,
        can_support_mandatory=can_support,
    )
    assert item.can_support_mandatory is can_support
    with pytest.raises(ValidationError):
        evidence(
            evidence_eligibility=eligibility,
            text_reliability=reliability,
            can_support_mandatory=not can_support,
        )


def test_form_refs_carry_no_policy_obligation_field() -> None:
    schema = FormRef.model_json_schema()
    properties = set(schema["properties"])
    for definition in schema.get("$defs", {}).values():
        properties |= set(definition.get("properties", {}))
    assert "mandatory_under_policy" not in properties
    assert "mandatory_under_policy" not in FormFieldRef.model_fields
