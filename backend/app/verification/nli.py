"""NLI scorer contract, a deterministic fake, and the local cross-encoder (M4 S2/S4; M4_PLAN §F).

Semantics every scorer must follow:
- **Ordering:** each pair is `(premise, hypothesis)` = (evidence window, claim), the MNLI
  convention. `score` receives `NLIPair` objects, so the order cannot be swapped by
  accident, and `CrossEncoderNLI` passes them to the model in exactly that order.
- **Labels:** results are returned in the canonical order `NLI_LABELS` (entailment,
  neutral, contradiction), whatever the model's own `id2label` order. The real scorer
  builds the permutation from the label map it reads from the model config at load time,
  never from a hard-coded index (the S0 candidates do not share one order).
- **Logits vs probabilities:** a scorer returns raw logits (the cross-encoder's activation
  is fixed to identity); `softmax` turns them into probabilities here. Both are stored.
  They are uncalibrated model signals compared with dev-selected thresholds, never a
  confidence and never a legal or regulatory certainty.
- **Over-long pairs (M4 S4):** a pair that does not fit the model window is never silently
  truncated. `score` returns `None` for it, and the caller records `window_too_long`, so
  the claim is `not_verified`.
- **Failure:** a scorer raises `NLIError`; the caller marks the affected claims
  `not_verified` (never "supported", never "not required").

Nothing here downloads or loads a model at import time. `CrossEncoderNLI` loads lazily,
on CPU, at a pinned revision, and checks its label map and a fixed sanity triplet before
it scores anything.
"""

import math
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from app.config import Settings
from app.graph.state import NLI_LABELS

if TYPE_CHECKING:  # heavy import (torch); loaded lazily at first use
    from sentence_transformers import CrossEncoder

MAX_LENGTH = 512  # tokens per (premise, hypothesis) pair: the DeBERTa-v3 window
DEVICE = "cpu"
_REVISION = re.compile(r"^[0-9a-f]{40}$")


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

    @property
    def label(self) -> str:
        """The argmax label (used only by the NLI-only ablation, never by the verifier)."""
        return NLI_LABELS[max(range(3), key=lambda i: self.logits[i])]


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

    def score(self, pairs: Sequence[NLIPair]) -> list[NLIScore | None]: ...


Rule = Callable[[NLIPair], tuple[float, float, float] | None]


@dataclass
class FakeNLI:
    """A deterministic scripted scorer for tests and the S2 decision table.

    `rules` are tried in order; the first that returns logits wins, else `default`
    (neutral). Every call is recorded (`calls`) so tests can assert pair ordering and
    budgets. `fail` makes `score` raise `NLIError`; `too_long` marks pairs whose premise
    contains it as not fitting the window (`None`).
    """

    rules: list[Rule] = field(default_factory=list)
    default: tuple[float, float, float] = (0.0, 2.0, 0.0)  # neutral
    fail: bool = False
    too_long: str | None = None
    model_id: str = "fake-nli"
    model_revision: str = "0" * 40
    label_map: dict[str, str] = field(
        default_factory=lambda: {"0": "entailment", "1": "neutral", "2": "contradiction"}
    )
    calls: list[list[NLIPair]] = field(default_factory=list)

    def score(self, pairs: Sequence[NLIPair]) -> list[NLIScore | None]:
        self.calls.append(list(pairs))
        if self.fail:
            raise NLIError("fake failure")
        out: list[NLIScore | None] = []
        for pair in pairs:
            if self.too_long is not None and self.too_long in pair.premise:
                out.append(None)
                continue
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


# --- the local cross-encoder (M4 S4) -------------------------------------------------------

# Fixed synthetic pairs checked at load: an obvious entailment, contradiction and neutral
# case, outside the KYC domain and outside every dataset. A model (or a label map) that
# gets one of them wrong is refused before it scores any evidence.
SANITY_TRIPLET: tuple[tuple[NLIPair, str], ...] = (
    (NLIPair("A man is playing a guitar on a stage.", "A man is playing an instrument."),
     "entailment"),
    (NLIPair("A man is playing a guitar on a stage.", "Nobody is playing any instrument."),
     "contradiction"),
    (NLIPair("A man is playing a guitar on a stage.", "The man is a famous singer."),
     "neutral"),
)  # fmt: skip


def canonical_order(id2label: dict[Any, str]) -> tuple[int, int, int]:
    """Model output indices of (entailment, neutral, contradiction). Fails closed unless
    the model's labels are exactly those three (case-insensitive)."""
    names = {int(k): str(v).strip().lower() for k, v in id2label.items()}
    if sorted(names) != [0, 1, 2] or sorted(names.values()) != sorted(NLI_LABELS):
        raise NLIError(f"unexpected NLI label map: {id2label!r}")
    index = {v: k for k, v in names.items()}
    a, b, c = (index[label] for label in NLI_LABELS)
    return a, b, c


