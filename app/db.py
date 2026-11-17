from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import prepare_sqlite_directory


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    prepare_sqlite_directory(url)
    kwargs = {"connect_args": {"check_same_thread": False, "timeout": 20}} if url.startswith("sqlite") else {}
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def configure(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")

    return engine


def make_sessions(engine):
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def writing(db: Session):
    """SQLite сериализует записи, PostgreSQL использует SELECT FOR UPDATE ниже."""
    try:
        if db.bind.dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        yield
        db.commit()
    except Exception:
        db.rollback()
        raise
