"""M3 S1: Groq text provider without network or API key.

The SDK call is injected (`complete=`); a fake clock, sleep and jitter source make
retries and backoff deterministic. Real `groq` exception classes check classification.
No test reads GROQ_API_KEY from the environment.
"""

import asyncio
import datetime as dt
import json
import logging
import os
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import groq
import httpx
import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

from app.config import REPO_ROOT, Settings
from app.llm.common import (
    ProviderError,
    ProviderRateLimited,
    ProviderRefused,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.llm.groq import GroqProvider, llm_from_settings
from app.llm.provider import FallbackLLMProvider, LLMRequest

EXAMPLE_ENV = REPO_ROOT / "infra" / ".env.example"  # never the real .env
KEY = "gsk_TEST-not-a-real-key-0123456789"
PROMPT_TEXT = "SYNTHETIC-PROMPT-MARKER what documents does a trust need"


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[str]


VALID = Answer(items=["deed of trust"])


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


class SpyLimiter:
    def __init__(self) -> None:
        self.tokens: list[int] = []

    async def acquire(self, tokens: int) -> None:
        self.tokens.append(tokens)


def completion(text: str | None, finish: str = "stop", refusal: str | None = None) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish,
                message=SimpleNamespace(content=text, refusal=refusal),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=900, completion_tokens=40),
    )


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", "https://api.groq.test/v1"))


def status_error(status: int, code: str | None = None) -> Exception:
    """A real SDK status error whose message and body echo the key and the prompt."""
    body = {"error": {"message": f"bad {KEY} {PROMPT_TEXT}", "code": code or "x"}}
    classes: dict[int, type[groq.APIStatusError]] = {
        400: groq.BadRequestError,
        401: groq.AuthenticationError,
        429: groq.RateLimitError,
        500: groq.InternalServerError,
    }
    cls = classes.get(status, groq.APIStatusError)
    return cls(f"Error code: {status} {KEY} {PROMPT_TEXT}", response=_response(status), body=body)


def provider(script: list[Any], **kw: Any) -> tuple[GroqProvider, FakeTime, SpyLimiter, list[Any]]:
    t = FakeTime()
    limiter = SpyLimiter()
    calls: list[Any] = []

    async def complete(req: LLMRequest, schema: Any, timeout_s: float) -> Any:
        calls.append((req.prompt_id, schema, timeout_s))
        nxt = script.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        if callable(nxt):
            return await nxt()
        return nxt

    p = GroqProvider(
        model_id="openai/gpt-oss-120b",
        api_key=SecretStr(KEY),
        limiter=kw.pop("limiter", limiter),
        sleep=t.sleep,
        clock=t.clock,
        rng=kw.pop("rng", lambda: 1.0),
        complete=complete,
        **kw,
    )
    return p, t, limiter, calls


def req(**kw: Any) -> LLMRequest:
    base: dict[str, Any] = {
        "prompt_id": "intake",
        "prompt_version": "1",
        "system": "You extract.",
        "user": PROMPT_TEXT,
        "max_output_tokens": 500,
    }
    return LLMRequest(**(base | kw))


def run(p: Any, request: LLMRequest | None = None) -> Any:
    return asyncio.run(p.generate_structured(request or req(), Answer))


# --- success and validation -------------------------------------------------------------


def test_valid_response_is_parsed_with_usage_and_provenance() -> None:
    p, _, limiter, calls = provider([completion(VALID.model_dump_json())])
    r = run(p)
    assert r.parsed == VALID and r.validation_errors == []
    assert (r.provider, r.model_id, r.finish_reason, r.attempts) == (
        "groq",
        "openai/gpt-oss-120b",
        "stop",
        1,
    )
    assert r.usage.input_tokens == 900 and r.usage.output_tokens == 40
    assert r.cache_hit is False and len(calls) == 1
    assert len(limiter.tokens) == 1 and limiter.tokens[0] > 500  # prompt estimate + output cap


@pytest.mark.parametrize("text", ['{"items": "not a list"}', "not json", '{"items": [], "x": 1}'])
def test_invalid_output_is_returned_unparsed_not_retried(text: str) -> None:
    p, _, _, calls = provider([completion(text)])
    r = run(p)
    assert r.parsed is None and r.validation_errors and len(calls) == 1


def test_empty_and_truncated_output_are_invalid() -> None:
    p, _, _, _ = provider([completion(None), completion(VALID.model_dump_json(), "length")])
    assert run(p).validation_errors == ["<root>: empty response"]
    r = run(p)
    assert r.parsed is None and r.validation_errors[0] == "<root>: output truncated (length)"