class CrossEncoderNLI:
    """A sentence-transformers `CrossEncoder` NLI checkpoint at a pinned revision, on CPU,
    loaded lazily, returning raw logits in canonical order."""

    def __init__(
        self,
        model_id: str,
        revision: str,
        *,
        batch_size: int = 16,
        max_length: int = MAX_LENGTH,
    ) -> None:
        if not model_id:
            raise NLIError("an NLI model id is required")
        if not _REVISION.fullmatch(revision or ""):
            raise NLIError("the NLI model must be pinned to a 40-hex Hugging Face commit")
        if batch_size < 1 or not 16 <= max_length <= MAX_LENGTH:
            raise NLIError("invalid batch size or max length")
        self.model_id = model_id
        self.model_revision = revision
        self.batch_size = batch_size
        self.max_length = max_length
        self.label_map: dict[str, str] = {}
        self.sanity: list[dict[str, Any]] = []
        self.load_ms = 0.0
        self._order: tuple[int, int, int] | None = None
        self._model: CrossEncoder | None = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _construct(self) -> "CrossEncoder":
        import torch
        from sentence_transformers import CrossEncoder

        model: CrossEncoder = CrossEncoder(
            self.model_id,
            revision=self.model_revision,
            max_length=self.max_length,
            device=DEVICE,
            activation_fn=torch.nn.Identity(),  # raw logits, never squashed
        )
        return model

    def load(self) -> None:
        """Load once, read the label map, and run the sanity triplet (fails closed)."""
        if self._model is not None:
            return
        started = time.perf_counter()
        try:
            model = self._construct()
            inner: Any = model.model
            # a missing map becomes {} and fails closed in canonical_order
            id2label = dict(getattr(getattr(inner, "config", None), "id2label", None) or {})
        except NLIError:
            raise
        except Exception as exc:
            raise NLIError(
                f"cannot load NLI model {self.model_id}@{self.model_revision}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        order = canonical_order(id2label)
        self.label_map = {str(k): str(v) for k, v in sorted(id2label.items())}
        self._model, self._order = model, order
        try:
            got = self._predict([p for p, _ in SANITY_TRIPLET])
        except NLIError:
            self._model = None
            raise
        self.sanity = [
            {"premise": p.premise, "hypothesis": p.hypothesis, "expected": want,
             "predicted": s.label, "logits": list(s.logits)}
            for (p, want), s in zip(SANITY_TRIPLET, got, strict=True)
        ]  # fmt: skip
        if any(row["predicted"] != row["expected"] for row in self.sanity):
            self._model = None
            raise NLIError(f"NLI label check failed for {self.model_id}: {self.sanity}")
        self.load_ms = (time.perf_counter() - started) * 1000

    def fits(self, pair: NLIPair) -> bool:
        """Whether the pair fits the window without truncation (special tokens included)."""
        if self._model is None:
            raise NLIError("model not loaded")
        encoded = self._model.tokenizer(pair.premise, pair.hypothesis, truncation=False)
        return len(encoded["input_ids"]) <= self.max_length

    def _predict(self, pairs: Sequence[NLIPair]) -> list[NLIScore]:
        if self._model is None or self._order is None:
            raise NLIError("model not loaded")
        try:
            raw: Any = self._model.predict(
                [(p.premise, p.hypothesis) for p in pairs],  # (premise, hypothesis) order
                batch_size=self.batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
        except Exception as exc:
            raise NLIError(f"NLI inference failed: {type(exc).__name__}: {exc}") from exc
        a, b, c = self._order
        out = []
        for row in raw:
            values = [float(x) for x in row]
            if len(values) != len(NLI_LABELS):
                raise NLIError("the model did not return three logits")
            out.append(NLIScore((values[a], values[b], values[c])))
        return out

    def score(self, pairs: Sequence[NLIPair]) -> list[NLIScore | None]:
        self.load()
        if not pairs:
            return []
        fit = [self.fits(p) for p in pairs]
        scored = iter(self._predict([p for p, ok in zip(pairs, fit, strict=True) if ok]))
        return [next(scored) if ok else None for ok in fit]


def nli_from_settings(settings: Settings) -> CrossEncoderNLI | None:
    """The configured scorer, or None when no NLI model is configured (verification is then
    `not_configured`, never "supported"). Nothing is loaded here."""
    if settings.NLI_MODEL is None:
        return None
    if settings.NLI_MODEL_REVISION is None:
        raise NLIError("NLI_MODEL is set but NLI_MODEL_REVISION is not: the model must be pinned")
    return CrossEncoderNLI(
        settings.NLI_MODEL, settings.NLI_MODEL_REVISION, batch_size=settings.NLI_BATCH_SIZE
    )
