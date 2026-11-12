"""Bounded source-conflict scan and the supersession rule (M4 S6; M4_PLAN §H, decision D10).

Bounded, not exhaustive:
- Only evidence ALREADY retrieved in this run is compared; nothing is retrieved here.
- A candidate is a pair of passage windows from two DIFFERENT documents that both name a
  requirement's item (the S2 anchor rule), at least one of them cited by that requirement.
- Pairs are unique across the run (a pair shared by two requirements is scored once) and
  ordered deterministically. At most `max_pairs` unique pairs are scored, each in both
  directions; eligible pairs beyond the cap, or that cannot be scored (a window too long,
  a scorer failure), are counted as not scanned, never silently dropped.

A pair is a conflict when the model's contradiction probability reaches the contradiction
threshold in either direction. Nothing else creates a conflict: the S2 deterministic rules
alone never do (owner decision S6-1). Without a scorer the scan does not run, and that is
reported as such, never as "no conflict found".

Resolution (D10): a conflict is resolved only when a recorded supersession relation links
the two documents, the superseding document is `active`, and its version review is
`confirmed`. A publication or issue date never resolves anything. A failed or incomplete
relation lookup leaves the conflict unresolved with a structured reason code.

A conflict adds the `conflicting_sources` review flag and keeps both sides visible; it never
changes applicability (owner decision S6-3).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.db.repository import DocumentRelationRow
from app.graph.state import (
    ConflictResolution,
    ConflictSide,
    ResolutionReason,
    SourceConflict,
)
from app.schemas.taxonomy import SourceStatus, VersionReviewStatus
from app.verification.decide import Thresholds
from app.verification.nli import NLIError, NLIPair, NLIScorer
from app.verification.premises import Premise, Window
from app.verification.statements import anchors, anchors_missing

PassageKey = tuple[str, int, int]  # (evidence_id, window_start, window_end)


@dataclass(frozen=True)
class Passage:
    premise: Premise
    window: Window

    @property
    def key(self) -> PassageKey:
        return (self.premise.evidence_id, self.window.start, self.window.end)


@dataclass(frozen=True)
class ScanItem:
    """One requirement as the scan sees it: its item name and cited chunk ids."""

    requirement_id: str
    name: str
    cited: frozenset[str]


@dataclass
class Candidate:
    """A unique passage pair (a before b by key) and the requirements it concerns."""

    a: Passage
    b: Passage
    requirements: list[ScanItem] = field(default_factory=list)


@dataclass(frozen=True)
class ScanResult:
    conflicts: tuple[tuple[Candidate, float], ...]  # (pair, max contradiction probability)
    pairs_scored: int
    pairs_not_scanned: int
    failed: bool


def passages_naming(item: str, premises: Sequence[Premise]) -> list[Passage]:
    """Windows that name the item, in a deterministic order. An item with no usable anchor
    names nothing (it can never be matched safely)."""
    spec = anchors(item)
    if not spec.strict and not spec.words:
        return []
    out = [Passage(p, w) for p in premises for w in p.windows if not anchors_missing(spec, w.text)]
    return sorted(out, key=lambda x: x.key)


def candidate_pairs(items: Sequence[ScanItem], premises: Sequence[Premise]) -> list[Candidate]:
    """Unique cross-document pairs, in a deterministic order (requirements in checklist
    order, then passage keys). Each pair involves a passage cited by the requirement."""
    by_key: dict[tuple[PassageKey, PassageKey], Candidate] = {}
    ordered = sorted(premises, key=lambda p: p.evidence_id)
    for item in items:
        found = passages_naming(item.name, ordered)
        for i, a in enumerate(found):
            for b in found[i + 1 :]:
                if a.premise.document_key == b.premise.document_key:
                    continue
                if not ({a.premise.evidence_id, b.premise.evidence_id} & item.cited):
                    continue
                cand = by_key.setdefault((a.key, b.key), Candidate(a, b))
                if all(r.requirement_id != item.requirement_id for r in cand.requirements):
                    cand.requirements.append(item)
    return list(by_key.values())


def scan(
    candidates: Sequence[Candidate],
    scorer: NLIScorer,
    thresholds: Thresholds,
    max_pairs: int,
) -> ScanResult:
    """Score at most `max_pairs` unique pairs, each in both directions."""
    if max_pairs < 0:
        raise ValueError("max_pairs must be >= 0")
    selected = list(candidates[:max_pairs])
    over = len(candidates) - len(selected)
    if not selected:
        return ScanResult((), 0, over, failed=False)
    batch = [
        pair
        for c in selected
        for pair in (
            NLIPair(c.a.window.text, c.b.window.text),
            NLIPair(c.b.window.text, c.a.window.text),
        )
    ]
    try:
        scores = scorer.score(batch)
        if len(scores) != len(batch):
            raise NLIError("the scorer returned the wrong number of scores")
    except NLIError:
        return ScanResult((), 0, len(candidates), failed=True)
    conflicts: list[tuple[Candidate, float]] = []
    scored = unscoreable = 0
    for n, cand in enumerate(selected):
        forward, backward = scores[2 * n], scores[2 * n + 1]
        if forward is None or backward is None:  # never truncated: not scanned
            unscoreable += 1
            continue
        scored += 1
        worst = max(forward.contradiction, backward.contradiction)
        if worst >= thresholds.contradict:
            conflicts.append((cand, worst))
    return ScanResult(tuple(conflicts), scored, over + unscoreable, failed=False)


def resolve(
    key_a: str, key_b: str, relations: dict[str, DocumentRelationRow] | None
) -> tuple[ConflictResolution, ResolutionReason, str | None]:
    """D10: resolved only by a recorded supersession whose superseding document is active
    and whose version review is confirmed. Dates are never consulted."""
    if relations is None:
        return "unresolved", "relation_lookup_failed", None
    if key_a not in relations or key_b not in relations:
        return "unresolved", "relation_incomplete", None
    superseding = [
        (new, old)
        for new, old in ((key_a, key_b), (key_b, key_a))
        if old in relations[new].supersedes
    ]
    if len(superseding) != 1:  # none, or a contradictory mutual relation
        return "unresolved", "no_supersession_relation", None
    new, _ = superseding[0]
    row = relations[new]
    if row.source_status != SourceStatus.ACTIVE.value:
        return "unresolved", "superseding_not_active", None
    if row.version_review_status != VersionReviewStatus.CONFIRMED.value:
        return "unresolved", "version_review_not_confirmed", None
    return "superseded", "supersession_confirmed", new


def _side(p: Passage, cited: frozenset[str]) -> ConflictSide:
    return ConflictSide(
        evidence_id=p.premise.evidence_id,
        document_key=p.premise.document_key,
        page=p.premise.page,
        page_end=p.premise.page_end,
        window_start=p.window.start,
        window_end=p.window.end,
        source_status=p.premise.source_status,
        evidence_eligibility=p.premise.evidence_eligibility,
        cited=p.premise.evidence_id in cited,
    )


def source_conflicts(
    result: ScanResult, relations: dict[str, DocumentRelationRow] | None
) -> list[SourceConflict]:
    """One record per (conflicting pair, requirement it concerns): ids, offsets, codes."""
    out: list[SourceConflict] = []
    for cand, worst in result.conflicts:
        resolution, reason, superseding = resolve(
            cand.a.premise.document_key, cand.b.premise.document_key, relations
        )
        for item in cand.requirements:
            out.append(
                SourceConflict(
                    requirement_id=item.requirement_id,
                    side_a=_side(cand.a, item.cited),
                    side_b=_side(cand.b, item.cited),
                    max_contradiction=round(min(max(worst, 0.0), 1.0), 6),
                    resolution=resolution,
                    resolution_reason=reason,
                    superseding_document_key=superseding,
                )
            )
    return out