def test_schema_rejection_by_groq_is_invalid_output_not_retried_and_never_echoed() -> None:
    p, _, _, calls = provider([status_error(400, "json_validate_failed")])
    r = run(p)
    assert r.parsed is None and len(calls) == 1
    assert r.finish_reason == "json_validate_failed"
    assert "provider rejected the output" in r.validation_errors[0]
    text = json.dumps(r.model_dump(mode="json"))
    assert KEY not in text and "SYNTHETIC-PROMPT-MARKER" not in text


@pytest.mark.parametrize(
    "resp", [completion("{}", finish="content_filter"), completion(None, refusal="no")]
)
def test_refusals_raise_and_are_never_retried_or_transient(resp: Any) -> None:
    p, _, _, calls = provider([resp])
    with pytest.raises(ProviderRefused) as err:
        run(p)
    assert err.value.transient is False and len(calls) == 1


# --- retries, backoff, timeouts ---------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_errors_are_retried_then_succeed(status: int) -> None:
    p, t, limiter, calls = provider([status_error(status), completion(VALID.model_dump_json())])
    r = run(p)
    assert r.parsed == VALID and r.attempts == 2 and len(calls) == 2
    assert t.sleeps == [1.0] and len(limiter.tokens) == 2  # every attempt is rate-limited


@pytest.mark.parametrize(
    ("status", "exc"), [(429, ProviderRateLimited), (503, ProviderUnavailable)]
)
def test_at_most_two_retries_then_a_transient_error(status: int, exc: type[ProviderError]) -> None:
    p, t, _, calls = provider([status_error(status) for _ in range(5)])
    with pytest.raises(exc) as err:
        run(p)
    assert len(calls) == 3 and err.value.transient is True  # 1 + 2 retries, hard max
    assert t.sleeps == [1.0, 2.0] and "after 3 attempts" in str(err.value)


def test_backoff_is_jittered_and_capped() -> None:
    p, t, _, _ = provider(
        [status_error(503)] * 3, rng=lambda: 0.0, backoff_initial_s=4.0, backoff_cap_s=6.0
    )
    with pytest.raises(ProviderUnavailable):
        run(p)
    assert t.sleeps == [2.0, 3.0]  # 50 % of min(cap, 4 * 2^n)


@pytest.mark.parametrize("status", [400, 401, 404])
def test_non_transient_errors_are_not_retried(status: int) -> None:
    p, _, _, calls = provider([status_error(status)])
    with pytest.raises(ProviderUnavailable) as err:
        run(p)
    assert len(calls) == 1 and err.value.transient is False


def test_sdk_timeout_and_connection_errors_are_retried() -> None:
    request = httpx.Request("POST", "https://api.groq.test/v1")
    p, _, _, calls = provider(
        [
            groq.APITimeoutError(request=request),
            groq.APIConnectionError(request=request),
            completion(VALID.model_dump_json()),
        ]
    )
    assert run(p).parsed == VALID and len(calls) == 3


def test_timeout_is_enforced_and_retried() -> None:
    async def hang() -> Any:
        await asyncio.sleep(10)

    p, _, _, calls = provider([hang, hang, hang], timeout_s=0.01)
    with pytest.raises(ProviderTimeout) as err:
        run(p)
    assert len(calls) == 3 and err.value.transient is True


def test_retry_and_timeout_limits_are_capped() -> None:
    for kw in ({"max_retries": 3}, {"timeout_s": 61}, {"backoff_initial_s": 9, "backoff_cap_s": 8}):
        with pytest.raises(ValueError):
            provider([], **kw)


# --- secrets ------------------------------------------------------------------------------


def test_key_and_prompt_never_appear_in_repr_errors_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    for script in ([status_error(401)], [status_error(429)] * 3):
        p, _, _, _ = provider(script)
        assert KEY not in repr(p) and KEY not in str(p)
        with pytest.raises(ProviderError) as err:
            run(p)
        chain = [err.value, err.value.__cause__, err.value.__context__]
        for e in chain:
            if e is not None:
                assert KEY not in str(e) and "SYNTHETIC-PROMPT-MARKER" not in str(e)
    assert KEY not in caplog.text and "SYNTHETIC-PROMPT-MARKER" not in caplog.text


# --- gates, settings, imports -------------------------------------------------------------


def settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "GROQ_API_KEY": KEY,
        "PRIMARY_LLM_MODEL": "openai/gpt-oss-120b",
        "FALLBACK_LLM_MODEL": "openai/gpt-oss-20b",
        "GROQ_LIVE_ENABLED": True,
        "GROQ_DATA_TERMS_VERIFIED_ON": dt.date(2026, 11, 11),
        "LLM_CACHE_ENABLED": False,
    }
    return Settings(_env_file=EXAMPLE_ENV, **(base | kw))  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("override", "missing"),
    [
        ({"GROQ_LIVE_ENABLED": False}, "GROQ_LIVE_ENABLED"),
        ({"GROQ_API_KEY": ""}, "GROQ_API_KEY"),
        ({"PRIMARY_LLM_MODEL": ""}, "PRIMARY_LLM_MODEL"),
        ({"GROQ_DATA_TERMS_VERIFIED_ON": ""}, "GROQ_DATA_TERMS_VERIFIED_ON"),
    ],
)
def test_live_provider_requires_every_gate(override: dict[str, Any], missing: str) -> None:
    with pytest.raises(ProviderUnavailable, match=missing) as err:
        GroqProvider.from_settings(settings(**override), limiter=SpyLimiter())
    assert KEY not in str(err.value)


def test_live_stack_is_built_from_settings_without_any_call() -> None:
    p = llm_from_settings(settings(), limiter=SpyLimiter())
    assert isinstance(p, FallbackLLMProvider)
    assert isinstance(p.primary, GroqProvider) and isinstance(p.fallback, GroqProvider)
    assert p.primary.model_id == "openai/gpt-oss-120b"
    assert p.fallback.model_id == "openai/gpt-oss-20b"
    assert p.primary.limiter is p.fallback.limiter  # one shared budget
    assert p.primary.max_retries == 2 and p.primary.timeout_s == 30.0
    assert p.primary._client is None  # no SDK client, no network until a call is made


def test_no_fallback_wrapper_when_the_fallback_model_is_the_primary() -> None:
    p = llm_from_settings(settings(FALLBACK_LLM_MODEL="openai/gpt-oss-120b"), limiter=SpyLimiter())
    assert isinstance(p, GroqProvider)


def test_live_calls_and_cache_are_off_by_default() -> None:
    fields = Settings.model_fields
    assert fields["GROQ_LIVE_ENABLED"].default is False
    assert fields["GROQ_DATA_TERMS_VERIFIED_ON"].default is None
    assert fields["GROQ_MAX_RETRIES"].default == 2
    assert fields["LLM_CACHE_ENABLED"].default is False
    s = Settings(_env_file=EXAMPLE_ENV)  # type: ignore[call-arg]
    assert s.GROQ_LIVE_ENABLED is False and s.LLM_CACHE_ENABLED is False
    assert s.GROQ_DATA_TERMS_VERIFIED_ON is None


def test_groq_retries_cannot_exceed_the_project_default() -> None:
    with pytest.raises(ValueError):
        settings(GROQ_MAX_RETRIES=3)


def test_app_imports_without_the_groq_sdk_or_a_key() -> None:
    code = (
        "import sys; import app.main, app.llm.provider, app.llm.groq, app.llm.cache; "
        "from app.config import get_settings; s = get_settings(); "
        "print(s.GROQ_API_KEY is None, s.GROQ_LIVE_ENABLED, 'groq' in sys.modules)"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("GROQ")}
    env |= {"GROQ_API_KEY": "", "TESSDATA_DIR": ""}
    out = subprocess.run(  # noqa: S603 (fixed interpreter and code)
        [sys.executable, "-c", code],
        cwd=REPO_ROOT / "backend",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip() == "True False False"


def test_live_request_uses_json_schema_output_and_no_sdk_retries() -> None:
    """The real request arguments, sent to a stub client (no network, no key use)."""
    captured: dict[str, Any] = {}

    async def create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return completion(VALID.model_dump_json())

    p = GroqProvider(model_id="openai/gpt-oss-120b", api_key=SecretStr(KEY), limiter=SpyLimiter())
    p._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert run(p).parsed == VALID
    assert captured["model"] == "openai/gpt-oss-120b"
    assert captured["messages"] == [
        {"role": "system", "content": "You extract."},
        {"role": "user", "content": PROMPT_TEXT},
    ]
    fmt = captured["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["name"] == "Answer"
    assert fmt["json_schema"]["schema"] == Answer.model_json_schema()
    assert captured["temperature"] == 0.0 and captured["stream"] is False
    assert captured["max_completion_tokens"] == 500


def test_live_client_disables_sdk_retries() -> None:
    p = GroqProvider(model_id="m", api_key=SecretStr(KEY), limiter=SpyLimiter(), timeout_s=12)
    client = p._live_client()
    assert client.max_retries == 0 and client.timeout == 12
