"""Logging setup. Never log settings values, connection URLs, keys or PII."""

import logging

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(app_env: str) -> None:
    level = logging.DEBUG if app_env == "dev" else logging.INFO
    logging.basicConfig(level=level, format=_FORMAT)
