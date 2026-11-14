"""The assistant's per-turn service behind `/api/v2/assistant` (A5).

One turn:
1. the raw body is parsed into `TurnInput` (enum-validated; errors never echo input);
2. under the session lock, a repeated `Idempotency-Key` returns the stored answer;
3. the A3 dialogue runs (`respond`; the optional LLM only behind the existing gate). It
   screens the message itself; a refused message is never stored, only its categories;
4. once the profile is ready, the A4 checklist is built from stored HDFC evidence;
5. the answer is mapped to the customer-facing `AssistantAnswer` and stored with the new state.

A checklist that could not be built (storage or retrieval failure) returns the fixed
technical message and stores nothing, so the same request can simply be sent again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.assistant.answer import AssistantAnswer, build_answer, profile_view, technical_answer
from app.assistant.catalogue import catalogue
from app.assistant.checklist import AnswerKind, ChecklistAnswer, ChecklistService
from app.assistant.dialogue import ReadyProfile, TurnInput, opening, respond
from app.assistant.store import AssistantStore, TurnRecord
from app.assistant.wording import Wording, wording
from app.config import Settings
from app.llm.provider import LLMProvider
from app.security.screen import screen_text

log = logging.getLogger(__name__)

SESSION_TTL = timedelta(hours=24)  # sliding: every stored turn extends it
MAX_BODY_BYTES = 16_384
MAX_KEY_CHARS = 200

ChecklistFactory = Callable[[], ChecklistService]


class AssistantError(Exception):
    """An API error with a fixed, codes-only message (never derived from the request)."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message


NOT_FOUND = AssistantError(404, "SESSION_NOT_FOUND", "The conversation was not found.")
EXPIRED = AssistantError(
    410, "SESSION_EXPIRED", "The conversation has expired. Please start a new one."
)
INVALID = AssistantError(422, "VALIDATION_ERROR", "The request is not valid.")
KEY_REQUIRED = AssistantError(
    400,
    "IDEMPOTENCY_KEY_REQUIRED",
    "An Idempotency-Key header (at most 200 characters) is required.",
)
KEY_MISMATCH = AssistantError(
    409, "IDEMPOTENCY_MISMATCH", "This Idempotency-Key was already used for a different request."
)


def _now() -> datetime:
    return datetime.now(UTC)


def request_sha256(data: dict[str, Any]) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _session_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise NOT_FOUND from None


def _parse(body: bytes) -> tuple[dict[str, Any], TurnInput]:
    if len(body) > MAX_BODY_BYTES:
        raise INVALID
    try:
        data = json.loads(body) if body else None
    except (ValueError, UnicodeDecodeError):
        raise INVALID from None
    if not isinstance(data, dict):
        raise INVALID
    try:
        return data, TurnInput.model_validate(data)
    except ValidationError:
        raise INVALID from None


def _stored_message(turn: TurnInput) -> tuple[str | None, tuple[str, ...]]:
    """What may be kept of the message: the screened text, only when screening allowed it."""
    if turn.message is None:
        return None, ()
    res = screen_text(turn.message)
    kept = res.text if res.action in ("allow", "redact") and not res.sensitive else None
    return kept, res.categories


def _choice(turn: TurnInput) -> dict[str, Any]:
    return {
        "action": turn.action.value if turn.action else None,
        "slot": turn.slot.value if turn.slot else None,
        "value": turn.value,
    }


