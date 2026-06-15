from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import sync_service
from app.clients import AuthenticationError, RateLimitError, WalmartFeedStatus
from app.database import Base
from app.models import SyncItem, SyncRun
from app.sync_service import (
    BsaleStock,
    _bsale_stock,
    _group_by_sku,
    build_preview_items,
    normalize_sku,
)


class FakeBsaleClient:
    def stocks(self, office_id):
        assert office_id == "10"
        return [
            {"quantityAvailable": 5, "variant": {"id": 1}, "office": {"id": 10}},
            {"quantityAvailable": -2, "variant": {"id": 2}, "office": {"id": 10}},
            {"quantityAvailable": 3, "variant": {"id": 1}, "office": {"id": 10}},
        ]

    def variants(self):
        return [
            {"id": 1, "code": " A-1 "},
            {"id": 2, "code": "B-2"},
            {"id": 3, "code": ""},
        ]


def sessions():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def config(timeout=10):
    return SimpleNamespace(
        walmart_client_id="client",
        walmart_client_secret="secret",
        walmart_feed_timeout_seconds=timeout,
        walmart_feed_poll_seconds=0,
    )


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def seed_applying_run(session_factory, values):
    with session_factory() as db:
        run = SyncRun(status="applying", changed_count=len(values))
        db.add(run)
        db.flush()
        db.add_all([
            SyncItem(
                run_id=run.id,
                sku=sku,
                target_quantity=quantity,
                status="pending",
            )
            for sku, quantity in values.items()
        ])
        db.commit()
        return run.id


def test_bsale_stock_sums_records_and_clamps_negative_to_zero():
    values = _bsale_stock(FakeBsaleClient(), "10")

    assert [(value.sku, value.variant_id, value.quantity) for value in values] == [
        ("A-1", "1", 8),
        ("B-2", "2", 0),
        ("", "3", 0),
    ]


def test_group_by_sku_keeps_duplicates_and_missing_separate():
    values = [{"sku": "A"}, {"sku": " A "}, {"sku": ""}]

    grouped, missing = _group_by_sku(values, lambda value: value["sku"])

    assert grouped["A"] == values[:2]
    assert missing == [values[2]]


def test_normalize_sku_only_trims_outer_whitespace():
    assert normalize_sku(" Ab c ") == "Ab c"
    assert normalize_sku(None) == ""


def test_build_preview_marks_all_valid_bsale_skus_pending():
    bsale = [
        BsaleStock("SEND", "1", 7),
        BsaleStock("DUP", "2", 1),
        BsaleStock("DUP", "3", 2),
        BsaleStock("", "4", 1),
    ]

    items = build_preview_items(10, bsale)
    by_sku = {item.sku: item for item in items}

    assert by_sku["SEND"].status == "pending"
    assert by_sku["SEND"].before_quantity is None
    assert by_sku["SEND"].target_quantity == 7
    assert "duplicado en Bsale" in by_sku["DUP"].message
    assert "no tiene SKU" in by_sku["SIN SKU / Bsale #4"].message


def test_create_preview_only_queries_bsale(monkeypatch):
    session_factory = sessions()
    with session_factory() as db:
        run = SyncRun(status="preparing")
        db.add(run)
        db.commit()
        run_id = run.id

    preview_config = SimpleNamespace(
        bsale_access_token="token",
        bsale_api_url="https://bsale.test",
        walmart_client_id="",
        walmart_client_secret="",
    )
    monkeypatch.setattr(sync_service, "SessionLocal", session_factory)
    monkeypatch.setattr(sync_service, "get_config", lambda: preview_config)
    monkeypatch.setattr(sync_service, "get_setting", lambda _db, _key: "10")
    monkeypatch.setattr(sync_service, "BsaleClient", lambda *_args: FakeBsaleClient())
    monkeypatch.setattr(
        sync_service,
        "_walmart_client",
        lambda: (_ for _ in ()).throw(AssertionError("No debe consultar Walmart")),
    )

    sync_service.create_preview(run_id)

    with session_factory() as db:
        run = db.get(SyncRun, run_id)
        items = {item.sku: item for item in db.scalars(select(SyncItem)).all()}
        assert run.status == "preview_ready"
        assert run.changed_count == 2
        assert run.error_count == 1
        assert items["A-1"].status == "pending"
        assert items["B-2"].status == "pending"


def test_create_preview_finishes_as_cancelled_before_api_calls(monkeypatch):
    session_factory = sessions()
    with session_factory() as db:
        run = SyncRun(status="preparing", cancel_requested=True)
        db.add(run)
        db.commit()
        run_id = run.id

    preview_config = SimpleNamespace(
        bsale_access_token="token",
        bsale_api_url="https://bsale.test",
    )
    monkeypatch.setattr(sync_service, "SessionLocal", session_factory)
    monkeypatch.setattr(sync_service, "get_config", lambda: preview_config)
    monkeypatch.setattr(sync_service, "get_setting", lambda _db, _key: "10")
    monkeypatch.setattr(
        sync_service,
        "BsaleClient",
        lambda *_args: (_ for _ in ()).throw(AssertionError("No debe consultar Bsale")),
    )

    sync_service.create_preview(run_id)

    with session_factory() as db:
        run = db.get(SyncRun, run_id)
        assert run.status == "cancelled"
        assert run.progress_stage == "Preparación cancelada"
        assert run.finished_at is not None


