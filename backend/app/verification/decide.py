"""The deterministic decision table (M4 S2; M4_PLAN §E, §G).

`verify_requirement` judges one validated requirement against the premises built from its
stored citations and returns a `RequirementVerification` (whose own validators re-check
the invariants). Signals and their single jobs:

- statement analysis (`statements.analyse`): a NECESSARY condition: the window must name
  the item, read with the right modality and polarity, not speak of another entity type
  or another person, for the claim type at hand;
- NLI scores (`NLIScorer`, `FakeNLI` in S2): entailment / contradiction above thresholds;
- evidence role (`state.ALLOWED_ROLES`): which premises may be judged for which claim at
  all. A form chunk is never scored against an obligation claim.

Window -> claim -> requirement:
- a window SUPPORTS a claim when entailment >= `entail` and the analysis is compatible;
- a window CONTRADICTS it when it names the item and either contradiction >=
  `contradict` or it states the obligation negated ("shall not", "need not", ...);
- claim verdict: conflicting (both), supported, contradicted, else insufficient; any
  allowed pair left unscored (no scorer, scorer failure, pair cap) makes it
  `not_verified`, because an unchecked pair is not evidence of anything.
- requirement verdict: against what the item ASSERTS (claims.extract); effects only
  ever lower applicability; MANDATORY additionally needs `state.mandatory_established`.

Thresholds are inputs, never defaults in code: S4 selects them on the dev set under a
pre-registered rule. `entail + contradict > 1`, so one window can never be both.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.graph.state import (
    ALLOWED_ROLES,
    APPLICABILITY_STRICTNESS,
    AtomicClaim,
    ClaimType,
    ClaimVerdict,
    ClaimVerdictLabel,
    EvidenceJudgement,
    EvidenceRole,
    RequirementVerdict,
    RequirementVerification,
    ReviewReason,
    StatementAnalysis,
    ValidatedRequirement,
    mandatory_established,
)
from app.schemas.taxonomy import Applicability, EntityType
from app.verification.nli import NLIError, NLIPair, NLIScore, NLIScorer
from app.verification.premises import Premise, Window
from app.verification.statements import Anchors, analyse, anchors, subject_conflict


@dataclass(frozen=True)
class Thresholds:
    entail: float
    contradict: float
    version: str

    def __post_init__(self) -> None:
        if not (0 < self.entail < 1 and 0 < self.contradict < 1):
            raise ValueError("thresholds lie strictly between 0 and 1")
        if self.entail + self.contradict <= 1:
            raise ValueError("entail + contradict must exceed 1 (a window is never both)")
        if not self.version:
            raise ValueError("thresholds carry a version")


@dataclass
class _Pair:
    claim: AtomicClaim
    premise: Premise
    window: Window
    analysis: StatementAnalysis
    blockers: list[str]  # why this window cannot support the claim (empty = compatible)
    det_contradiction: bool
    score: NLIScore | None = None
    not_scored: str | None = None


@dataclass(frozen=True)
class Verified:
    verification: RequirementVerification
    pairs_scored: int
    pairs_unscored: int
    skipped_sentences: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)


# --- compatibility of one window with one claim ---------------------------------------------


def _claim_polarity(claim: AtomicClaim) -> str:
    return analyse(claim.text, Anchors((), frozenset()), entity=None).polarity


def blockers(claim: AtomicClaim, a: StatementAnalysis) -> list[str]:
    """Why window analysis `a` cannot support `claim`; empty = compatible."""
    out: list[str] = []
    if not a.object_match:
        out.append("anchors_missing")
    if subject_conflict(claim.text, a):
        out.append("subject_mismatch")
    if claim.claim_type == ClaimType.FORM_REQUEST:
        if a.polarity == "negated":
            out.append("polarity_negated")
        return out
    if a.scope == "mismatch":
        out.append("scope_mismatch")
    if claim.claim_type == ClaimType.POLICY_OBLIGATION:
        wanted = {"obligation"} if claim.asserted_strength == "unconditional" else {
            "obligation", "conditional"}  # fmt: skip
        if a.modality not in wanted:
            out.append(f"modality_{a.modality}")
    if a.polarity != "affirmative":
        if claim.claim_type == ClaimType.EXPLANATORY_CLAIM and _claim_polarity(claim) == (
            a.polarity
        ):
            pass  # a negative explanation read against an equally negative window
        else:
            out.append(f"polarity_{a.polarity}")
    return out


def _det_contradiction(claim: AtomicClaim, a: StatementAnalysis) -> bool:
    """The window names the item and states it negated, while the claim is affirmative."""
    return (
        a.object_match
        and a.polarity == "negated"
        and a.modality in ("obligation", "conditional")
        and _claim_polarity(claim) == "affirmative"
        and a.scope != "mismatch"
    )


# --- the table ----------------------------------------------------------------------------------


def _claim_anchors(claim: AtomicClaim, item_anchors: Anchors) -> Anchors:
    return anchors(claim.text) if claim.claim_type == ClaimType.EXPLANATORY_CLAIM else item_anchors


def _pairs(
    claims: Sequence[AtomicClaim],
    premises: Sequence[Premise],
    item: str,
    entity: EntityType | None,
) -> list[_Pair]:
    item_anchors = anchors(item)
    out: list[_Pair] = []
    for claim in claims:
        spec = _claim_anchors(claim, item_anchors)
        for premise in premises:
            if premise.evidence_role not in ALLOWED_ROLES[claim.claim_type]:
                continue
            for window in premise.windows:
                a = analyse(window.text, spec, entity=entity, section=premise.section)
                out.append(
                    _Pair(claim, premise, window, a, blockers(claim, a),
                          _det_contradiction(claim, a))  # fmt: skip
                )
    return out


def _score(pairs: list[_Pair], scorer: NLIScorer | None, max_pairs: int) -> None:
    if scorer is None:
        for p in pairs:
            p.not_scored = "no_scorer"
        return
    batch, over = pairs[:max_pairs], pairs[max_pairs:]
    for p in over:
        p.not_scored = "pair_cap"
    if not batch:
        return
    try:
        scores = scorer.score([NLIPair(p.window.text, p.claim.text) for p in batch])
        if len(scores) != len(batch):
            raise NLIError("the scorer returned the wrong number of scores")
    except NLIError:
        for p in batch:
            p.not_scored = "scorer_failed"
        return
    for p, s in zip(batch, scores, strict=True):
        if s is None:  # never truncated: an unscored pair is evidence of nothing
            p.not_scored = "window_too_long"
        else:
            p.score = s


def _supports(p: _Pair, t: Thresholds) -> bool:
    return p.score is not None and p.score.entailment >= t.entail and not p.blockers


def _contradicts(p: _Pair, t: Thresholds) -> bool:
    if p.det_contradiction:
        return True
    return (
        p.score is not None
        and p.score.contradiction >= t.contradict
        and p.analysis.object_match
        and p.analysis.scope != "mismatch"
    )


def _judgement(p: _Pair) -> EvidenceJudgement:
    pr = p.premise
    scored = p.score is not None
    return EvidenceJudgement(
        claim_id=p.claim.claim_id,
        evidence_id=pr.evidence_id,
        document_key=pr.document_key,
        document_type=pr.document_type,
        page=pr.page,
        page_end=pr.page_end,
        window_chunk_id=pr.window_chunk_id,
        window_start=p.window.start,
        window_end=p.window.end,
        evidence_role=pr.evidence_role,
        evidence_eligibility=pr.evidence_eligibility,
        match_scope=pr.match_scope,
        text_reliability=pr.text_reliability,
        source_status=pr.source_status,
        can_support_mandatory=pr.can_support_mandatory,
        analysis=p.analysis,
        logits=list(p.score.logits) if p.score is not None else None,
        probs=list(p.score.probs) if p.score is not None else None,
        not_scored_reason=None if scored else p.not_scored,
    )


def _ent(p: _Pair) -> float:
    return p.score.entailment if p.score is not None else 0.0


def _con(p: _Pair) -> float:
    """Contradiction strength; a deterministic negated obligation ranks first."""
    return 2.0 if p.det_contradiction else (p.score.contradiction if p.score else 0.0)


def _verdict(
    claim: AtomicClaim,
    label: ClaimVerdictLabel,
    best: _Pair | None = None,
    codes: Sequence[str] = (),
) -> ClaimVerdict:
    return ClaimVerdict(
        claim_id=claim.claim_id,
        verdict=label,
        best_evidence_id=best.premise.evidence_id if best is not None else None,
        reason_codes=list(codes)[:20],
    )


def _judge_claim(
    claim: AtomicClaim, pairs: list[_Pair], t: Thresholds
) -> tuple[ClaimVerdict, list[EvidenceJudgement]]:
    mine = [p for p in pairs if p.claim.claim_id == claim.claim_id]
    if not mine:
        reasons = ["no_allowed_evidence"]
        if claim.claim_type == ClaimType.POLICY_OBLIGATION:
            reasons.append("no_primary_citation")
        return _verdict(claim, "insufficient", codes=reasons), []

    # One judgement per cited chunk (its best window), plus its strongest contradiction.
    judgements: list[EvidenceJudgement] = []
    for evidence_id in dict.fromkeys(p.premise.evidence_id for p in mine):
        group = [p for p in mine if p.premise.evidence_id == evidence_id]
        scored = [p for p in group if p.score is not None]
        if not scored:
            judgements.append(_judgement(group[0]))
            continue
        best = max(scored, key=lambda p: (_supports(p, t), _ent(p)))
        judgements.append(_judgement(best))
        worst = max((p for p in group if _contradicts(p, t)), key=_con, default=None)
        if worst is not None and worst is not best:
            judgements.append(_judgement(worst))

    unscored = [p for p in mine if p.score is None]
    if unscored:  # an unchecked pair is evidence of nothing
        return _verdict(claim, "not_verified", codes=[unscored[0].not_scored or "not_scored"]), (
            judgements
        )

    best_support = max((p for p in mine if _supports(p, t)), key=_ent, default=None)
    best_contra = max((p for p in mine if _contradicts(p, t)), key=_con, default=None)
    _ensure_recorded(judgements, best_support)
    _ensure_recorded(judgements, best_contra)
    if best_support is not None and best_contra is not None:
        return _verdict(claim, "conflicting", best_support, ["support_and_contradiction"]), (
            judgements
        )
    if best_support is not None:
        return _verdict(claim, "supported", best_support), judgements
    if best_contra is not None:
        code = "negated_obligation" if best_contra.det_contradiction else "nli_contradiction"
        return _verdict(claim, "contradicted", best_contra, [code]), judgements
    top = max(mine, key=_ent)
    codes = sorted(set(top.blockers))
    if _ent(top) < t.entail:
        codes.append("below_entailment_threshold")
    return _verdict(claim, "insufficient", codes=codes), judgements


def _ensure_recorded(judgements: list[EvidenceJudgement], p: _Pair | None) -> None:
    """The verdict's best evidence must be a recorded judgement of that window."""
    if p is None:
        return
    key = (p.claim.claim_id, p.premise.evidence_id, p.window.start, p.window.end)
    recorded = {(j.claim_id, j.evidence_id, j.window_start, j.window_end) for j in judgements}
    if key not in recorded:
        judgements.append(_judgement(p))


