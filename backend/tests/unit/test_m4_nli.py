"""M4 S4.1: the local NLI scorer contract, tested with a scripted fake CrossEncoder (no model
is downloaded or loaded). Covers the label-map permutation, fail-closed label checks, the
load sanity triplet, (premise, hypothesis) ordering, over-long pairs, pinning and settings."""

import itertools
from typing import Any

import pytest

from app.config import Settings
from app.graph.state import NLI_LABELS
from app.verification.evaluation import CachedScorer
from app.verification.nli import (
    SANITY_TRIPLET,
    CrossEncoderNLI,
    FakeNLI,
    NLIError,
    NLIPair,
    canonical_order,
    nli_from_settings,
    softmax,
)

REV = "a" * 40


def intended(pair: tuple[str, str]) -> str:
    """The fake model's 'opinion' of a pair, in canonical label names."""
    premise, hypothesis = pair
    if "Nobody" in hypothesis or "never" in hypothesis:
        return "contradiction"
    if "famous" in hypothesis or "unrelated" in hypothesis:
        return "neutral"
    return "entailment"


class FakeTokenizer:
    def __call__(self, premise: str, hypothesis: str, truncation: bool) -> dict[str, list[int]]:
        assert truncation is False  # the scorer never asks for silent truncation
        return {"input_ids": [0] * (len((premise + " " + hypothesis).split()) + 3)}


class FakeCrossEncoder:
    """Mimics sentence_transformers.CrossEncoder: config.id2label, tokenizer, predict."""

    def __init__(self, id2label: dict[Any, str], opinion: Any = intended) -> None:
        self.model = type("M", (), {"config": type("C", (), {"id2label": id2label})()})()
        self.tokenizer = FakeTokenizer()
        self.opinion = opinion
        self.seen: list[tuple[str, str]] = []
        self.batch_sizes: list[int] = []
        names = {int(k): v.lower() for k, v in id2label.items()}
        self.index = {v: k for k, v in names.items()}

    def predict(self, pairs: list[tuple[str, str]], **kw: Any) -> list[list[float]]:
        self.seen += pairs
        self.batch_sizes.append(kw["batch_size"])
        assert kw["show_progress_bar"] is False
        rows = []
        for pair in pairs:
            row = [0.0, 0.0, 0.0]
            row[self.index[self.opinion(pair)]] = 5.0  # a confident logit in the MODEL's order
            rows.append(row)
        return rows


def scorer(id2label: dict[Any, str] | None = None, **kw: Any) -> tuple[CrossEncoderNLI, Any]:
    default = {0: "contradiction", 1: "entailment", 2: "neutral"}
    fake = FakeCrossEncoder(default if id2label is None else id2label, **kw)
    s = CrossEncoderNLI("fake/nli", REV, batch_size=4, max_length=64)
    s._construct = lambda: fake  # type: ignore[method-assign,assignment,return-value]
    return s, fake


# --- label maps -------------------------------------------------------------------------------


@pytest.mark.parametrize("order", list(itertools.permutations(NLI_LABELS)))
def test_every_model_label_order_maps_to_canonical_order(order: tuple[str, ...]) -> None:
    s, _ = scorer(dict(enumerate(order)))
    [ent, con, neu] = s.score(
        [NLIPair("p", "it is so"), NLIPair("p", "Nobody did"), NLIPair("p", "unrelated")]
    )
    assert ent is not None and con is not None and neu is not None
    assert (ent.label, con.label, neu.label) == ("entailment", "contradiction", "neutral")
    assert ent.logits == (5.0, 0.0, 0.0) and con.logits == (0.0, 0.0, 5.0)
    assert s.label_map == {str(i): label for i, label in enumerate(order)}


def test_label_names_are_case_insensitive() -> None:
    assert canonical_order({0: "CONTRADICTION", 1: "Entailment", 2: "neutral"}) == (1, 2, 0)


@pytest.mark.parametrize(
    "id2label",
    [
        {0: "entailment", 1: "not_entailment"},
        {0: "yes", 1: "maybe", 2: "no"},
        {0: "entailment", 1: "neutral", 2: "contradiction", 3: "other"},
        {1: "entailment", 2: "neutral", 3: "contradiction"},
        {0: "entailment", 1: "entailment", 2: "contradiction"},
        {},
    ],
)
def test_an_unexpected_label_map_fails_closed(id2label: dict[Any, str]) -> None:
    s, _ = scorer(id2label)
    with pytest.raises(NLIError, match="label map"):
        s.load()
    assert not s.loaded


