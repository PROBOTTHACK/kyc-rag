"""Application settings loaded from env vars.

Precedence (highest first): process env vars, repo-root `.env`, `infra/.env.example`.
The example file supplies non-secret defaults; real keys belong only in `.env`.
"""

from functools import lru_cache
from pathlib import Path

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
    NLI_MODEL: str | None = None
    # --- Corpus / ingestion (relative paths resolve against the repo root) ---
    CORPUS_DIR: Path = Path("data/raw_pdfs")
    CORPUS_MANIFEST: Path = Path("data/raw_pdfs/manifest.yaml")
    # Tesseract for PyMuPDF OCR (resolution order in app/ingestion/ocr.py).
    # TESSERACT_CMD: the tesseract executable; None = PATH, then the standard install location.
    # TESSDATA_DIR: language data; None = the `tessdata` folder next to TESSERACT_CMD.
    TESSERACT_CMD: Path | None = None
    TESSDATA_DIR: Path | None = None
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
        "TESSERACT_CMD",
        "TESSDATA_DIR",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        # Empty values in .env mean "not configured", never an empty key or model ID.
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("CORPUS_DIR", "CORPUS_MANIFEST", "TESSERACT_CMD", "TESSDATA_DIR")
    @classmethod
    def _repo_relative(cls, v: Path | None) -> Path | None:
        # Same rule as M0's env files: paths work from any working directory.
        if v is None or v.is_absolute():
            return v
        return REPO_ROOT / v


@lru_cache
def get_settings() -> Settings:
    return Settings()
