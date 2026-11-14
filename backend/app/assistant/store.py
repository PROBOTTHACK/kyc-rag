"""Persistence for assistant conversations (A5; migration 0009).

`AssistantStore` is the interface the service uses; `PostgresAssistantStore` implements it on
the async engine. A turn runs inside `store.turn(session_id)`: the session row is locked
(`SELECT ... FOR UPDATE`) for the whole turn, so two requests on one conversation are
applied one after the other, and a repeated idempotency key always sees the first result.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.assistant.profile import DialogueState
from app.db.models import AssistantIdempotencyRow, AssistantSessionRow, AssistantTurnRow


@dataclass(frozen=True)
class TurnRecord:
    turn_no: int
    message: str | None  # screened text only; None when screening refused it
    screen_categories: tuple[str, ...]
    request: dict[str, Any]  # the enum-validated choice fields (action, slot, value)
    answer: dict[str, Any]


@dataclass(frozen=True)
class StoredSession:
    id: uuid.UUID
    state: DialogueState
    expires_at: datetime
    turns: tuple[TurnRecord, ...] = ()  # filled by `load` only


@dataclass(frozen=True)
class Replay:
    request_sha256: str
    turn_no: int
    answer: dict[str, Any]


class TurnTx(Protocol):
    @property
    def session(self) -> StoredSession | None: ...

    async def replay(self, key: str) -> Replay | None: ...

    async def recent(self, n: int) -> list[tuple[str | None, str | None]]:
        """The last `n` turns, oldest first: (screened customer message, assistant reply)."""
        ...

    async def save(
        self, state: DialogueState, record: TurnRecord, key: str, sha: str, expires_at: datetime
    ) -> None: ...


class AssistantStore(Protocol):
    async def create(
        self, sid: uuid.UUID, state: DialogueState, opening: TurnRecord, expires_at: datetime
    ) -> None: ...

    async def load(self, sid: uuid.UUID) -> StoredSession | None: ...

    def turn(self, sid: uuid.UUID) -> AbstractAsyncContextManager[TurnTx]: ...


def _state_json(state: DialogueState) -> dict[str, Any]:
    return state.model_dump(mode="json")


def _record(row: AssistantTurnRow) -> TurnRecord:
    return TurnRecord(
        turn_no=row.turn_no,
        message=row.message,
        screen_categories=tuple(row.screen_categories),
        request=row.request,
        answer=row.answer,
    )


def _turn_row(sid: uuid.UUID, record: TurnRecord) -> AssistantTurnRow:
    return AssistantTurnRow(
        id=uuid.uuid4(),
        session_id=sid,
        turn_no=record.turn_no,
        message=record.message,
        screen_categories=list(record.screen_categories),
        request=record.request,
        answer=record.answer,
    )


class _PostgresTurn:
    def __init__(self, s: AsyncSession, row: AssistantSessionRow | None) -> None:
        self._s = s
        self._row = row
        self._session = (
            None
            if row is None
            else StoredSession(
                id=row.id,
                state=DialogueState.model_validate(row.state),
                expires_at=row.expires_at,
            )
        )

    @property
    def session(self) -> StoredSession | None:
        return self._session

    async def replay(self, key: str) -> Replay | None:
        assert self._row is not None  # noqa: S101 - only called for an existing session
        found = (
            await self._s.execute(
                select(AssistantIdempotencyRow.request_sha256, AssistantTurnRow)
                .join(AssistantTurnRow, AssistantTurnRow.id == AssistantIdempotencyRow.turn_id)
                .where(
                    AssistantIdempotencyRow.session_id == self._row.id,
                    AssistantIdempotencyRow.key == key,
                )
            )
        ).first()
        if found is None:
            return None
        sha, turn = found
        return Replay(request_sha256=sha, turn_no=turn.turn_no, answer=turn.answer)

    async def recent(self, n: int) -> list[tuple[str | None, str | None]]:
        assert self._row is not None  # noqa: S101 - only called for an existing session
        rows = (
            await self._s.scalars(
                select(AssistantTurnRow)
                .where(AssistantTurnRow.session_id == self._row.id)
                .order_by(AssistantTurnRow.turn_no.desc())
                .limit(n)
            )
        ).all()
        return [(t.message, (t.answer or {}).get("reply")) for t in reversed(rows)]

    async def save(
        self, state: DialogueState, record: TurnRecord, key: str, sha: str, expires_at: datetime
    ) -> None:
        row = self._row
        assert row is not None  # noqa: S101 - only called for an existing session
        turn = _turn_row(row.id, record)
        self._s.add(turn)
        await self._s.flush()
        self._s.add(
            AssistantIdempotencyRow(session_id=row.id, key=key, request_sha256=sha, turn_id=turn.id)
        )
        row.state = _state_json(state)
        row.turn_count = state.turn
        row.updated_at = func.now()
        row.expires_at = expires_at


class PostgresAssistantStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self, sid: uuid.UUID, state: DialogueState, opening: TurnRecord, expires_at: datetime
    ) -> None:
        async with self._sessions() as s, s.begin():
            s.add(
                AssistantSessionRow(
                    id=sid, state=_state_json(state), turn_count=state.turn, expires_at=expires_at
                )
            )
            await s.flush()
            s.add(_turn_row(sid, opening))

    async def load(self, sid: uuid.UUID) -> StoredSession | None:
        async with self._sessions() as s:
            row = await s.get(AssistantSessionRow, sid)
            if row is None:
                return None
            turns = (
                await s.scalars(
                    select(AssistantTurnRow)
                    .where(AssistantTurnRow.session_id == sid)
                    .order_by(AssistantTurnRow.turn_no)
                )
            ).all()
            return StoredSession(
                id=row.id,
                state=DialogueState.model_validate(row.state),
                expires_at=row.expires_at,
                turns=tuple(_record(t) for t in turns),
            )

    @asynccontextmanager
    async def _turn(self, sid: uuid.UUID) -> AsyncIterator[TurnTx]:
        async with self._sessions() as s, s.begin():
            row = await s.get(AssistantSessionRow, sid, with_for_update=True)
            yield _PostgresTurn(s, row)

    def turn(self, sid: uuid.UUID) -> AbstractAsyncContextManager[TurnTx]:
        return self._turn(sid)
