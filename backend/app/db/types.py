"""Shared column types, so conventions are declared once instead of being
re-derived (and mis-derived) per model."""

import enum
from typing import TypeVar

from sqlalchemy import Enum

E = TypeVar("E", bound=enum.Enum)


def pg_enum(enum_cls: type[E], name: str) -> Enum:
    """A native Postgres enum whose labels are the enum's *values*, not its
    member names.

    Members here are conventionally written `RUNNING = "running"` — uppercase
    name, lowercase value. SQLAlchemy defaults to persisting the member name,
    which would put `RUNNING` in the database while Pydantic serialises
    `running` to the API. `values_callable` keeps the database, the API, and
    the logs all speaking the same lowercase vocabulary.
    """
    return Enum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda cls: [member.value for member in cls],
    )