def requirement_verdict(
    claims: Sequence[AtomicClaim], verdicts: Sequence[ClaimVerdict]
) -> RequirementVerdict:
    """Judged against what the item asserts (M4_PLAN §G)."""
    if not claims:
        return "not_verified"
    label = {v.claim_id: v.verdict for v in verdicts}
    labels = list(label.values())
    if "conflicting" in labels or ("supported" in labels and "contradicted" in labels):
        return "conflicting"
    if "contradicted" in labels:
        return "contradicted"
    core = next((c for c in claims if c.claim_type == ClaimType.POLICY_OBLIGATION), None)
    others = [label[c.claim_id] for c in claims if c is not core]
    if core is not None:
        core_label = label[core.claim_id]
        if core_label == "not_verified":
            return "not_verified"
        if core_label == "supported":
            return "supported" if all(v == "supported" for v in others) else "partially_supported"
        weak_types = (ClaimType.FORM_REQUEST, ClaimType.SUPPORTING_STATEMENT)
        weaker = [label[c.claim_id] for c in claims if c.claim_type in weak_types]
        return "supported_weaker_than_asserted" if "supported" in weaker else "unsupported"
    if all(v == "not_verified" for v in labels):
        return "not_verified"
    supported = labels.count("supported")
    if supported == len(labels):
        return "supported"
    if supported:
        return "partially_supported"
    return "not_verified" if "not_verified" in labels else "unsupported"


