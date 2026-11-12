"""Claim-verification evaluation on the DEV set only (M4 S4; docs/10, M4_LABELLING_GUIDELINES).

Every dataset case is judged by exactly the S2 verifier (`decide.judge_claim`) against
premises built from the STORED Postgres chunk, never from the dataset's quotes. The held-out
set is refused here: it is read once, in S8, for an already-selected configuration.

Verdict -> label: supported -> entailed; contradicted -> contradicted; insufficient,
conflicting and not_verified -> insufficient (the verifier abstains). Conflicting and
not_verified are also counted on their own.

Metrics are exact fractions (as in `app.retrieval.selection`). The safety metric is the
false-support rate: P(predicted entailed | gold is not entailed). MANDATORY agreement uses
the same `state.mandatory_established` rule the report model enforces.

Ablations (registered with the protocol, docs/10 "A vs D"):
- `rules_only`: no scorer, so nothing can be supported (only rule-based contradictions);
- `nli_only`: the model alone, no gates: over all scored pairs, the pair with the highest
  entailment-or-contradiction probability decides by its argmax; no pair -> insufficient;
- `combined`: the verifier as designed.
"""

import hashlib
import json
import statistics
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

from app.config import REPO_ROOT
from app.graph.corpus import CorpusReader
from app.graph.state import (
    AtomicClaim,
    Citation,
    ClaimType,
    EvidenceRole,
    mandatory_established,
)
from app.schemas.taxonomy import EntityType
from app.verification.decide import JudgedClaim, Thresholds, judge_claim
from app.verification.nli import NLIPair, NLIScore, NLIScorer
from app.verification.premises import Premise, build_premise
from app.verification.statements import analyse, anchors

DEV_PATH = REPO_ROOT / "data" / "eval" / "m4_claims_dev.jsonl"
HELDOUT_NAME = "m4_claims_heldout"
DATASET_TEMPLATE = "dataset-v1"  # typed dataset claims are written by hand, not templated
LABELS = ("entailed", "contradicted", "insufficient")
Label = Literal["entailed", "contradicted", "insufficient"]
Mode = Literal["rules_only", "nli_only", "combined"]


class EvaluationError(ValueError):
    """The evaluation was asked to do something the protocol forbids."""


# --- dataset ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    id: str
    claim: str
    claim_type: ClaimType
    entity: EntityType | None
    match_scope: Literal["entity", "entity_group"]
    gold: Label
    expected_obligation: bool
    perturbations: tuple[str, ...]
    chunk_ids: tuple[str, ...]


def load_dev(path: Path = DEV_PATH) -> tuple[list[Case], str]:
    """The dev cases and the file's SHA-256 (LF-normalised). Any other file is refused."""
    if HELDOUT_NAME in path.name or path.resolve() != DEV_PATH.resolve():
        raise EvaluationError("S4 evaluates the dev set only; the held-out set is for S8")
    data = path.read_bytes().replace(b"\r\n", b"\n")
    cases = []
    for line in data.decode("utf-8").splitlines():
        r = json.loads(line)
        if r["split"] != "dev":
            raise EvaluationError(f"{r['id']} is not a dev case")
        cases.append(
            Case(
                id=r["id"],
                claim=r["claim"],
                claim_type=ClaimType(r["claim_type"]),
                entity=EntityType(r["entity"]) if r["entity"] else None,
                match_scope=r["match_scope"],
                gold=r["gold"],
                expected_obligation=r["expected_obligation"],
                perturbations=tuple(r["perturbations"]),
                chunk_ids=tuple(e["chunk_id"] for e in r["evidence"]),
            )
        )
    return cases, hashlib.sha256(data).hexdigest()


def premises_for(case: Case, corpus: CorpusReader) -> list[Premise]:
    """Premises from the stored chunks; provenance and eligibility come from Postgres."""
    contexts = corpus.evidence(case.chunk_ids)
    missing = [c for c in case.chunk_ids if c not in contexts]
    if missing:
        raise EvaluationError(f"{case.id}: chunks not stored: {missing}")
    out = []
    for cid in case.chunk_ids:
        ctx = contexts[cid]
        stored, shown = ctx.retrieved.metadata, ctx.context.metadata
        citation = Citation(
            evidence_id=stored.chunk_id,
            document_key=stored.document_key,
            page=shown.page_number,
            page_end=shown.page_end,
            paragraph=stored.paragraph,
            match_scope=case.match_scope,
            evidence_eligibility=stored.evidence_eligibility,
            text_reliability=stored.text_reliability,
            source_status=stored.source_status,
            can_support_mandatory=stored.can_support_mandatory,
        )
        out.append(build_premise(citation, ctx))
    return out


