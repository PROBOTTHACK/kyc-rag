"""Application settings loaded from env vars.

Precedence (highest first): process env vars, repo-root `.env`, `infra/.env.example`.
The example file supplies non-secret defaults; real keys belong only in `.env`.
"""

import datetime as dt
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
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
    # Groq live calls are OFF unless explicitly enabled AND the data-use terms were
    # verified (date recorded in docs/02, "Groq API verification"). M3 sends Groq only
    # screened, synthetic queries and public corpus text (M3_PLAN §6).
    GROQ_LIVE_ENABLED: bool = False
    GROQ_DATA_TERMS_VERIFIED_ON: dt.date | None = None
    GROQ_TIMEOUT_S: float = Field(default=30.0, gt=0, le=60)
    # Project default (CLAUDE.md): at most 2 retries, i.e. 3 attempts per model.
    GROQ_MAX_RETRIES: int = Field(default=2, ge=0, le=2)
    GROQ_BACKOFF_INITIAL_S: float = Field(default=1.0, gt=0, le=30)
    GROQ_BACKOFF_CAP_S: float = Field(default=8.0, gt=0, le=30)
    GEMINI_API_KEY: SecretStr | None = None
    MULTIMODAL_MODEL: str | None = None
    # Gemini live calls are OFF unless explicitly enabled AND the data-use terms were
    # verified (date recorded in docs/02). Only approved public blank forms are sent.
    GEMINI_LIVE_ENABLED: bool = False
    GEMINI_DATA_TERMS_VERIFIED_ON: dt.date | None = None
    GEMINI_TIMEOUT_S: float = Field(default=60.0, gt=0, le=60)
    # Owner-approved override of the project default of 2 (Gemini transport only):
    # 5 attempts per model, waits 2 -> 4 -> 8 -> 16 s, each capped.
    GEMINI_MAX_RETRIES: int = Field(default=4, ge=0, le=4)
    GEMINI_BACKOFF_INITIAL_S: float = Field(default=2.0, gt=0, le=60)
    GEMINI_BACKOFF_CAP_S: float = Field(default=32.0, gt=0, le=60)
    # Used only after the primary model exhausts its retries on transient errors.
    MULTIMODAL_FALLBACK_MODEL: str | None = None
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
    # Optional cross-encoder reranking (M2 B1; docs/04). Off by default: dense stays the
    # default path. Scores are raw and uncalibrated (ordering only, never a confidence).
    RERANK_ENABLED: bool = False
    RERANK_MODEL: str = "cross-encoder/ms-marco-MiniLM-L6-v2"
    # Hugging Face commit to load; None = latest. Pinned in .env.example for reproducibility.
    RERANK_MODEL_REVISION: str | None = None
    RERANK_CANDIDATES: int = Field(default=20, ge=1, le=100)  # retrieved, then reranked
    # --- Tracing ---
    LANGSMITH_TRACING: bool = False
    LANGSMITH_API_KEY: SecretStr | None = None
    LANGSMITH_PROJECT: str = "kyc-engine"
    # --- Limits ---
    MAX_REGENERATIONS: int = Field(default=2, ge=0, le=2)
    # --- Workflow graph (M3) ---
    # Clarification rounds before INSUFFICIENT_INFORMATION (M3_PLAN §5; hard max 2).
    MAX_CLARIFICATION_ROUNDS: int = Field(default=2, ge=0, le=2)
    # LangGraph super-step limit per invocation; must cover the longest path (builder check).
    GRAPH_RECURSION_LIMIT: int = Field(default=25, ge=1, le=100)
    # Longest accepted user question (characters); longer input is rejected at the gate.
    QUERY_MAX_CHARS: int = Field(default=2000, ge=100, le=10000)
    # Overall cap on an LLM node's work (calls, re-ask, retries, waits, fallback model).
    NODE_TIMEOUT_S: float = Field(default=180.0, gt=0, le=600)
    # --- Workflow API and recovery (M3 S6; M3_PLAN §9) ---
    # Postgres run lease: renewed every RUN_HEARTBEAT_S; a run whose lease expired can be
    # taken over by POST /retry. Never re-executed automatically.
    RUN_LEASE_S: float = Field(default=60.0, gt=0, le=3600)
    RUN_HEARTBEAT_S: float = Field(default=10.0, gt=0, le=600)
    SESSION_TTL_H: float = Field(default=24.0, gt=0, le=720)
    CHECKPOINT_POOL_MAX: int = Field(default=5, ge=1, le=50)
    # --- Progress events and SSE (M3 S7). PostgreSQL is the source; Redis only wakes the
    # stream up (falls back to polling every EVENTS_POLL_S). ---
    EVENTS_ENABLED: bool = True
    EVENTS_PUBLISH_TIMEOUT_S: float = Field(default=0.25, gt=0, le=5)
    EVENTS_POLL_S: float = Field(default=1.0, gt=0, le=60)
    SSE_KEEPALIVE_S: float = Field(default=15.0, gt=0, le=300)
    SSE_IDLE_TIMEOUT_S: float = Field(default=900.0, gt=0, le=86400)
    MAX_UPLOAD_MB: int = Field(default=20, gt=0)
    RATE_LIMIT_GROQ_RPM: int = Field(default=20, gt=0)
    RATE_LIMIT_GEMINI_RPM: int = Field(default=10, gt=0)
    # Per-minute token budget for Groq (input estimate + max output). Below the lowest
    # published figure (8K TPM, docs/02); check the organisation's real limits.
    RATE_LIMIT_GROQ_TPM: int = Field(default=6000, gt=0)
    # Longest a call may wait for rate-limit capacity before failing (transient).
    RATE_LIMIT_MAX_WAIT_S: float = Field(default=120.0, gt=0, le=600)
    # Redis operation timeout for the shared limiter; on any Redis error the limiter
    # falls back to an in-process window (never a different result, only less sharing).
    RATE_LIMIT_REDIS_TIMEOUT_S: float = Field(default=0.25, gt=0, le=5)
    # --- Health checks ---
    HEALTH_CHECK_TIMEOUT_S: float = Field(default=2.0, gt=0)
    # --- Form-schema cache (Redis; Postgres stays the source of truth) ---
    FORM_CACHE_ENABLED: bool = True
    FORM_CACHE_TTL_S: int = Field(default=604800, gt=0)  # 7 days
    FORM_CACHE_TIMEOUT_S: float = Field(default=0.25, gt=0, le=5)
    # --- LLM response cache (M3 decision D8): off by default. When on, only requests
    # whose input digest is in the committed synthetic allowlist are cached. ---
    LLM_CACHE_ENABLED: bool = False
    LLM_CACHE_TTL_S: int = Field(default=86400, gt=0, le=86400)  # at most 24 h
    LLM_CACHE_TIMEOUT_S: float = Field(default=0.25, gt=0, le=5)
    LLM_CACHE_ALLOWLIST: Path = Path("data/eval/m3_synthetic_inputs.jsonl")

    @model_validator(mode="after")
    def _heartbeat_below_lease(self) -> "Settings":
        if self.RUN_HEARTBEAT_S >= self.RUN_LEASE_S:
            raise ValueError("RUN_HEARTBEAT_S must be smaller than RUN_LEASE_S")
        return self

    @field_validator(
        "QDRANT_API_KEY",
        "GROQ_API_KEY",
        "GEMINI_API_KEY",
        "LANGSMITH_API_KEY",
        "PRIMARY_LLM_MODEL",
        "FALLBACK_LLM_MODEL",
        "PROMPT_GUARD_MODEL",
        "MULTIMODAL_MODEL",
        "MULTIMODAL_FALLBACK_MODEL",
        "GEMINI_DATA_TERMS_VERIFIED_ON",
        "GROQ_DATA_TERMS_VERIFIED_ON",
        "NLI_MODEL",
        "EMBEDDING_MODEL_REVISION",
        "RERANK_MODEL_REVISION",
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
        "CORPUS_DIR",
        "CORPUS_MANIFEST",
        "TESSERACT_CMD",
        "TESSDATA_DIR",
        "OCR_CACHE_DIR",
        "LLM_CACHE_ALLOWLIST",
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
