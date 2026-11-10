"""Application settings loaded from env vars.

Precedence (highest first): process env vars, repo-root `.env`, `infra/.env.example`.
The example file supplies non-secret defaults; real keys belong only in `.env`.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Later files override earlier ones.
        env_file=(REPO_ROOT / "infra" / ".env.example", REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- App ---
    APP_ENV: str = "dev"
    FRONTEND_ORIGIN: str = "http://localhost:5173"
    # --- Postgres ---
    # Connection URLs have no in-code default: they come from .env / infra/.env.example.
    DATABASE_URL: SecretStr
    # --- Redis ---
    REDIS_URL: SecretStr
    # --- Qdrant ---
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_API_KEY: SecretStr | None = None
    QDRANT_COLLECTION: str = "policy_chunks"
    # --- LLMs (IDs come from env; verify at implementation time) ---
    GROQ_API_KEY: SecretStr | None = None
    PRIMARY_LLM_MODEL: str | None = None
    FALLBACK_LLM_MODEL: str | None = None
    PROMPT_GUARD_MODEL: str | None = None
    GEMINI_API_KEY: SecretStr | None = None
    MULTIMODAL_MODEL: str | None = None
    # --- Local models ---
    EMBEDDING_MODEL: str = "BAAI/bge-small-en-v1.5"
    # Hugging Face commit to load; None = latest. Pinned in .env.example for reproducibility.
    EMBEDDING_MODEL_REVISION: str | None = None
    NLI_MODEL: str | None = None
    # --- Corpus / ingestion (relative paths resolve against the repo root) ---
    CORPUS_DIR: Path = Path("data/raw_pdfs")
    CORPUS_MANIFEST: Path = Path("data/raw_pdfs/manifest.yaml")
    # Tesseract for PyMuPDF OCR (resolution order in app/ingestion/ocr.py).
    # TESSERACT_CMD: the tesseract executable; None = PATH, then the standard install location.
    # TESSDATA_DIR: language data; None = `tessdata` next to the executable, else the
    # directory the executable reports (Linux packages keep it under /usr/share).
    TESSERACT_CMD: Path | None = None
    TESSDATA_DIR: Path | None = None
    # OCR results cache (gitignored under data/processed/); blank disables caching.
    OCR_CACHE_DIR: Path | None = Path("data/processed/ocr_cache")
    # OCR evidence policy (docs/15): a chunk's OCR text is reliable only if its pages are
    # `ok` and its mean line confidence (0-1) reaches this. Manifest entries may override.
    OCR_RELIABLE_MIN_CONFIDENCE: float = Field(default=0.80, ge=0, le=1)
    # Embed uncertain OCR chunks (retrievable, flagged, never MANDATORY evidence)?
    OCR_EMBED_UNCERTAIN: bool = True
    # --- Retrieval (M2) ---
    # Default mode for `search` / `evaluate` without --mode. M2 A3 selected C6 (hybrid, BM25
    # stopwords, RRF k=20) on the development set (docs/milestones/M2_A3_SELECTION.md); its
    # gain did not generalize on the held-out set, so the operational default is dense again
    # (docs/milestones/M2_RETRIEVAL_OPERATIONAL_DECISION.md). C6 stays selectable:
    # `--mode hybrid` with the RRF_K / BM25_STOPWORDS values below is exactly C6.
    RETRIEVAL_MODE: Literal["dense", "bm25", "hybrid"] = "dense"
    RRF_K: int = Field(default=20, ge=1)  # hybrid fusion constant (C6: 20)
    RETRIEVAL_CANDIDATES: int = Field(default=50, ge=1, le=1000)  # per retriever, hybrid
    # BM25 tokenizer variant (M2 A3): False = plain v1; True = plain v1 + the registered
    # Lucene English stopword list (removes "no"/"not"; docs/04). C6: True. Also applies to
    # `--mode bm25` (C2 rather than C1); set False for the plain tokenizer.
    BM25_STOPWORDS: bool = True
    # --- Tracing ---
    LANGSMITH_TRACING: bool = False
    LANGSMITH_API_KEY: SecretStr | None = None
    LANGSMITH_PROJECT: str = "kyc-engine"
    # --- Limits ---
    MAX_REGENERATIONS: int = Field(default=2, ge=0, le=2)
    MAX_UPLOAD_MB: int = Field(default=20, gt=0)
    RATE_LIMIT_GROQ_RPM: int = Field(default=20, gt=0)
    RATE_LIMIT_GEMINI_RPM: int = Field(default=10, gt=0)
    # --- Health checks ---
    HEALTH_CHECK_TIMEOUT_S: float = Field(default=2.0, gt=0)

    @field_validator(
        "QDRANT_API_KEY",
        "GROQ_API_KEY",
        "GEMINI_API_KEY",
        "LANGSMITH_API_KEY",
        "PRIMARY_LLM_MODEL",
        "FALLBACK_LLM_MODEL",
        "PROMPT_GUARD_MODEL",
        "MULTIMODAL_MODEL",
        "NLI_MODEL",
        "EMBEDDING_MODEL_REVISION",
        "TESSERACT_CMD",
        "TESSDATA_DIR",
        "OCR_CACHE_DIR",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        # Empty values in .env mean "not configured", never an empty key or model ID.
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator(
        "CORPUS_DIR", "CORPUS_MANIFEST", "TESSERACT_CMD", "TESSDATA_DIR", "OCR_CACHE_DIR"
    )
    @classmethod
    def _repo_relative(cls, v: Path | None) -> Path | None:
        # Same rule as M0's env files: paths work from any working directory.
        if v is None or v.is_absolute():
            return v
        return REPO_ROOT / v


@lru_cache
def get_settings() -> Settings:
    return Settings()