def claim_for(case: Case) -> AtomicClaim:
    """The case as one atomic claim of its own type, its text verbatim."""
    reading = analyse(case.claim, anchors(case.claim), entity=None)
    if case.claim_type in (ClaimType.FORM_REQUEST, ClaimType.SUPPORTING_STATEMENT):
        strength: Literal["unconditional", "conditional", "none"] = "none"
    elif reading.modality == "conditional":
        strength = "conditional"
    elif case.claim_type == ClaimType.POLICY_OBLIGATION or (
        reading.modality == "obligation" and reading.polarity == "affirmative"
    ):
        strength = "unconditional"
    else:
        strength = "none"
    explanatory = case.claim_type == ClaimType.EXPLANATORY_CLAIM
    return AtomicClaim(
        claim_id=f"{case.id}-C0",
        requirement_id=case.id,
        claim_type=case.claim_type,
        asserted_strength=strength,
        text=case.claim,
        template_version=None if explanatory else DATASET_TEMPLATE,
        source_sentence_index=0 if explanatory else None,
    )


# --- scoring once per model ------------------------------------------------------------------


@dataclass
class CachedScorer:
    """Scores each distinct (premise, hypothesis) pair once, so the threshold grid reuses
    the same model outputs. Records per-call latency for the report."""

    inner: NLIScorer
    cache: dict[tuple[str, str], NLIScore | None] = field(default_factory=dict)
    call_ms: list[float] = field(default_factory=list)
    pairs_per_call: list[int] = field(default_factory=list)
    model_id: str = field(init=False)
    model_revision: str = field(init=False)
    label_map: dict[str, str] = field(init=False)

    def __post_init__(self) -> None:
        self.model_id = self.inner.model_id
        self.model_revision = self.inner.model_revision
        self.label_map = self.inner.label_map

    def score(self, pairs: Sequence[NLIPair]) -> list[NLIScore | None]:
        todo = list(dict.fromkeys((p.premise, p.hypothesis) for p in pairs))
        todo = [k for k in todo if k not in self.cache]
        if todo:
            started = time.perf_counter()
            got = self.inner.score([NLIPair(a, b) for a, b in todo])
            self.call_ms.append((time.perf_counter() - started) * 1000)
            self.pairs_per_call.append(len(todo))
            self.cache.update(zip(todo, got, strict=True))
        return [self.cache[(p.premise, p.hypothesis)] for p in pairs]


# --- predictions -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Prediction:
    case_id: str
    label: Label
    verdict: str  # the verifier's own verdict (before mapping to a label)
    would_be_mandatory: bool
    pairs_scored: int
    pairs_unscored: int
    reason_codes: tuple[str, ...]


def _label(verdict: str) -> Label:
    if verdict == "supported":
        return "entailed"
    if verdict == "contradicted":
        return "contradicted"
    return "insufficient"  # insufficient, conflicting, not_verified: the verifier abstains


def _nli_only(scores: Iterable[NLIScore]) -> Label:
    best = max(scores, key=lambda s: max(s.entailment, s.contradiction), default=None)
    if best is None or best.label == "neutral":
        return "insufficient"
    return "entailed" if best.label == "entailment" else "contradicted"


def predict(
    case: Case,
    premises: Sequence[Premise],
    *,
    scorer: NLIScorer | None,
    thresholds: Thresholds,
    max_pairs: int,
    mode: Mode = "combined",
) -> Prediction:
    claim = claim_for(case)
    judged: JudgedClaim = judge_claim(
        claim,
        premises,
        scorer=None if mode == "rules_only" else scorer,
        thresholds=thresholds,
        entity=case.entity,
        max_pairs=max_pairs,
    )
    if mode == "nli_only":
        label = _nli_only(judged.scores)
        verdict = f"nli_only:{label}"
        mandatory = False  # never: the model alone cannot establish an obligation
    else:
        verdict = judged.verdict.verdict
        label = _label(verdict)
        mandatory = mandatory_established(
            {claim.claim_id: claim}, [judged.verdict], list(judged.judgements)
        )
    return Prediction(
        case_id=case.id,
        label=label,
        verdict=verdict,
        would_be_mandatory=mandatory,
        pairs_scored=judged.pairs_scored,
        pairs_unscored=judged.pairs_unscored,
        reason_codes=tuple(judged.verdict.reason_codes),
    )


# --- metrics ---------------------------------------------------------------------------------


def _ratio(num: int, den: int) -> Fraction:
    return Fraction(num, den) if den else Fraction(0)


def _f1(p: Fraction, r: Fraction) -> Fraction:
    return 2 * p * r / (p + r) if p + r else Fraction(0)


@dataclass(frozen=True)
class Metrics:
    n: int
    precision: dict[str, Fraction]
    recall: dict[str, Fraction]
    f1: dict[str, Fraction]
    macro_f1: Fraction
    accuracy: Fraction
    false_support_rate: Fraction  # P(pred entailed | gold != entailed)
    false_contradiction_rate: Fraction  # P(pred contradicted | gold != contradicted)
    abstention_rate: Fraction  # predicted insufficient
    coverage: Fraction  # predicted entailed or contradicted
    selective_accuracy: Fraction  # accuracy among covered cases
    mandatory_false_positives: int  # would be MANDATORY though not expected
    mandatory_false_negatives: int  # expected but not established (reported, not a gate)
    obligation_from_form_errors: int  # MANDATORY established with any form evidence
    verdicts: dict[str, int]
    per_perturbation: dict[str, Fraction]  # accuracy per tag


