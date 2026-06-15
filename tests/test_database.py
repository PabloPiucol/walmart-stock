from sqlalchemy import create_engine, inspect, text

from app import database


def test_sqlite_migration_adds_columns_to_existing_tables(monkeypatch):
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE sync_runs ("
            "id INTEGER PRIMARY KEY, status VARCHAR(30) NOT NULL, created_at DATETIME NOT NULL"
            ")"
        ))
        connection.execute(text(
            "CREATE TABLE sync_items ("
            "id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, sku VARCHAR(255) NOT NULL"
            ")"
        ))

    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database.config, "database_url", "sqlite://")

    database.init_db()

    run_columns = {column["name"] for column in inspect(engine).get_columns("sync_runs")}
    item_columns = {column["name"] for column in inspect(engine).get_columns("sync_items")}
    assert set(database.SQLITE_SYNC_RUN_COLUMNS) <= run_columns
    assert set(database.SQLITE_SYNC_ITEM_COLUMNS) <= item_columns
