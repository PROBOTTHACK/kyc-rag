"""K8 (A7.5): dense search ranks tied scores deterministically (score desc, then chunk id),
whatever order Qdrant returns them in, including a tie that straddles the k-th place.

The real case: two chunks with identical text (NRI opening form p.3 and NRO conversion
p.3) score exactly 0.6943718 for golden query m1-03; their order followed Qdrant's index
layout. The store is faked here; the real-corpus check is test_dense_golden."""

from itertools import permutations
from typing import Any

from qdrant_client import models as qm

from app.retrieval.search import MAX_TIE_REFETCHES, TIE_MARGIN, search


class FakeEmbedder:
    def embed_query(self, text: str) -> list[float]:
        return [0.0, 1.0]


class FakeStore:
    """Returns the first `limit` points in the given order (Qdrant sorts by score only)."""

    def __init__(self, points: list[tuple[str, float]]) -> None:
        self.points = [qm.ScoredPoint(id=i, version=0, score=s, payload={}) for i, s in points]
        self.limits: list[int] = []

    def search(self, vector: Any, limit: int, flt: Any = None) -> list[qm.ScoredPoint]:
        self.limits.append(limit)
        return self.points[:limit]


A = "726f715a-4e66-5743-be12-3b3d5637a3e1"
B = "b5497a8b-f838-525c-b14b-1b13c6bfb060"
HEAD = [(f"00000000-0000-0000-0000-00000000000{n}", 0.9 - n / 100) for n in range(1, 5)]
TAIL = [("ffffffff-0000-0000-0000-000000000001", 0.5)]


def ids(store: FakeStore, top_k: int = 5) -> list[str]:
    hits = search("q", FakeEmbedder(), store, top_k=top_k)  # type: ignore[arg-type]
    return [h.chunk_id for h in hits]


def test_a_tie_straddling_the_kth_place_is_ranked_by_chunk_id_in_any_store_order() -> None:
    # The m1-03 shape: ranks 1-4 distinct, A and B tied for rank 5.
    for tied in permutations([(A, 0.6943718), (B, 0.6943718)]):
        store = FakeStore(HEAD + list(tied) + TAIL)
        assert ids(store) == [i for i, _ in HEAD] + [A]
        assert store.limits == [5 + TIE_MARGIN]


def test_ties_inside_the_top_k_are_ordered_by_chunk_id() -> None:
    tied = [("c" * 8 + A[8:], 0.8), (B, 0.8), (A, 0.8)]
    assert ids(FakeStore(tied + TAIL), top_k=3) == [A, B, "c" * 8 + A[8:]]


def test_distinct_scores_keep_qdrants_order() -> None:
    assert ids(FakeStore(HEAD + TAIL)) == [i for i, _ in HEAD + TAIL]


def test_a_tie_reaching_the_last_candidate_refetches_a_bounded_number_of_times() -> None:
    many = [(f"{n:08x}-0000-0000-0000-000000000000", 0.7) for n in range(200, 0, -1)]
    store = FakeStore(many)
    got = ids(store)
    assert len(store.limits) == 1 + MAX_TIE_REFETCHES  # never unbounded
    assert store.limits == [10, 20, 40]
    fetched = sorted(i for i, _ in many[:40])
    assert got == fetched[:5]  # deterministic within what was fetched


def test_fewer_points_than_the_limit_need_no_refetch() -> None:
    store = FakeStore([(A, 0.7), (B, 0.7)])
    assert ids(store) == [A, B]
    assert store.limits == [5 + TIE_MARGIN]