def test_apply_preview_processes_multiple_feeds_and_omits_rejections(monkeypatch):
    session_factory = sessions()
    run_id = seed_applying_run(session_factory, {"OK": 4, "MISSING": 5})
    submitted = []

    class FakeWalmart:
        def inventory_feed_batches(self, quantities):
            assert quantities == {"MISSING": 5, "OK": 4}
            return [{"OK": 4}, {"MISSING": 5}]

        def submit_inventory_feed(self, quantities):
            submitted.append(quantities)
            return f"feed-{len(submitted)}"

        def feed_status(self, feed_id):
            if feed_id == "feed-1":
                return WalmartFeedStatus("PROCESSED", {}, 1, 0)
            return WalmartFeedStatus(
                "PROCESSED",
                {"MISSING": ("DATA_ERROR", "SKU inexistente")},
                0,
                1,
            )

    monkeypatch.setattr(sync_service, "SessionLocal", session_factory)
    monkeypatch.setattr(sync_service, "get_config", config)
    monkeypatch.setattr(sync_service, "_walmart_client", lambda _db: FakeWalmart())

    sync_service.apply_preview(run_id)

    with session_factory() as db:
        run = db.get(SyncRun, run_id)
        items = {item.sku: item for item in db.scalars(select(SyncItem)).all()}
        assert run.status == "completed"
        assert run.feed_id == "feed-1"
        assert run.feed_ids == ["feed-1", "feed-2"]
        assert run.applied_count == 1
        assert run.omitted_count == 1
        assert run.failure_count == 0
        assert items["OK"].status == "applied"
        assert items["OK"].feed_id == "feed-1"
        assert items["MISSING"].status == "omitted"
        assert items["MISSING"].feed_id == "feed-2"
        assert items["MISSING"].message == "SKU inexistente"


def test_apply_preview_stops_before_feeds_when_authentication_fails(monkeypatch):
    session_factory = sessions()
    run_id = seed_applying_run(session_factory, {"A": 1})

    def fail_authentication(_db):
        raise AuthenticationError("GET /v3/token/detail", "credenciales inválidas")

    monkeypatch.setattr(sync_service, "SessionLocal", session_factory)
    monkeypatch.setattr(sync_service, "get_config", config)
    monkeypatch.setattr(sync_service, "_walmart_client", fail_authentication)

    sync_service.apply_preview(run_id)

    with session_factory() as db:
        run = db.get(SyncRun, run_id)
        item = db.scalar(select(SyncItem))
        assert run.status == "failed"
        assert run.feed_ids == []
        assert run.progress_stage == "Falló la aplicación"
        assert "credenciales inválidas" in run.error_message
        assert item.status == "pending"
        assert item.feed_id == ""


