"""Stage F units: bounded backoff, retrieval metrics, benchmark file, CLI parsing."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from app.config import REPO_ROOT, get_settings
from app.core.retry import call_with_backoff, is_transient
from app.ingestion.cli import build_parser, main
from app.retrieval.evaluation import RetrievalCase, Target, load_cases, score_case
from app.retrieval.qdrant_store import ChunkStore
from app.retrieval.search import SearchHit

pytestmark = pytest.mark.filterwarnings("ignore:Payload indexes have no effect:UserWarning")
DATASET = REPO_ROOT / "data" / "eval" / "m1_retrieval.jsonl"


# --- bounded backoff -----------------------------------------------------------------------


def unexpected(status: int) -> UnexpectedResponse:
    return UnexpectedResponse(status, "x", b"", None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (ResponseHandlingException(OSError("connection refused")), True),
        (ConnectionError("reset"), True),
        (TimeoutError("slow"), True),
        (unexpected(503), True),
        (unexpected(429), True),
        (unexpected(400), False),  # a bad request never succeeds on retry
        (unexpected(404), False),
        (ValueError("bad input"), False),
    ],
)
def test_transient_classification(exc: Exception, transient: bool) -> None:
    assert is_transient(exc) is transient


def test_backoff_retries_twice_then_raises_with_growing_delays() -> None:
    calls, sleeps = [], []

    def flaky() -> int:
        calls.append(1)
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        call_with_backoff(flaky, what="t", sleep=sleeps.append)
    assert len(calls) == 3 and sleeps == [0.5, 1.0]  # bounded: 1 call + 2 retries


def test_backoff_recovers_and_never_retries_permanent_errors() -> None:
    results = iter([TimeoutError("slow"), 7])

    def once_slow() -> int:
        item = next(results)
        if isinstance(item, Exception):
            raise item
        return item

    sleeps: list[float] = []
    assert call_with_backoff(once_slow, what="t", sleep=sleeps.append) == 7 and sleeps == [0.5]
    calls = []

    def bad() -> None:
        calls.append(1)
        raise ValueError("permanent")

    with pytest.raises(ValueError):
        call_with_backoff(bad, what="t", sleep=sleeps.append)
    assert calls == [1]


def test_store_retries_a_transient_upsert_failure() -> None:
    store = ChunkStore(QdrantClient(":memory:"), "retry_test")
    store.ensure_collection(4)
    real = store.client.upsert
    failures = iter([True, False])

    def sometimes(*args: Any, **kw: Any) -> Any:
        if next(failures):
            raise ResponseHandlingException(OSError("connection reset"))
        return real(*args, **kw)

    store.client.upsert = sometimes  # type: ignore[method-assign]
    store.sleep = lambda _s: None
    from qdrant_client import models as qm

    store.upsert([qm.PointStruct(id=1, vector=[1.0, 0, 0, 0], payload={})])
    assert store.count() == 1


# --- metrics ----------------------------------------------------------------------------------


def payload(key: str, page: int, end: int | None = None, para: str | None = None,
            content: str = "text") -> dict[str, Any]:  # fmt: skip
    return {"document_key": key, "page_number": page, "page_end": end, "paragraph": para,
            "content": content}  # fmt: skip


def hit(rank: int, p: dict[str, Any]) -> SearchHit:
    return SearchHit(rank=rank, score=1.0 / rank, chunk_id=str(rank), payload=p)


def test_target_matching_rules() -> None:
    t = Target(document_key="d", page=37, paragraph="33")
    assert t.matches(payload("d", 37, para="33"))
    assert t.matches(payload("d", 36, 38, para="33"))  # page range covers the target
    assert not t.matches(payload("d", 38, para="33"))
    assert not t.matches(payload("d", 37, para="34"))
    assert not t.matches(payload("other", 37, para="33"))
    form = Target(document_key="f", page=1, text_contains="Dormant Activation")
    assert form.matches(payload("f", 1, content="... DORMANT ACTIVATION (branch visit)"))
    assert not form.matches(payload("f", 1, content="unrelated field labels"))


def test_score_case_recall_hit_and_reciprocal_rank() -> None:
    case = RetrievalCase(
        id="c",
        query="what is q?",
        targets=[Target(document_key="d", page=1), Target(document_key="d", page=9)],
    )
    hits = [hit(1, payload("x", 1)), hit(2, payload("d", 1)), hit(3, payload("d", 1))]
    r = score_case(case, hits, k=5)
    assert (r.recall, r.hit, r.reciprocal_rank) == (0.5, True, 0.5)
    none = score_case(case, [hit(1, payload("x", 1))], k=5)
    assert (none.recall, none.hit, none.reciprocal_rank) == (0.0, False, 0.0)
    # Hits beyond k do not count.
    late = [hit(i, payload("x", 1)) for i in range(1, 6)] + [hit(6, payload("d", 9))]
    assert score_case(case, late, k=5).recall == 0.0


def test_duplicate_case_ids_are_rejected(tmp_path: Path) -> None:
    line = '{"id": "a", "query": "q??", "targets": [{"document_key": "d", "page": 1}]}'
    path = tmp_path / "x.jsonl"
    path.write_text(f"{line}\n{line}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_cases(path)


def test_m1_benchmark_file_is_valid() -> None:
    cases = load_cases(DATASET)
    assert len(cases) >= 15  # docs/12 M1 done criterion
    manifest = yaml.safe_load((REPO_ROOT / "data" / "raw_pdfs" / "manifest.yaml").read_text(
        encoding="utf-8"))  # fmt: skip
    keys = {d["document_key"] for d in manifest}
    for case in cases:
        assert {t.document_key for t in case.targets} <= keys, case.id
        # Benchmark labels never point at context_only material (excluded from search).
        assert "hdfc_netbanking_corporate_application" not in {t.document_key for t in case.targets}


# --- CLI parsing ----------------------------------------------------------------------------------


def test_cli_commands_and_aliases_parse() -> None:
    p = build_parser(get_settings())
    a = p.parse_args(["run", "--document", "x", "--document", "y", "--dry-run"])
    assert (a.command, a.document, a.dry_run, a.force) == ("run", ["x", "y"], True, False)
    assert p.parse_args(["ingest"]).command == "ingest"
    s = p.parse_args(["search", "company documents", "--entity", "COMPANY", "--expand",
                      "--top-k", "3", "--bank", "RBI"])  # fmt: skip
    assert (s.query, s.entity, s.expand, s.top_k, s.bank) == (
        "company documents", ["COMPANY"], True, 3, ["RBI"])  # fmt: skip
    assert p.parse_args(["index", "--from-db"]).from_db is True
    assert p.parse_args(["retry"]).document == []


@pytest.mark.parametrize(
    "argv",
    [
        ["reconcile", "--delete-unknown"],  # needs --repair
        ["index", "--from-db", "--prune"],  # prune is in-memory mode only
        ["search", "q", "--top-k", "0"],
        ["search", "q", "--bank", "SBI"],  # only RBI and HDFC sources exist
    ],
)
def test_cli_rejects_invalid_combinations(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