# Applicability cap per requirement verdict; the result is min(before, cap): never higher.
CAP: dict[str, Applicability] = {
    "supported": Applicability.MANDATORY,
    "partially_supported": Applicability.MANDATORY,
    "supported_weaker_than_asserted": Applicability.CONDITIONAL,
    "unsupported": Applicability.UNCLEAR,
    "contradicted": Applicability.UNCLEAR,
    "conflicting": Applicability.UNCLEAR,
    "not_verified": Applicability.CONDITIONAL,
}
FLAG: dict[str, ReviewReason] = {
    "unsupported": "unsupported",
    "contradicted": "contradicted",
    "conflicting": "conflicting_sources",
    "not_verified": "not_verified",
}


def _lower(a: Applicability, b: Applicability) -> Applicability:
    return min(a, b, key=APPLICABILITY_STRICTNESS.__getitem__)


def verify_requirement(
    req: ValidatedRequirement,
    claims: Sequence[AtomicClaim],
    premises: Sequence[Premise],
    *,
    scorer: NLIScorer | None,
    thresholds: Thresholds,
    entity: EntityType | None,
    max_pairs: int,
) -> Verified:
    if max_pairs < 0:
        raise ValueError("max_pairs must be >= 0")
    pairs = _pairs(claims, premises, req.name, entity)
    _score(pairs, scorer, max_pairs)
    verdicts: list[ClaimVerdict] = []
    judgements: list[EvidenceJudgement] = []
    for claim in claims:
        v, js = _judge_claim(claim, pairs, thresholds)
        verdicts.append(v)
        judgements += js
    verdict = requirement_verdict(claims, verdicts)

    before = req.applicability
    after = _lower(before, CAP[verdict])
    flags: list[ReviewReason] = [FLAG[verdict]] if verdict in FLAG else []
    by_id = {c.claim_id: c for c in claims}
    if after == Applicability.MANDATORY and not mandatory_established(by_id, verdicts, judgements):
        after = Applicability.CONDITIONAL
    positive = ("supported", "partially_supported", "supported_weaker_than_asserted")
    if before == Applicability.MANDATORY != after and verdict in positive:
        flags.append("mandatory_not_established")
    best_role = {(j.claim_id, j.evidence_id): j.evidence_role for j in judgements}
    for v in verdicts:
        c = by_id[v.claim_id]
        if (
            c.claim_type == ClaimType.EXPLANATORY_CLAIM
            and c.asserted_strength != "none"
            and v.verdict == "supported"
            and best_role.get((v.claim_id, v.best_evidence_id or "")) != EvidenceRole.PRIMARY_POLICY
        ):
            flags.append("explanation_overstates_source")
            break
    verification = RequirementVerification(
        requirement_id=req.requirement_id,
        claims=list(claims),
        judgements=judgements,
        verdicts=verdicts,
        requirement_verdict=verdict,
        applicability_before=before,
        applicability_after=after,
        review_flags=list(dict.fromkeys(flags)),
    )
    scored = sum(1 for p in pairs if p.score is not None)
    return Verified(verification, pairs_scored=scored, pairs_unscored=len(pairs) - scored)


