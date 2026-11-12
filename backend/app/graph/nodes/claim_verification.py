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

M4 S6, the bounded source-conflict scan (`app.verification.conflicts`): only with a scorer.
It compares passages of evidence ALREADY retrieved in this run (eligible evidence only, no
new retrieval), at most `max_conflict_pairs` unique pairs. A conflict adds the
`conflicting_sources` flag and is listed with both sides; applicability never changes.
Without a scorer the scan is `not_run` (zero pairs, no conflicts, no flags): never reported
as "no conflict found".

No LLM call, no network call, no model is loaded here. State holds codes, ids, offsets,
scores and the model prose already in the report; never user words.
"""

import time
from typing import TYPE_CHECKING, Any

from app.db.repository import DocumentRelationRow, EvidenceContext
from app.graph.routing import ELIGIBLE
from app.graph.state import (
    Citation,
    Degradation,
    EntityProfile,
    EvidenceItem,
    KYCState,
    RequirementVerification,
    SourceConflict,
    ValidatedRequirement,
    VerificationMeta,
    dump,
    dump_list,
    evidence_role,
    read,
    read_list,
)
from app.schemas.taxonomy import EntityType
from app.verification.claims import TEMPLATES_VERSION, extract
from app.verification.conflicts import ScanItem, candidate_pairs, scan, source_conflicts
from app.verification.decide import Thresholds, not_verified_requirement, verify_requirement
from app.verification.nli import NLIScorer
from app.verification.premises import Premise, PremiseMismatch, build_premise

if TYPE_CHECKING:
    from app.graph.builder import GraphDeps

NLI_NOT_CONFIGURED = "nli_not_configured"
PREMISE_UNAVAILABLE = "premise_unavailable"
CORPUS_UNAVAILABLE = "corpus_unavailable"
CONFLICT_SCAN_FAILED = "conflict_scan_failed"


def _entity(state: KYCState) -> EntityType | None:
    profile = read(state, "entity_profile", EntityProfile)
    value = profile.entity_type.value if profile is not None else None
    return EntityType(value) if value is not None else None


def _contexts(ids: list[str], deps: "GraphDeps") -> tuple[dict[str, EvidenceContext], str | None]:
    """The stored chunks behind the given ids, or a failure code (fail closed)."""
    if deps.corpus is None:
        return {}, CORPUS_UNAVAILABLE
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


def _retrieved_premises(
    state: KYCState, contexts: dict[str, EvidenceContext], known: dict[str, Premise]
) -> dict[str, Premise]:
    """Premises for eligible evidence already retrieved in this run (never re-retrieved).
    A chunk whose stored facts disagree with state is left out (not scanned)."""
    out = dict(known)
    for e in read_list(state, "evidence", EvidenceItem):
        if e.evidence_id in out or e.evidence_eligibility.value not in ELIGIBLE:
            continue
        ctx = contexts.get(e.evidence_id)
        if ctx is None:
            continue
        citation = Citation(
            evidence_id=e.evidence_id, document_key=e.document_key, page=e.page,
            page_end=e.page_end, paragraph=e.paragraph, match_scope=e.match_scope,
            evidence_eligibility=e.evidence_eligibility, text_reliability=e.text_reliability,
            source_status=e.source_status, can_support_mandatory=e.can_support_mandatory,
        )  # fmt: skip
        try:
            out[e.evidence_id] = build_premise(citation, ctx)
        except PremiseMismatch:
            continue
    return out


def _relations(keys: list[str], deps: "GraphDeps") -> dict[str, DocumentRelationRow] | None:
    """Supersession facts; None on any lookup failure (then nothing is ever resolved)."""
    if deps.corpus is None:
        return None
    try:
        return deps.corpus.document_relations(keys)
    except Exception:  # a failed lookup never resolves a conflict
        return None


def _conflict_scan(
    state: KYCState,
    deps: "GraphDeps",
    checklist: list[ValidatedRequirement],
    premises: dict[str, Premise],
    contexts: dict[str, EvidenceContext],
    scorer: NLIScorer,
    thresholds: Thresholds,
) -> tuple[list[SourceConflict], dict[str, Any]]:
    pool = _retrieved_premises(state, contexts, premises)
    items = [
        ScanItem(r.requirement_id, r.name, frozenset(c.evidence_id for c in r.citations))
        for r in checklist
    ]
    result = scan(candidate_pairs(items, list(pool.values())), scorer, thresholds,
                  deps.max_conflict_pairs)  # fmt: skip
    keys = sorted({p.premise.document_key for c, _ in result.conflicts for p in (c.a, c.b)})
    relations = _relations(keys, deps) if keys else {}
    meta = {
        "contradiction_scan": "failed" if result.failed else "bounded",
        "contradiction_scan_size": result.pairs_scored,
        "contradiction_pairs_not_scanned": result.pairs_not_scanned,
    }
    return source_conflicts(result, relations), meta


def _flag_conflicts(
    results: list[RequirementVerification], conflicts: list[SourceConflict]
) -> list[RequirementVerification]:
    """Add the review flag only; applicability and verdicts are untouched (S6-3)."""
    flagged = {c.requirement_id for c in conflicts}
    out = []
    for r in results:
        if r.requirement_id in flagged and "conflicting_sources" not in r.review_flags:
            r = RequirementVerification.model_validate(
                r.model_dump() | {"review_flags": [*r.review_flags, "conflicting_sources"]}
            )
        out.append(r)
    return out


def claim_verification(state: KYCState, deps: "GraphDeps") -> dict[str, Any]:
    raw = state.get("checklist")
    if not raw:  # routing sends only a non-empty checklist here
        return {}
    started = time.perf_counter()
    checklist = [ValidatedRequirement.model_validate(c) for c in raw]
    entity = _entity(state)
    # Both or neither (GraphDeps enforces it); None = NLI disabled, the S5 default.
    config = (
        (deps.nli, deps.nli_thresholds)
        if deps.nli is not None and deps.nli_thresholds is not None
        else None
    )
    ids = {c.evidence_id for r in checklist for c in r.citations}
    if config is not None:  # the conflict scan also reads the other retrieved evidence
        ids |= {e.evidence_id for e in read_list(state, "evidence", EvidenceItem)}
    contexts, corpus_error = _contexts(sorted(ids), deps)

    results: list[RequirementVerification] = []
    premises_by_id: dict[str, Premise] = {}
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
            premises_by_id.update({p.evidence_id: p for p in premises})
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

    conflicts: list[SourceConflict] = []
    scan_meta: dict[str, Any] = {}  # not_run (the VerificationMeta default) without a scorer
    if config is not None and corpus_error is None:
        conflicts, scan_meta = _conflict_scan(
            state, deps, checklist, premises_by_id, contexts, config[0], config[1]
        )
        results = _flag_conflicts(results, conflicts)
    elif config is not None:
        scan_meta = {"contradiction_scan": "failed"}

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
            **scan_meta,
        )
        reason = (
            corpus_error if corpus_error is not None
            else "verification_partial" if status == "partial"
            else CONFLICT_SCAN_FAILED if meta.contradiction_scan == "failed"
            else None
        )  # fmt: skip
    update: dict[str, Any] = {
        "claim_results": dump_list(results),
        "verification_meta": dump(meta),
        "unverified_items": [],  # the report derives them from the results (decision D2)
        "source_conflicts": dump_list(conflicts),
    }
    if reason is not None:
        update["degradations"] = [
            *(state.get("degradations") or []),
            dump(Degradation(component="verification", reason=reason)),
        ]
    return update
