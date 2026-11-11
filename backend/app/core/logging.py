"""Logging setup. Never log settings values, connection URLs, keys or PII.

Third-party HTTP/LLM clients log full request options (prompt messages, evidence text)
and response headers at DEBUG. Found in the M3 S8 live Groq run: with the dev DEBUG root
level, `groq._base_client` wrote the prompt into the API log. M3_PLAN §8 says prompts are
never written to logs, so these loggers are held at WARNING in EVERY environment; the
app's own loggers keep the environment's level.
"""

import logging

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
# Libraries that log request/response payloads or headers at DEBUG/INFO.
QUIET_LOGGERS = (
    "groq",
    "google_genai",
    "google.genai",
    "httpx",
    "httpcore",
    "httpcore2",
    "urllib3",
    "huggingface_hub",
    "qdrant_client",
    "langsmith",
    "openai",
)


def configure_logging(app_env: str) -> None:
    level = logging.DEBUG if app_env == "dev" else logging.INFO
    logging.basicConfig(level=level, format=_FORMAT)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
