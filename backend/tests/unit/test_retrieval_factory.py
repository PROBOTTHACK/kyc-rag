"""M3 S2: the public `retriever_from_settings` factory (moved from the CLI unchanged).

Byte-identical CLI output before and after the move was checked separately on the live
stores (S2 handoff); these tests pin the factory's wiring and laziness.
"""

from typing import Any

import pytest

import app.ingestion.cli as cli
import app.retrieval.factory as factory
from app.config import REPO_ROOT, Settings
from app.retrieval.factory import retriever_from_settings
from app.retrieval.retrieve import Retriever

EXAMPLE_ENV = REPO_ROOT / "infra" / ".env.example"


def settings(**kw: Any) -> Settings:
    return Settings(_env_file=EXAMPLE_ENV, **kw)  # type: ignore[call-arg]


@pytest.fixture
def loads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Record every dependency the factory builds, without loading anything real."""
    seen: list[tuple[str, Any]] = []

    def rec(name: str) -> Any:
        def build(*args: Any, **kw: Any) -> object:
            seen.append((name, (args, kw)))
            return object()

        return build

    monkeypatch.setattr(factory, "embedder_from_settings", rec("embedder"))
    monkeypatch.setattr(factory, "store_from_settings", rec("store"))
    monkeypatch.setattr(factory, "KeywordIndexManager", rec("keyword"))
    monkeypatch.setattr(factory, "reranker_from_settings", rec("reranker"))
    return seen


def test_configuration_comes_from_settings(loads: list[Any]) -> None:
    s = settings(RRF_K=7, RETRIEVAL_CANDIDATES=33, RERANK_CANDIDATES=11)
    r = retriever_from_settings(s, sessions=object())  # type: ignore[arg-type]
    assert isinstance(r, Retriever)
    assert (r.rrf_k, r.candidates, r.rerank_candidates) == (7, 33, 11)


def test_nothing_is_loaded_until_a_retriever_needs_it(loads: list[Any]) -> None:
    retriever_from_settings(settings(), sessions=object())  # type: ignore[arg-type]
    assert loads == []


def test_each_dependency_is_built_from_settings_on_demand(loads: list[Any]) -> None:
    s = settings(BM25_STOPWORDS=False)
    sessions: Any = object()
    r = retriever_from_settings(s, sessions=sessions)
    r._embedder_factory()
    r._store_factory()
    r._keyword_factory()
    assert r._reranker_factory is not None
    r._reranker_factory()
    names = [n for n, _ in loads]
    assert names == ["embedder", "store", "keyword", "reranker"]
    keyword_args = loads[2][1]
    assert keyword_args == ((sessions,), {"stopwords": False})


def test_supplied_embedder_and_store_replace_the_settings_ones(loads: list[Any]) -> None:
    embedder: Any = object()
    store: Any = object()
    r = retriever_from_settings(settings(), sessions=object(), embedder=embedder, store=store)  # type: ignore[arg-type]
    assert r._embedder_factory() is embedder and r._store_factory() is store
    assert loads == []


def test_the_cli_uses_the_public_factory_and_no_private_copy_remains() -> None:
    assert cli.retriever_from_settings is retriever_from_settings
    assert not hasattr(cli, "_retriever")
