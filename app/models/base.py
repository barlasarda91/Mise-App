"""SQLAlchemy declarative base. Concrete models land in milestone 2 with migrations."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
