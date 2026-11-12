"""Pre-registered NLI checkpoint and threshold selection (M4 S4; registered in S4.1, before
any model was downloaded or run; applied once in S4.2 on the DEV set only).

The rule (owner-approved plan, decisions D1-D5):
- Candidates: the four checkpoints pinned in S0 x the threshold grid
  tau_e, tau_c in {0.50, 0.60, 0.70, 0.80, 0.90, 0.95} with tau_e + tau_c > 1.
- Hard gates (else disqualified): MANDATORY false positives = 0; obligation-from-form
  errors = 0; the load sanity triplet passed; a second scoring pass reproduced every score
  within 1e-6.
- Qualification: false-support rate <= 1/20 (at most 2 of the 40 non-entailed dev cases
  predicted entailed).
- Objective among qualifiers: maximise entailed recall.
- Tie-breaks, in order: lower false-contradiction rate; higher macro-F1; smaller model
  (xsmall < small < base); higher tau_e; higher tau_c; lower p50 per-pair latency;
  candidate id.
- If nothing qualifies: no selection. The record says so, and NLI stays off by default
  (every claim not_verified, an honest DEGRADED outcome in S5).

Known limitation, stated in the record: choosing thresholds on 55 dev cases overfits; the
one-time held-out run in S8 is the only unbiased estimate and is reported as is. Bootstrap
intervals are reported, never decisive. The record is written once; `select` refuses to
overwrite it.
"""

import json
from dataclasses import dataclass
from fractions import Fraction
from itertools import product
from pathlib import Path
from typing import Any

from app.verification.decide import Thresholds
from app.verification.evaluation import Metrics, metrics_json

PROTOCOL_VERSION = "m4-s4-v1"
GRID: tuple[float, ...] = (0.50, 0.60, 0.70, 0.80, 0.90, 0.95)
FSR_CAP = Fraction(1, 20)  # = 2/40 on the 55-case dev set
DETERMINISM_TOLERANCE = 1e-6


@dataclass(frozen=True)
class Candidate:
    id: str
    model_id: str
    revision: str
    size_rank: int  # 0 xsmall < 1 small < 2 base (tie-break: smaller first)


# Pinned in S0 (docs/milestones/M4_PLAN.md, "S0 outcome"): licences, revisions, label maps.
CANDIDATES: tuple[Candidate, ...] = (
    Candidate("N1", "cross-encoder/nli-deberta-v3-xsmall",
              "a150876415327c80daeff35ca6f68f5ed8cf5c24", 0),
    Candidate("N2", "cross-encoder/nli-deberta-v3-small",
              "fa2804872c3b4bd748f38c0185cc85775361e735", 1),
    Candidate("N3", "cross-encoder/nli-deberta-v3-base",
              "6c749ce3425cd33b46d187e45b92bbf96ee12ec7", 2),
    Candidate("N4", "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli",
              "6f5cf0a2b59cabb106aca4c287eed12e357e90eb", 2),
)  # fmt: skip


def threshold_grid(version_prefix: str) -> list[Thresholds]:
    """Every registered (tau_e, tau_c) pair with tau_e + tau_c > 1."""
    return [
        Thresholds(e, c, f"{version_prefix}:e{e:.2f}:c{c:.2f}")
        for e, c in product(GRID, GRID)
        if e + c > 1 + 1e-9
    ]


@dataclass(frozen=True)
class Outcome:
    candidate: Candidate
    thresholds: Thresholds
    metrics: Metrics
    sanity_ok: bool
    deterministic: bool
    p50_ms: float

    @property
    def gate_failures(self) -> list[str]:
        out = []
        if self.metrics.mandatory_false_positives:
            out.append("mandatory_false_positive")
        if self.metrics.obligation_from_form_errors:
            out.append("obligation_from_form")
        if not self.sanity_ok:
            out.append("sanity_triplet")
        if not self.deterministic:
            out.append("non_deterministic")
        return out

    @property
    def qualifies(self) -> bool:
        return not self.gate_failures and self.metrics.false_support_rate <= FSR_CAP

    def key(self) -> tuple[Any, ...]:
        m = self.metrics
        return (
            -m.recall["entailed"],
            m.false_contradiction_rate,
            -m.macro_f1,
            self.candidate.size_rank,
            -self.thresholds.entail,
            -self.thresholds.contradict,
            self.p50_ms,
            self.candidate.id,
        )


@dataclass(frozen=True)
class Decision:
    selected: Outcome | None
    qualifiers: list[Outcome]
    outcomes: list[Outcome]


def decide(outcomes: list[Outcome]) -> Decision:
    qualifiers = sorted((o for o in outcomes if o.qualifies), key=Outcome.key)
    return Decision(qualifiers[0] if qualifiers else None, qualifiers, outcomes)


# --- the committed record and the runtime loader ---------------------------------------------


def outcome_json(o: Outcome) -> dict[str, Any]:
    return {
        "candidate": o.candidate.id,
        "tau_e": o.thresholds.entail,
        "tau_c": o.thresholds.contradict,
        "thresholds_version": o.thresholds.version,
        "qualifies": o.qualifies,
        "gate_failures": o.gate_failures,
        "p50_ms": round(o.p50_ms, 3),
        "metrics": metrics_json(o.metrics),
    }


def write_record(path: Path, record: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"{path} exists: the selection runs once (no retuning)")
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class Selected:
    model_id: str
    revision: str
    thresholds: Thresholds


def load_selected(path: Path) -> Selected | None:
    """The configuration the committed record selected, or None (no record, or nothing
    qualified). Thresholds come only from here, never from code or settings."""
    if not path.exists():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("protocol") != PROTOCOL_VERSION:
        raise ValueError(f"unknown selection protocol: {record.get('protocol')!r}")
    chosen = record.get("selected")
    if chosen is None:
        return None
    candidate = next(c for c in CANDIDATES if c.id == chosen["candidate"])
    return Selected(
        model_id=candidate.model_id,
        revision=candidate.revision,
        thresholds=Thresholds(chosen["tau_e"], chosen["tau_c"], chosen["thresholds_version"]),
    )
