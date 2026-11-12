"""M4 S4: one pinned NLI checkpoint, really loaded (marker `nli`; `make test-nli`).

Skipped unless a candidate is already in the local Hugging Face cache: the test runs
OFFLINE and never downloads (downloads are the explicit S4.2 step, decision D12).
"""

import pytest

from app.verification.nli import CrossEncoderNLI, NLIPair
from app.verification.selection import CANDIDATES

pytestmark = pytest.mark.nli


def _cached() -> list:  # type: ignore[type-arg]
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:  # pragma: no cover
        return []
    return [
        c for c in CANDIDATES
        if isinstance(try_to_load_from_cache(c.model_id, "config.json", revision=c.revision), str)
    ]  # fmt: skip


@pytest.mark.parametrize("candidate", _cached() or [None], ids=lambda c: c.id if c else "none")
def test_a_cached_candidate_loads_checks_and_scores(candidate, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    if candidate is None:
        pytest.skip("no pinned NLI checkpoint in the local Hugging Face cache (S4.2 downloads)")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    model = CrossEncoderNLI(candidate.model_id, candidate.revision, batch_size=4)
    model.load()  # label map + sanity triplet, fail closed
    assert all(row["predicted"] == row["expected"] for row in model.sanity)
    out = model.score(
        [
            NLIPair(
                "The bank shall obtain the trust deed of a trust.", "A trust deed is obtained."
            ),
            NLIPair(
                "The bank shall obtain the trust deed of a trust.",
                "The bank never obtains the trust deed of a trust.",
            ),
            NLIPair("word " * 600, "anything"),  # over the 512-token window: never truncated
        ]
    )
    assert out[0] is not None and out[0].label == "entailment"
    assert out[1] is not None and out[1].label == "contradiction"
    assert out[2] is None
