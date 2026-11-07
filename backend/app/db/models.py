"""ORM models. Tables are added per milestone, always via Alembic migrations (docs/08)."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