@dataclass(frozen=True)
class JudgedClaim:
    """One claim judged against premises: the S2 table, exposed for evaluation (M4 S4)."""

    verdict: ClaimVerdict
    judgements: tuple[EvidenceJudgement, ...]
    pairs_scored: int
    pairs_unscored: int
    scores: tuple[NLIScore, ...]  # every scored pair, in pair order (for the NLI-only ablation)


def judge_claim(
    claim: AtomicClaim,
    premises: Sequence[Premise],
    *,
    scorer: NLIScorer | None,
    thresholds: Thresholds,
    entity: EntityType | None,
    max_pairs: int,
    item: str | None = None,
) -> JudgedClaim:
    """Judge ONE claim with exactly the logic `verify_requirement` uses (no copy of it).
    `item` names the requirement for anchor matching; without one, the claim's own text
    supplies the anchors (dataset claims have no separate item name)."""
    if max_pairs < 0:
        raise ValueError("max_pairs must be >= 0")
    pairs = _pairs([claim], premises, item if item is not None else claim.text, entity)
    _score(pairs, scorer, max_pairs)
    verdict, judgements = _judge_claim(claim, pairs, thresholds)
    scored = tuple(p.score for p in pairs if p.score is not None)
    return JudgedClaim(verdict, tuple(judgements), len(scored), len(pairs) - len(scored), scored)


def not_verified_requirement(
    req: ValidatedRequirement, claims: Sequence[AtomicClaim], *, reason: str
) -> RequirementVerification:
    """Every claim `not_verified` without judging anything (M4 S5): used when semantic
    verification is unavailable (no selected NLI model) or a premise cannot be built.

    Nothing is scored and no rule-based reading is applied, so a missing check is never
    turned into a contradiction, an absence of evidence or a finding that a claim is false.
    The only effect is the `not_verified` cap (MANDATORY cannot be established without a
    supported claim); applicability is never raised."""
    verdicts = [
        ClaimVerdict(claim_id=c.claim_id, verdict="not_verified", reason_codes=[reason])
        for c in claims
    ]
    before = req.applicability
    return RequirementVerification(
        requirement_id=req.requirement_id,
        claims=list(claims),
        judgements=[],
        verdicts=verdicts,
        requirement_verdict="not_verified",
        applicability_before=before,
        applicability_after=_lower(before, CAP["not_verified"]),
        review_flags=[FLAG["not_verified"]],
    )