class AssistantService:
    def __init__(
        self,
        store: AssistantStore,
        checklist: ChecklistFactory,
        llm: LLMProvider | None = None,
        *,
        words: Wording | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.store = store
        self._checklist = checklist
        self.llm = llm
        self.words = words or wording()
        self.technical_message = catalogue().wording.technical_error
        self.clock = clock

    # --- endpoints -------------------------------------------------------------------------------

    async def create_session(self) -> dict[str, Any]:
        state, response = opening()
        answer = build_answer(state, response, self.words).model_dump(mode="json")
        sid = uuid.uuid4()
        record = TurnRecord(
            turn_no=0, message=None, screen_categories=(), request={}, answer=answer
        )
        await self.store.create(sid, state, record, self.clock() + SESSION_TTL)
        return {"session_id": str(sid), "turn": 0, "answer": answer}

    async def get_session(self, raw_sid: str) -> dict[str, Any]:
        session = await self.store.load(_session_id(raw_sid))
        if session is None:
            raise NOT_FOUND
        if session.expires_at <= self.clock():
            raise EXPIRED
        turns = [
            {
                "turn": t.turn_no,
                "message": t.message,
                "choice": t.request if any(v is not None for v in t.request.values()) else None,
                "answer": t.answer,
            }
            for t in session.turns
        ]
        return {
            "session_id": str(session.id),
            "turn": session.state.turn,
            "profile": {
                k.value: v.model_dump(mode="json")
                for k, v in profile_view(session.state, self.words).items()
            },
            "turns": turns,
            "answer": session.turns[-1].answer if session.turns else None,
        }

    async def submit_turn(self, raw_sid: str, key: str | None, body: bytes) -> dict[str, Any]:
        if not key or len(key) > MAX_KEY_CHARS:
            raise KEY_REQUIRED
        sid = _session_id(raw_sid)
        data, turn = _parse(body)
        sha = request_sha256(data)
        async with self.store.turn(sid) as tx:
            session = tx.session
            if session is None:
                raise NOT_FOUND
            replay = await tx.replay(key)
            if replay is not None:
                if replay.request_sha256 != sha:
                    raise KEY_MISMATCH
                return {"session_id": str(sid), "turn": replay.turn_no, "answer": replay.answer}
            now = self.clock()
            if session.expires_at <= now:
                raise EXPIRED
            state, response = await respond(session.state, turn, llm=self.llm)
            checklist: ChecklistAnswer | None = None
            if response.ready is not None:
                checklist = await self._build(response.ready)
                if checklist is None or checklist.kind is AnswerKind.TECHNICAL_ERROR:
                    answer = technical_answer(session.state, self.words, self.technical_message)
                    # Nothing stored: the same request can be sent again.
                    return {
                        "session_id": str(sid),
                        "turn": session.state.turn,
                        "answer": answer.model_dump(mode="json"),
                    }
            built: AssistantAnswer = build_answer(state, response, self.words, checklist)
            message, categories = _stored_message(turn)
            record = TurnRecord(
                turn_no=state.turn,
                message=message,
                screen_categories=categories,
                request=_choice(turn),
                answer=built.model_dump(mode="json"),
            )
            await tx.save(state, record, key, sha, now + SESSION_TTL)
        return {"session_id": str(sid), "turn": record.turn_no, "answer": record.answer}

    # --- checklist -------------------------------------------------------------------------------

    async def _build(self, ready: ReadyProfile) -> ChecklistAnswer | None:
        def build() -> ChecklistAnswer:
            return self._checklist().build(ready)

        try:
            return await run_in_threadpool(build)
        except Exception as exc:  # noqa: BLE001 - the customer gets the fixed technical message
            log.error("assistant checklist could not be built: %s", type(exc).__name__)
            return None


class LazyChecklist:
    """Builds the production `ChecklistService` on first use (the sync engine for stored
    chunks and the dense retriever load lazily, off the event loop). Without a retriever the
    checklist still cites the catalogue's stored chunks (A4)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._service: ChecklistService | None = None
        self._engine: Any = None

    def __call__(self) -> ChecklistService:
        with self._lock:
            if self._service is None:
                from app.assistant.chunks import PostgresChunkSource
                from app.assistant.retrieval import AssistantRetriever
                from app.db.session import create_sync_engine, create_sync_sessionmaker
                from app.retrieval.factory import retriever_from_settings

                self._engine = create_sync_engine(self._settings)
                sessions = create_sync_sessionmaker(self._engine)
                retriever: AssistantRetriever | None
                try:
                    retriever = AssistantRetriever(
                        retriever_from_settings(self._settings, sessions=sessions)
                    )
                except Exception as exc:  # noqa: BLE001 - supplementary references only
                    log.warning("assistant retriever unavailable: %s", type(exc).__name__)
                    retriever = None
                self._service = ChecklistService(PostgresChunkSource(sessions), retriever)
            return self._service

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
