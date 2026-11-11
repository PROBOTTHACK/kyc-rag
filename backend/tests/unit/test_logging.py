"""M3 S8: prompts never reach the logs through third-party DEBUG logging (found in the
live Groq run: `groq._base_client` logs the full request options at DEBUG)."""

import logging

import pytest

from app.core.logging import QUIET_LOGGERS, configure_logging


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_payload_logging_libraries_are_held_at_warning(env: str) -> None:
    configure_logging(env)
    for name in (*QUIET_LOGGERS, "groq._base_client", "httpcore.http11", "httpcore2.http11"):
        assert not logging.getLogger(name).isEnabledFor(logging.INFO), name


def test_a_groq_debug_request_record_is_dropped(caplog: pytest.LogCaptureFixture) -> None:
    configure_logging("dev")
    with caplog.at_level(logging.DEBUG):  # the root at DEBUG, as in dev
        logging.getLogger("groq._base_client").debug("Request options: %s", {"messages": "x"})
        logging.getLogger("app.workflow.service").debug("app record")
    names = [r.name for r in caplog.records]
    assert "groq._base_client" not in names and "app.workflow.service" in names