# --- load checks ------------------------------------------------------------------------------


def test_the_sanity_triplet_runs_at_load_and_is_recorded() -> None:
    s, fake = scorer()
    s.load()
    assert [row["expected"] for row in s.sanity] == ["entailment", "contradiction", "neutral"]
    assert all(row["predicted"] == row["expected"] for row in s.sanity)
    assert fake.seen == [(p.premise, p.hypothesis) for p, _ in SANITY_TRIPLET]
    s.load()  # idempotent: no second load, no second triplet
    assert len(fake.seen) == 3


def test_a_model_that_fails_the_sanity_triplet_is_refused() -> None:
    s, _ = scorer(opinion=lambda pair: "entailment")  # calls everything entailment
    with pytest.raises(NLIError, match="label check failed"):
        s.load()
    assert not s.loaded


def test_a_swapped_label_map_is_caught_by_the_sanity_triplet() -> None:
    """A config whose labels are named wrongly (contradiction/entailment swapped) passes the
    name check but fails the behavioural check."""
    s, fake = scorer({0: "entailment", 1: "neutral", 2: "contradiction"})
    fake.index = {"contradiction": 0, "neutral": 1, "entailment": 2}  # the model's real order
    with pytest.raises(NLIError, match="label check failed"):
        s.load()


def test_a_load_failure_is_an_nli_error() -> None:
    s = CrossEncoderNLI("fake/nli", REV)

    def boom() -> Any:
        raise OSError("no network")

    s._construct = boom  # type: ignore[method-assign]
    with pytest.raises(NLIError, match="cannot load"):
        s.load()


@pytest.mark.parametrize(
    ("model_id", "revision"),
    [("", REV), ("x/y", ""), ("x/y", "main"), ("x/y", "A" * 40), ("x/y", "a" * 39)],
)
def test_the_model_must_be_pinned(model_id: str, revision: str) -> None:
    with pytest.raises(NLIError):
        CrossEncoderNLI(model_id, revision)


# --- scoring ----------------------------------------------------------------------------------


def test_pairs_reach_the_model_as_premise_then_hypothesis_in_batches() -> None:
    s, fake = scorer()
    s.load()
    fake.seen.clear()
    s.score([NLIPair("evidence window", "the claim")])
    assert fake.seen == [("evidence window", "the claim")]
    assert fake.batch_sizes[-1] == 4


def test_an_over_long_pair_is_never_truncated_and_scores_none() -> None:
    s, fake = scorer()
    long_premise = "word " * 80  # > max_length=64 fake tokens
    out = s.score([NLIPair("short premise", "it is so"), NLIPair(long_premise, "it is so")])
    assert out[0] is not None and out[1] is None
    assert all(len(p[0].split()) < 80 for p in fake.seen)  # the long pair was not sent


def test_scores_are_logits_with_softmax_probabilities() -> None:
    s, _ = scorer()
    [score] = s.score([NLIPair("p", "it is so")])
    assert score is not None
    assert score.probs == softmax(score.logits)
    assert abs(sum(score.probs) - 1) < 1e-12 and score.entailment > 0.98


def test_an_empty_batch_loads_nothing_twice_and_returns_nothing() -> None:
    s, _ = scorer()
    assert s.score([]) == []


def test_the_cache_scores_each_distinct_pair_once() -> None:
    fake = FakeNLI()
    cached = CachedScorer(fake)
    pairs = [NLIPair("a", "b"), NLIPair("a", "b"), NLIPair("c", "d")]
    first = cached.score(pairs)
    second = cached.score(pairs[::-1])
    assert len(fake.calls) == 1 and len(fake.calls[0]) == 2
    assert first[0] == first[1] and second[0] == first[2]
    assert (cached.model_id, cached.model_revision) == (fake.model_id, fake.model_revision)


# --- settings ---------------------------------------------------------------------------------


def test_no_configured_model_means_no_scorer() -> None:
    assert nli_from_settings(Settings(NLI_MODEL=None)) is None


def test_a_configured_model_must_carry_its_revision() -> None:
    with pytest.raises(NLIError, match="pinned"):
        nli_from_settings(Settings(NLI_MODEL="x/y", NLI_MODEL_REVISION=None))


def test_the_factory_builds_but_never_loads() -> None:
    s = nli_from_settings(Settings(NLI_MODEL="x/y", NLI_MODEL_REVISION=REV, NLI_BATCH_SIZE=8))
    assert s is not None and not s.loaded and s.batch_size == 8
