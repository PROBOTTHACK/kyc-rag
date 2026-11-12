"""`claim_verification` node (M4 S5; M4_PLAN §C, §G): after `output_validation`.

Checks each validated requirement's typed claims against the text of its stored citations
and only ever LOWERS applicability (the never-upgrade invariant is re-checked by
`RequirementVerification`). The M3 checklist in state is left untouched: the report shows
the verified view next to the M3 applicability (`applicability_before`).

NLI is disabled by default (S4 outcome `m4-s4-v1`: no configuration qualified). Without a
scorer and thresholds in `GraphDeps`:
- every claim is `not_verified` (`nli_not_configured`); nothing is scored and no rule-based
  reading is applied, so a missing check never becomes a contradiction, an absence of
  evidence or a finding that a claim is false;
- applicability is capped at CONDITIONAL (MANDATORY needs a supported claim) and each item
  is flagged for review;
- a `verification` degradation makes the outcome DEGRADED, with the checklist kept.

With a scorer (tests only in S5, `FakeNLI`): premises are built from the stored chunks
(Postgres), and `verify_requirement` applies the S2 decision table. A requirement whose
premises cannot be built is `not_verified`; a corpus failure fails the stage closed.

No LLM call, no network call, no model is loaded here. State holds codes, ids, offsets,
scores and the model prose already in the report; never user words.
"""

import time
from typing import TYPE_CHECKING, Any

from app.db.repository import EvidenceContext
from app.graph.state import (
    Degradation,
    EntityProfile,
    KYCState,
    RequirementVerification,
    ValidatedRequirement,
    VerificationMeta,
    dump,
    dump_list,
    evidence_role,
    read,
)
from app.schemas.taxonomy import EntityType
from app.verification.claims import TEMPLATES_VERSION, extract
from app.verification.decide import not_verified_requirement, verify_requirement
from app.verification.premises import PremiseMismatch, build_premise

if TYPE_CHECKING:
    from app.graph.builder import GraphDeps

NLI_NOT_CONFIGURED = "nli_not_configured"
PREMISE_UNAVAILABLE = "premise_unavailable"
CORPUS_UNAVAILABLE = "corpus_unavailable"


def _entity(state: KYCState) -> EntityType | None:
    profile = read(state, "entity_profile", EntityProfile)
    value = profile.entity_type.value if profile is not None else None
    return EntityType(value) if value is not None else None


def _contexts(
    checklist: list[ValidatedRequirement], deps: "GraphDeps"
) -> tuple[dict[str, EvidenceContext], str | None]:
    """The stored chunks behind every citation, or a failure code (fail closed)."""
    if deps.corpus is None:
        return {}, CORPUS_UNAVAILABLE
    ids = sorted({c.evidence_id for r in checklist for c in r.citations})
    try:
        return deps.corpus.evidence(ids), None
    except Exception as exc:  # any reader failure: verify nothing, never guess
        return {}, f"{CORPUS_UNAVAILABLE}:{type(exc).__name__}"


def _roles(req: ValidatedRequirement, contexts: dict[str, EvidenceContext]) -> set[str]:
    roles = set()
    for c in req.citations:
        ctx = contexts.get(c.evidence_id)
        if ctx is not None:
            stored = ctx.retrieved.metadata
            roles.add(evidence_role(stored.document_type, stored.evidence_eligibility).value)
    return roles


def claim_verification(state: KYCState, deps: "GraphDeps") -> dict[str, Any]:
    raw = state.get("checklist")
    if not raw:  # routing sends only a non-empty checklist here
        return {}
    started = time.perf_counter()
    checklist = [ValidatedRequirement.model_validate(c) for c in raw]
    entity = _entity(state)
    contexts, corpus_error = _contexts(checklist, deps)
    # Both or neither (GraphDeps enforces it); None = NLI disabled, the S5 default.
    config = (
        (deps.nli, deps.nli_thresholds)
        if deps.nli is not None and deps.nli_thresholds is not None
        else None
    )

    results: list[RequirementVerification] = []
    scored = unscored = fallbacks = 0
    for req in checklist:
        claims = extract(req, entity=entity, roles=_roles(req, contexts)).claims
        if config is None:
            results.append(not_verified_requirement(req, claims, reason=NLI_NOT_CONFIGURED))
            continue
        if corpus_error is not None:
            results.append(not_verified_requirement(req, claims, reason=CORPUS_UNAVAILABLE))
            fallbacks += 1
            continue
        try:
            premises = [build_premise(c, contexts[c.evidence_id]) for c in req.citations]
        except (KeyError, PremiseMismatch):
            results.append(not_verified_requirement(req, claims, reason=PREMISE_UNAVAILABLE))
            fallbacks += 1
            continue
        verified = verify_requirement(
            req, claims, premises, scorer=config[0], thresholds=config[1], entity=entity,
            max_pairs=deps.nli_max_pairs,
        )  # fmt: skip
        results.append(verified.verification)
        scored += verified.pairs_scored
        unscored += verified.pairs_unscored

    timings = {"total_ms": round((time.perf_counter() - started) * 1000, 3)}
    if config is None:
        meta = VerificationMeta(
            status="not_configured", templates_version=TEMPLATES_VERSION, timings_ms=timings
        )
        reason: str | None = NLI_NOT_CONFIGURED
    else:
        scorer, thresholds = config
        status = (
            "failed"
            if corpus_error is not None
            else "partial" if unscored or fallbacks else "ran"
        )  # fmt: skip
        meta = VerificationMeta(
            status=status,
            model_id=scorer.model_id,
            model_revision=scorer.model_revision,
            label_map=dict(scorer.label_map),
            templates_version=TEMPLATES_VERSION,
            thresholds_version=thresholds.version,
            pairs_scored=scored,
            pairs_capped=unscored,
            timings_ms=timings,
        )
        reason = (
            None
            if status == "ran"
            else corpus_error if corpus_error is not None else "verification_partial"
        )  # fmt: skip
    update: dict[str, Any] = {
        "claim_results": dump_list(results),
        "verification_meta": dump(meta),
        "unverified_items": [],  # the report derives them from the results (decision D2)
    }
    if reason is not None:
        update["degradations"] = [
            *(state.get("degradations") or []),
            dump(Degradation(component="verification", reason=reason)),
        ]
    return update
