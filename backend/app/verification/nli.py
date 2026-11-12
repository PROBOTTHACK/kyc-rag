"""NLI scorer contract and a deterministic fake (M4 S2; M4_PLAN §F).

S2 defines the contract only; the real local DeBERTa cross-encoder arrives in S4 behind
the same `NLIScorer` protocol. No model is loaded or downloaded here.

Semantics every scorer must follow:
- **Ordering:** each pair is `(premise, hypothesis)` = (evidence window, claim), the MNLI
  convention. `score` receives `NLIPair` objects, so the order cannot be swapped by
  accident.
- **Labels:** results are returned in the canonical order `NLI_LABELS` (entailment,
  neutral, contradiction), whatever the model's own `id2label` order. A real scorer maps
  from its `label_map` (read from the model config at load time), never from a hard-coded
  index.
- **Logits vs probabilities:** a scorer returns raw logits; `softmax` turns them into
  probabilities here. Both are stored. They are uncalibrated model signals compared with
  dev-selected thresholds, never a confidence and never a legal or regulatory certainty.
- **Failure:** a scorer raises `NLIError`; the caller marks the affected claims
  `not_verified` (never "supported", never "not required").
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from app.graph.state import NLI_LABELS


class NLIError(RuntimeError):
    """The scorer could not produce scores (load, label-map or inference failure)."""


@dataclass(frozen=True)
class NLIPair:
    premise: str  # evidence window text (untrusted data: classified, never obeyed)
    hypothesis: str  # the claim


@dataclass(frozen=True)
class NLIScore:
    logits: tuple[float, float, float]  # NLI_LABELS order

    @property
    def probs(self) -> tuple[float, float, float]:
        return softmax(self.logits)

    @property
    def entailment(self) -> float:
        return self.probs[0]

    @property
    def contradiction(self) -> float:
        return self.probs[2]


def softmax(logits: Sequence[float]) -> tuple[float, float, float]:
    """Numerically stable softmax over exactly three finite logits."""
    if len(logits) != len(NLI_LABELS) or not all(math.isfinite(x) for x in logits):
        raise NLIError("a score needs three finite logits")
    top = max(logits)
    exps = [math.exp(x - top) for x in logits]
    total = math.fsum(exps)
    a, b, c = (e / total for e in exps)
    return a, b, c


class NLIScorer(Protocol):
    model_id: str
    model_revision: str
    label_map: dict[str, str]  # the model's own id2label, as read at load time

    def score(self, pairs: Sequence[NLIPair]) -> list[NLIScore]: ...


Rule = Callable[[NLIPair], tuple[float, float, float] | None]


@dataclass
class FakeNLI:
    """A deterministic scripted scorer for tests and the S2 decision table.

    `rules` are tried in order; the first that returns logits wins, else `default`
    (neutral). Every call is recorded (`calls`) so tests can assert pair ordering and
    budgets. `fail` makes `score` raise `NLIError`.
    """

    rules: list[Rule] = field(default_factory=list)
    default: tuple[float, float, float] = (0.0, 2.0, 0.0)  # neutral
    fail: bool = False
    model_id: str = "fake-nli"
    model_revision: str = "0" * 40
    label_map: dict[str, str] = field(
        default_factory=lambda: {"0": "entailment", "1": "neutral", "2": "contradiction"}
    )
    calls: list[list[NLIPair]] = field(default_factory=list)

    def score(self, pairs: Sequence[NLIPair]) -> list[NLIScore]:
        self.calls.append(list(pairs))
        if self.fail:
            raise NLIError("fake failure")
        out = []
        for pair in pairs:
            logits = next((r for r in (rule(pair) for rule in self.rules) if r), self.default)
            out.append(NLIScore(logits))
        return out


ENTAIL: tuple[float, float, float] = (4.0, 0.0, -2.0)
CONTRADICT: tuple[float, float, float] = (-2.0, 0.0, 4.0)


def when(premise_has: str, hypothesis_has: str, logits: tuple[float, float, float]) -> Rule:
    """A FakeNLI rule: both substrings present (case-insensitive) -> `logits`."""

    def rule(pair: NLIPair) -> tuple[float, float, float] | None:
        hit = (
            premise_has.lower() in pair.premise.lower()
            and hypothesis_has.lower() in pair.hypothesis.lower()
        )
        return logits if hit else None

    return rule
