"""`policy_retrieval` node (M3 S4; M3_PLAN §3, "no evidence, no answer").

Real retrieval only, through the injected retriever (the M1/M2 `Retriever`; dense by
default) and the read-only `CorpusReader` (parent expansion from Postgres):

1. Entity pass: the screened question, filtered to chunks tagged with the customer's
   entity type (`SearchFilters.entity_terms`; context-only sources excluded as always).
2. Entity-group pass (decision S4-D1): only when the entity pass retrieved no
   primary-capable chunk, once more with the entity's narrowest taxonomy group
   (LEGAL_ENTITY, else NON_INDIVIDUAL; none for INDIVIDUAL). Those items are labelled
   `match_scope="entity_group"` and can never on their own establish MANDATORY (S5).
3. Every item is built from its stored chunk; a hit with no Postgres row is dropped and
   counted, never cited.

The result is one of four distinct statuses in `retrieval_meta.evidence_status`:
`found` (entity-scope primary-capable evidence RETRIEVED; relevance and correctness are
not verified here: scores are uncalibrated and S5 judges support), `partial` (with gap
codes), `none` (the search ran and found nothing eligible) and `retrieval_failed` (a
technical failure). Missing evidence is a gap, never "not required". No LLM is called.

Residency and account type are not retrieval filters: chunks are not tagged by account
type, and `SearchFilters` has no residency filter (residency is used for form selection,
individuals only).
"""

from typing import TYPE_CHECKING, Any

from app.graph.evidence import (
    GAP_RETRIEVAL_FAILED,
    TOP_K,
    Scope,
    candidate_forms,
    classify_evidence,
    evidence_item,
    group_for,
)
from app.graph.state import (
    Degradation,
    EntityProfile,
    EvidenceItem,
    KYCState,
    RetrievalFilters,
    RetrievalMeta,
    dump,
    dump_list,
    read,
)
from app.retrieval.retrieve import RetrievalError, RetrievalResult
from app.retrieval.search import SearchFilters, SearchHit
from app.schemas.taxonomy import EntityType

if TYPE_CHECKING:
    from app.graph.builder import GraphDeps


def _failed(state: KYCState, deps: "GraphDeps", kinds: list[str]) -> dict[str, Any]:
    meta = RetrievalMeta(
        mode=deps.retrieval_mode,
        failed=True,
        failure_kinds=kinds,
        evidence_status="retrieval_failed",
        gaps=[GAP_RETRIEVAL_FAILED],
    )
    degradation = Degradation(component="retrieval", reason=",".join(kinds)[:200])
    return {
        "evidence": [],
        "retrieval_meta": dump(meta),
        "form_analysis_required": False,
        "degradations": [*(state.get("degradations") or []), dump(degradation)],
    }


def _kinds(exc: RetrievalError) -> list[str]:
    """Exception class names only (`RetrieverFailure.error` is "ExcType: message")."""
    return [f"{f.retriever}:{f.error.split(':', 1)[0]}" for f in exc.failures] or ["RetrievalError"]


def policy_retrieval(state: KYCState, deps: "GraphDeps") -> dict[str, Any]:
    if deps.retriever is None or deps.corpus is None:
        return _failed(state, deps, ["not_configured"])
    profile = read(state, "entity_profile", EntityProfile)
    entity = profile.entity_type.value if profile else None
    if profile is None or entity is None:
        return _failed(state, deps, ["incomplete_intake"])  # routing never allows this
    query = state.get("query", "")

    retriever = deps.retriever

    def run(terms: str) -> RetrievalResult:
        return retriever.retrieve(
            query,
            mode=deps.retrieval_mode,
            top_k=TOP_K,
            filters=SearchFilters(entity_terms=(terms,)),
            rerank=deps.rerank,
        )

    passes: list[tuple[RetrievalResult, Scope]] = []
    try:
        passes.append((run(entity), "entity"))
        hits: list[tuple[SearchHit, Scope]] = [(h, "entity") for h in passes[0][0].hits]
        contexts = deps.corpus.evidence(h.chunk_id for h, _ in hits)
    except RetrievalError as exc:
        return _failed(state, deps, _kinds(exc))
    except (ValueError, LookupError) as exc:
        return _failed(state, deps, [type(exc).__name__])

    degradations: list[Degradation] = []
    entity_primary = any(c.retrieved.metadata.can_support_mandatory for c in contexts.values())
    group = group_for(EntityType(entity))
    if not entity_primary and group is not None:
        try:
            second = run(group.value)
            seen = {h.chunk_id for h, _ in hits}
            extra: list[tuple[SearchHit, Scope]] = [
                (h, "entity_group") for h in second.hits if h.chunk_id not in seen
            ]
            contexts |= deps.corpus.evidence(h.chunk_id for h, _ in extra)
            hits += extra
            passes.append((second, "entity_group"))
        except (RetrievalError, LookupError) as exc:
            # The entity pass stands; the missing group pass is recorded, never hidden.
            reason = f"group_pass:{type(exc).__name__}"
            degradations.append(Degradation(component="retrieval", reason=reason))

    items: list[EvidenceItem] = []
    dropped = sum(len(r.dropped) for r, _ in passes)
    for hit, scope in hits:
        ctx = contexts.get(hit.chunk_id)
        if ctx is None:  # no stored row: never cited
            dropped += 1
            continue
        score_kind = passes[0 if scope == "entity" else -1][0].score_kind.value
        items.append(evidence_item(ctx, rank=len(items) + 1, score_kind=score_kind, scope=scope))

    degraded: list[str] = []
    timings: dict[str, float] = {}
    for result, scope in passes:
        for component in result.degraded:
            if component not in degraded:
                degraded.append(component)
                kind = "rerank" if component == "rerank" else "retrieval"
                degradations.append(Degradation(component=kind, reason=f"{component}_failed"))
        prefix = "" if scope == "entity" else "group_"
        timings |= {f"{prefix}{k}": v for k, v in result.timings_ms.items()}

    status, gaps = classify_evidence(items)
    meta = RetrievalMeta(
        mode=deps.retrieval_mode,
        degraded=degraded,
        failed=False,
        timings_ms=timings,
        score_kind=passes[0][0].score_kind.value,
        evidence_status=status,
        gaps=gaps,
        dropped_count=dropped,
    )
    forms_needed = bool(
        candidate_forms(deps.corpus.form_documents(), profile, (i.document_key for i in items))
    )
    update: dict[str, Any] = {
        "retrieval_query": query,
        "filters": dump(RetrievalFilters(entity_terms=[entity])),
        "evidence": dump_list(items),
        "retrieval_meta": dump(meta),
        "form_analysis_required": forms_needed,
    }
    if degradations:
        update["degradations"] = [*(state.get("degradations") or []), *dump_list(degradations)]
    return update
