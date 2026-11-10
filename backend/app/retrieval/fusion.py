"""Reciprocal Rank Fusion (RRF) of ranked chunk lists (M2 A2, docs/04).

`score(chunk) = sum over rankings containing it of 1 / (k + rank)`, ranks 1-based.
Fusion uses ranks only, never the rankers' raw scores (dense cosine and BM25 scores are
not comparable). Chunks are identified by `chunk_id` alone.

Ordering is fully deterministic: fused score descending, then the chunk's best rank in
any single ranking ascending, then `chunk_id` ascending.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

DEFAULT_RRF_K = 60  # the conventional constant; not tuned (M2_PLAN section 6)


@dataclass(frozen=True)
class FusedCandidate:
    chunk_id: str
    score: float  # the RRF fused score
    ranks: dict[str, int]  # ranking name -> 1-based rank, for rankings containing the chunk

    @property
    def best_rank(self) -> int:
        return min(self.ranks.values())


def rrf_fuse(
    rankings: Mapping[str, Sequence[str]], *, k: int = DEFAULT_RRF_K, top_k: int | None = None
) -> list[FusedCandidate]:
    """Fuse named rankings of chunk ids (best first) into one RRF ranking."""
    if k < 1:
        raise ValueError("RRF k must be >= 1")
    ranks: dict[str, dict[str, int]] = {}
    for name, ids in rankings.items():
        if len(set(ids)) != len(ids):
            raise ValueError(f"ranking {name!r} lists a chunk more than once")
        for rank, cid in enumerate(ids, start=1):
            ranks.setdefault(cid, {})[name] = rank
    fused = [
        FusedCandidate(cid, sum(1.0 / (k + r) for r in by.values()), dict(by))
        for cid, by in ranks.items()
    ]
    fused.sort(key=lambda c: (-c.score, c.best_rank, c.chunk_id))
    return fused if top_k is None else fused[:top_k]
