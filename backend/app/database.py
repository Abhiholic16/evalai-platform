"""SQLite connection and table setup for the local EvalAI demo."""
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def database_url() -> str:
    configured = os.getenv("EVALAI_DATABASE_URL")
    if configured:
        return configured
    path = Path(__file__).resolve().parents[1] / "evalai.sqlite3"
    return f"sqlite:///{path.as_posix()}"


engine = create_engine(database_url(), connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def restrict_database_file_permissions() -> None:
    """Limit the SQLite file to its service account on POSIX hosts."""
    if os.name == "nt" or engine.url.get_backend_name() != "sqlite":
        return
    filename = engine.url.database
    if filename and filename != ":memory:":
        path = Path(filename)
        if path.is_file():
            path.chmod(0o600)
