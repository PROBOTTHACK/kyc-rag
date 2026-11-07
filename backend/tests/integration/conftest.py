import pytest


@pytest.fixture(autouse=True)
def _synthetic_connection_env() -> None:
    """Override the unit-test fixture: integration tests use the real .env settings."""
