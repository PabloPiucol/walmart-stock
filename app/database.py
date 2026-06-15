from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, delete, event, inspect, text, update
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_config


class Base(DeclarativeBase):
    pass


config = get_config()
if config.database_url.startswith("sqlite:///") and not config.database_url.endswith(":memory:"):
    database_path = Path(config.database_url.removeprefix("sqlite:///"))
    database_path.parent.mkdir(parents=True, exist_ok=True)
connect_args = {"check_same_thread": False} if config.database_url.startswith("sqlite") else {}
engine = create_engine(config.database_url, connect_args=connect_args)


if config.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

SQLITE_SYNC_RUN_COLUMNS = {
    "progress_stage": "VARCHAR(100) NOT NULL DEFAULT ''",
    "progress_current": "INTEGER NOT NULL DEFAULT 0",
    "progress_total": "INTEGER NOT NULL DEFAULT 0",
    "progress_sku": "VARCHAR(255) NOT NULL DEFAULT ''",
    "progress_updated_at": "DATETIME",
    "cancel_requested": "BOOLEAN NOT NULL DEFAULT 0",
    "feed_ids_json": "TEXT NOT NULL DEFAULT '[]'",
    "omitted_count": "INTEGER NOT NULL DEFAULT 0",
}

SQLITE_SYNC_ITEM_COLUMNS = {
    "feed_id": "VARCHAR(100) NOT NULL DEFAULT ''",
}


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(engine)
    if config.database_url.startswith("sqlite"):
        run_columns = {column["name"] for column in inspect(engine).get_columns("sync_runs")}
        item_columns = {column["name"] for column in inspect(engine).get_columns("sync_items")}
        with engine.begin() as connection:
            for name, definition in SQLITE_SYNC_RUN_COLUMNS.items():
                if name not in run_columns:
                    connection.execute(text(f"ALTER TABLE sync_runs ADD COLUMN {name} {definition}"))
            for name, definition in SQLITE_SYNC_ITEM_COLUMNS.items():
                if name not in item_columns:
                    connection.execute(text(f"ALTER TABLE sync_items ADD COLUMN {name} {definition}"))


def purge_old_runs() -> None:
    from app.models import SyncRun

    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=90)
    with SessionLocal() as session:
        session.execute(delete(SyncRun).where(SyncRun.created_at < cutoff))
        session.commit()


def recover_interrupted_runs() -> None:
    from app.models import SyncRun

    with SessionLocal() as session:
        session.execute(
            update(SyncRun)
            .where(SyncRun.status.in_(("preparing", "applying")))
            .values(
                status="failed",
                finished_at=datetime.now(UTC).replace(tzinfo=None),
                progress_stage="Interrumpida",
                progress_updated_at=datetime.now(UTC).replace(tzinfo=None),
                error_message="La ejecución fue interrumpida por un reinicio del servicio",
            )
        )
        session.commit()
