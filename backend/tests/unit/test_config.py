import pytest
from pydantic import ValidationError

from app.config import REPO_ROOT, Settings


def test_reads_infra_env_example_defaults() -> None:
    assert (REPO_ROOT / "infra" / ".env.example").is_file()
    s = Settings(_env_file=REPO_ROOT / "infra" / ".env.example")  # type: ignore[call-arg]
    assert s.QDRANT_COLLECTION == "policy_chunks"
    assert s.EMBEDDING_MODEL == "BAAI/bge-small-en-v1.5"
    assert s.MAX_REGENERATIONS == 2


def test_blank_values_become_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("MULTIMODAL_MODEL", "   ")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.GROQ_API_KEY is None
    assert s.MULTIMODAL_MODEL is None


def test_env_var_overrides_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QDRANT_COLLECTION", "override")
    s = Settings(_env_file=REPO_ROOT / "infra" / ".env.example")  # type: ignore[call-arg]
    assert s.QDRANT_COLLECTION == "override"


def test_secrets_are_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk_synthetic_test_value")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:synthetic_pw@h:5432/d")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    dumped = repr(s) + str(s.model_dump())
    assert "gsk_synthetic_test_value" not in dumped
    assert "synthetic_pw" not in dumped


def test_corpus_paths_resolve_against_repo_root() -> None:
    s = Settings(_env_file=REPO_ROOT / "infra" / ".env.example")  # type: ignore[call-arg]
    assert s.CORPUS_DIR == REPO_ROOT / "data" / "raw_pdfs"
    assert s.CORPUS_MANIFEST == REPO_ROOT / "data" / "raw_pdfs" / "manifest.yaml"
    assert s.TESSDATA_DIR is None  # blank in .env.example = auto-detect


def test_absolute_corpus_path_is_kept(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    monkeypatch.setenv("CORPUS_DIR", str(tmp_path))
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert str(s.CORPUS_DIR) == str(tmp_path)


def test_regeneration_limit_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_REGENERATIONS", "5")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]