def test_apply_preview_waits_until_feed_becomes_visible(monkeypatch):
    session_factory = sessions()
    run_id = seed_applying_run(session_factory, {"A": 1})
    clock = FakeClock()
    statuses = [
        WalmartFeedStatus("", {}, 0, 0, available=False),
        WalmartFeedStatus("PROCESSED", {}, 1, 0),
    ]

    class FakeWalmart:
        def inventory_feed_batches(self, quantities):
            return [quantities]

        def submit_inventory_feed(self, _quantities):
            return "feed-1"

        def feed_status(self, _feed_id):
            return statuses.pop(0)

    wait_config = config()
    wait_config.walmart_feed_poll_seconds = 2
    monkeypatch.setattr(sync_service, "SessionLocal", session_factory)
    monkeypatch.setattr(sync_service, "get_config", lambda: wait_config)
    monkeypatch.setattr(sync_service, "_walmart_client", lambda _db: FakeWalmart())
    monkeypatch.setattr(sync_service.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(sync_service.time, "sleep", clock.sleep)

    sync_service.apply_preview(run_id)

    with session_factory() as db:
        run = db.get(SyncRun, run_id)
        assert run.status == "completed"
        assert run.applied_count == 1
        assert clock.sleeps == [2]


def test_apply_preview_retries_rate_limit_during_feed_tracking(monkeypatch):
    session_factory = sessions()
    run_id = seed_applying_run(session_factory, {"A": 1})
    clock = FakeClock()
    calls = 0

    class FakeWalmart:
        def inventory_feed_batches(self, quantities):
            return [quantities]

        def submit_inventory_feed(self, _quantities):
            return "feed-1"

        def feed_status(self, _feed_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RateLimitError("GET", "/v3/feeds", "limitado", retry_after=3)
            return WalmartFeedStatus("PROCESSED", {}, 1, 0)

    monkeypatch.setattr(sync_service, "SessionLocal", session_factory)
    monkeypatch.setattr(sync_service, "get_config", config)
    monkeypatch.setattr(sync_service, "_walmart_client", lambda _db: FakeWalmart())
    monkeypatch.setattr(sync_service.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(sync_service.time, "sleep", clock.sleep)

    sync_service.apply_preview(run_id)

    with session_factory() as db:
        assert db.get(SyncRun, run_id).status == "completed"
    assert calls == 2
    assert clock.sleeps == [3]


def test_resume_existing_feed_without_resubmitting_and_continues_pending(monkeypatch):
    session_factory = sessions()
    with session_factory() as db:
        run = SyncRun(
            status="applying",
            changed_count=2,
            feed_id="feed-existing",
            feed_ids_json='["feed-existing"]',
        )
        db.add(run)
        db.flush()
        db.add_all([
            SyncItem(
                run_id=run.id,
                sku="EXISTING",
                target_quantity=1,
                status="submitted",
                feed_id="",
            ),
            SyncItem(
                run_id=run.id,
                sku="PENDING",
                target_quantity=2,
                status="pending",
            ),
        ])
        db.commit()
        run_id = run.id
    submitted = []

    class FakeWalmart:
        def inventory_feed_batches(self, quantities):
            assert quantities == {"PENDING": 2}
            return [quantities]

        def submit_inventory_feed(self, quantities):
            submitted.append(quantities)
            return "feed-new"

        def feed_status(self, feed_id):
            assert feed_id in {"feed-existing", "feed-new"}
            return WalmartFeedStatus("PROCESSED", {}, 1, 0)

    monkeypatch.setattr(sync_service, "SessionLocal", session_factory)
    monkeypatch.setattr(sync_service, "get_config", config)
    monkeypatch.setattr(sync_service, "_walmart_client", lambda _db: FakeWalmart())

    sync_service.apply_preview(run_id)

    with session_factory() as db:
        run = db.get(SyncRun, run_id)
        items = {item.sku: item for item in db.scalars(select(SyncItem)).all()}
        assert run.status == "completed"
        assert run.feed_ids == ["feed-existing", "feed-new"]
        assert submitted == [{"PENDING": 2}]
        assert items["EXISTING"].feed_id == "feed-existing"
        assert items["PENDING"].feed_id == "feed-new"


def test_apply_preview_stops_later_batches_after_technical_error(monkeypatch):
    session_factory = sessions()
    run_id = seed_applying_run(session_factory, {"A": 1, "B": 2, "C": 3})
    submitted = []

    class FakeWalmart:
        def inventory_feed_batches(self, _quantities):
            return [{"A": 1}, {"B": 2}, {"C": 3}]

        def submit_inventory_feed(self, quantities):
            submitted.append(quantities)
            return f"feed-{len(submitted)}"

        def feed_status(self, feed_id):
            if feed_id == "feed-1":
                return WalmartFeedStatus("PROCESSED", {}, 1, 0)
            raise RuntimeError("Walmart no disponible")

    monkeypatch.setattr(sync_service, "SessionLocal", session_factory)
    monkeypatch.setattr(sync_service, "get_config", config)
    monkeypatch.setattr(sync_service, "_walmart_client", lambda _db: FakeWalmart())

    sync_service.apply_preview(run_id)

    with session_factory() as db:
        run = db.get(SyncRun, run_id)
        items = {item.sku: item for item in db.scalars(select(SyncItem)).all()}
        assert run.status == "failed"
        assert run.feed_ids == ["feed-1", "feed-2"]
        assert submitted == [{"A": 1}, {"B": 2}]
        assert items["A"].status == "applied"
        assert items["B"].status == "submitted"
        assert items["C"].status == "pending"


def test_apply_preview_times_out_and_preserves_feed_ids(monkeypatch):
    session_factory = sessions()
    run_id = seed_applying_run(session_factory, {"A": 1})

    class FakeWalmart:
        def inventory_feed_batches(self, quantities):
            return [quantities]

        def submit_inventory_feed(self, _quantities):
            return "feed-timeout"

        def feed_status(self, _feed_id):
            raise AssertionError("No debe consultar si el timeout ya venció")

    monkeypatch.setattr(sync_service, "SessionLocal", session_factory)
    monkeypatch.setattr(sync_service, "get_config", lambda: config(timeout=0))
    monkeypatch.setattr(sync_service, "_walmart_client", lambda _db: FakeWalmart())

    sync_service.apply_preview(run_id)

    with session_factory() as db:
        run = db.get(SyncRun, run_id)
        assert run.status == "failed"
        assert run.feed_id == "feed-timeout"
        assert run.feed_ids == ["feed-timeout"]
        assert "feed-timeout" in run.error_message
