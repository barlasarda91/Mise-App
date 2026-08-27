"""Declarative base + shared column helpers."""

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


# JSON that becomes JSONB on Postgres, plain JSON elsewhere (SQLite in tests).
PortableJSON = JSON().with_variant(JSONB(), "postgresql")


def db_enum(enum_cls: type[enum.Enum], **kwargs) -> Enum:
    """Store enums as VARCHAR + CHECK (not native PG enums), so value changes
    stay plain migrations instead of ALTER TYPE dances."""
    return Enum(
        enum_cls,
        native_enum=False,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        **kwargs,
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