def compute_metrics(
    cases: Sequence[Case],
    preds: Sequence[Prediction],
    roles: dict[str, set[EvidenceRole]],
) -> Metrics:
    """`roles`: evidence roles of each case's stored chunks (for the form-error count)."""
    by_id = {p.case_id: p for p in preds}
    if set(by_id) != {c.id for c in cases}:
        raise EvaluationError("one prediction per case is required")
    pairs = [(c, by_id[c.id]) for c in cases]
    precision, recall, f1 = {}, {}, {}
    for lab in LABELS:
        tp = sum(1 for c, p in pairs if c.gold == lab and p.label == lab)
        pred_n = sum(1 for _, p in pairs if p.label == lab)
        gold_n = sum(1 for c, _ in pairs if c.gold == lab)
        precision[lab], recall[lab] = _ratio(tp, pred_n), _ratio(tp, gold_n)
        f1[lab] = _f1(precision[lab], recall[lab])
    not_ent = [p for c, p in pairs if c.gold != "entailed"]
    not_con = [p for c, p in pairs if c.gold != "contradicted"]
    covered = [(c, p) for c, p in pairs if p.label != "insufficient"]
    tags = sorted({t for c in cases for t in c.perturbations})
    return Metrics(
        n=len(pairs),
        precision=precision,
        recall=recall,
        f1=f1,
        macro_f1=sum(f1.values(), Fraction(0)) / len(LABELS),
        accuracy=_ratio(sum(1 for c, p in pairs if c.gold == p.label), len(pairs)),
        false_support_rate=_ratio(sum(1 for p in not_ent if p.label == "entailed"), len(not_ent)),
        false_contradiction_rate=_ratio(
            sum(1 for p in not_con if p.label == "contradicted"), len(not_con)
        ),
        abstention_rate=_ratio(len(pairs) - len(covered), len(pairs)),
        coverage=_ratio(len(covered), len(pairs)),
        selective_accuracy=_ratio(sum(1 for c, p in covered if c.gold == p.label), len(covered)),
        mandatory_false_positives=sum(
            1 for c, p in pairs if p.would_be_mandatory and not c.expected_obligation
        ),
        mandatory_false_negatives=sum(
            1 for c, p in pairs if c.expected_obligation and not p.would_be_mandatory
        ),
        obligation_from_form_errors=sum(
            1 for c, p in pairs if p.would_be_mandatory and EvidenceRole.FORM in roles[c.id]
        ),
        verdicts=dict(sorted(_counts(p.verdict for _, p in pairs).items())),
        per_perturbation={
            t: _ratio(
                sum(1 for c, p in pairs if t in c.perturbations and c.gold == p.label),
                sum(1 for c, _ in pairs if t in c.perturbations),
            )
            for t in tags
        },
    )


def _counts(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def latency_summary(scorer: CachedScorer) -> dict[str, float]:
    """Per-pair latency from the scoring calls (indicative; laptop CPU)."""
    per_pair = [ms / n for ms, n in zip(scorer.call_ms, scorer.pairs_per_call, strict=True) if n]
    if not per_pair:
        return {"pairs": 0.0}
    ordered = sorted(per_pair)
    return {
        "pairs": float(sum(scorer.pairs_per_call)),
        "calls": float(len(scorer.call_ms)),
        "per_pair_p50_ms": statistics.median(ordered),
        "per_pair_p95_ms": ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))],
        "total_ms": sum(scorer.call_ms),
    }


def fraction_json(x: Fraction) -> dict[str, Any]:
    return {"num": x.numerator, "den": x.denominator, "value": round(float(x), 6)}


def metrics_json(m: Metrics) -> dict[str, Any]:
    def frac_map(d: dict[str, Fraction]) -> dict[str, Any]:
        return {k: fraction_json(v) for k, v in d.items()}

    return {
        "n": m.n,
        "precision": frac_map(m.precision),
        "recall": frac_map(m.recall),
        "f1": frac_map(m.f1),
        "macro_f1": fraction_json(m.macro_f1),
        "accuracy": fraction_json(m.accuracy),
        "false_support_rate": fraction_json(m.false_support_rate),
        "false_contradiction_rate": fraction_json(m.false_contradiction_rate),
        "abstention_rate": fraction_json(m.abstention_rate),
        "coverage": fraction_json(m.coverage),
        "selective_accuracy": fraction_json(m.selective_accuracy),
        "mandatory_false_positives": m.mandatory_false_positives,
        "mandatory_false_negatives": m.mandatory_false_negatives,
        "obligation_from_form_errors": m.obligation_from_form_errors,
        "verdicts": m.verdicts,
        "per_perturbation": frac_map(m.per_perturbation),
    }
