import pytest


@pytest.fixture(autouse=True)
def _synthetic_connection_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests never touch real services; give Settings synthetic required URLs."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@localhost:1/test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:1/0")
