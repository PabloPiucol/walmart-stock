from fastapi import BackgroundTasks, HTTPException
import pytest

from app import main
from app.main import (
    cancel_preview,
    health,
    resume_apply,
    run_status_payload,
    start_apply,
    templates,
)
from app.models import SyncItem, SyncRun
from tests.test_sync_service import sessions


def test_health_and_required_templates_exist():
    assert health() == {"status": "ok"}
    for template_name in ("base.html", "dashboard.html", "settings.html", "run.html"):
        assert templates.get_template(template_name) is not None


def test_preview_can_only_be_applied_once():
    session_factory = sessions()
    with session_factory() as db:
        run = SyncRun(status="preview_ready", changed_count=1)
        db.add(run)
        db.commit()

        response = start_apply(run.id, BackgroundTasks(), db)
        assert response.status_code == 303
        assert db.get(SyncRun, run.id).status == "applying"

        with pytest.raises(HTTPException) as exc:
            start_apply(run.id, BackgroundTasks(), db)
        assert exc.value.status_code == 409


def test_run_status_payload_reports_progress_and_cancel_capability():
    run = SyncRun(
        id=10,
        status="preparing",
        progress_stage="Consultando stock y variantes en Bsale",
        progress_current=3,
        progress_total=4,
        progress_sku="SKU-3",
        feed_id="feed-1",
        feed_ids_json='["feed-1","feed-2"]',
        omitted_count=2,
        walmart_summary_json='{"feed-1":{"items_received":4,"items_succeeded":3,"items_failed":1,"detailed_errors":1}}',
    )

    payload = run_status_payload(run)

    assert payload["percent"] == 75
    assert payload["can_cancel"] is True
    assert payload["sku"] == "SKU-3"
    assert payload["feed_ids"] == ["feed-1", "feed-2"]
    assert payload["omitted_count"] == 2
    assert payload["walmart_summary"] == {
        "received": 4,
        "succeeded": 3,
        "failed": 1,
        "detailed_errors": 1,
    }


def test_cancel_preview_is_only_allowed_while_preparing():
    session_factory = sessions()
    with session_factory() as db:
        preparing = SyncRun(status="preparing")
        ready = SyncRun(status="preview_ready")
        db.add_all([preparing, ready])
        db.commit()

        payload = cancel_preview(preparing.id, db)
        assert payload["cancel_requested"] is True
        assert payload["stage"] == "Cancelación solicitada"

        with pytest.raises(HTTPException) as exc:
            cancel_preview(ready.id, db)
        assert exc.value.status_code == 409


def test_failed_run_with_existing_feed_can_resume():
    session_factory = sessions()
    with session_factory() as db:
        run = SyncRun(
            status="failed",
            feed_id="feed-1",
            feed_ids_json='["feed-1"]',
            changed_count=1,
        )
        db.add(run)
        db.flush()
        db.add(SyncItem(
            run_id=run.id,
            sku="A",
            target_quantity=1,
            status="submitted",
            feed_id="feed-1",
        ))
        db.commit()

        response = resume_apply(run.id, BackgroundTasks(), db)

        assert response.status_code == 303
        assert run.status == "applying"
        assert run.progress_stage == "Reanudando seguimiento de Walmart"


def test_failed_run_without_feed_cannot_resume():
    session_factory = sessions()
    with session_factory() as db:
        run = SyncRun(status="failed", changed_count=1)
        db.add(run)
        db.commit()

        with pytest.raises(HTTPException) as exc:
            resume_apply(run.id, BackgroundTasks(), db)

        assert exc.value.status_code == 409


def test_manual_walmart_authentication_test_redirects_after_result(monkeypatch):
    calls = []
    db = object()
    monkeypatch.setattr(main, "obtain_walmart_token", lambda value: calls.append(value))

    response = main.test_walmart_authentication(db)

    assert response.status_code == 303
    assert response.headers["location"] == "/settings"
    assert calls == [db]
