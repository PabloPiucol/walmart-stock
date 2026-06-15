from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.clients import BsaleClient
from app.config import get_config
from app.database import get_db, init_db, purge_old_runs, recover_interrupted_runs
from app.models import SyncRun, utcnow
from app.settings_store import get_setting, set_setting
from app.sync_service import ACTIVE_STATUSES, apply_preview, create_preview
from app.walmart_auth import (
    validate_walmart_auth,
    walmart_auth_diagnostic,
    walmart_configured,
)


BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")
SYNC_START_LOCK = Lock()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    recover_interrupted_runs()
    purge_old_runs()
    yield


app = FastAPI(title="Bsale → Walmart Chile Stock", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def page_context(request: Request, db: Session, **values):
    config = get_config()
    return {
        "request": request,
        "walmart_configured": walmart_configured(config),
        "bsale_configured": bool(config.bsale_access_token),
        "office_name": get_setting(db, "bsale_office_name"),
        **values,
    }


def can_resume_run(run: SyncRun) -> bool:
    return (
        run.status == "failed"
        and bool(run.feed_ids)
        and any(item.status in {"submitted", "pending"} for item in run.items)
    )


def run_status_payload(run: SyncRun) -> dict:
    active = run.status in ACTIVE_STATUSES
    percent = None
    if run.progress_total > 0:
        percent = min(100, round(run.progress_current * 100 / run.progress_total))
    elif not active:
        percent = 100
    return {
        "id": run.id,
        "status": run.status,
        "active": active,
        "stage": run.progress_stage,
        "current": run.progress_current,
        "total": run.progress_total,
        "percent": percent,
        "sku": run.progress_sku,
        "cancel_requested": run.cancel_requested,
        "can_cancel": run.status == "preparing" and not run.cancel_requested,
        "can_resume": can_resume_run(run),
        "updated_at": run.progress_updated_at.isoformat() if run.progress_updated_at else None,
        "feed_id": run.feed_id,
        "feed_ids": run.feed_ids,
        "applied_count": run.applied_count,
        "omitted_count": run.omitted_count,
        "failure_count": run.failure_count,
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    runs = db.scalars(select(SyncRun).order_by(SyncRun.created_at.desc()).limit(20)).all()
    active = db.scalar(select(SyncRun).where(SyncRun.status.in_(ACTIVE_STATUSES)).limit(1))
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=page_context(request, db, runs=runs, active=active),
    )


@app.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    config = get_config()
    offices = []
    error = ""
    if config.bsale_access_token:
        try:
            offices = BsaleClient(config.bsale_api_url, config.bsale_access_token).offices()
        except Exception as exc:
            error = str(exc)
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context=page_context(
            request,
            db,
            offices=offices,
            selected_office=get_setting(db, "bsale_office_id"),
            walmart_auth=walmart_auth_diagnostic(db),
            error=error,
        ),
    )


@app.post("/settings/office")
def save_office(
    office_id: str = Form(...),
    office_name: str = Form(...),
    db: Session = Depends(get_db),
):
    set_setting(db, "bsale_office_id", office_id)
    set_setting(db, "bsale_office_name", office_name)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/walmart/test")
def test_walmart_authentication(db: Session = Depends(get_db)):
    try:
        validate_walmart_auth(db)
    except Exception:
        pass
    return RedirectResponse("/settings", status_code=303)


@app.post("/sync/preview")
def start_preview(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    with SYNC_START_LOCK:
        purge_old_runs()
        active = db.scalar(select(SyncRun).where(SyncRun.status.in_(ACTIVE_STATUSES)).limit(1))
        if active:
            return RedirectResponse(f"/runs/{active.id}", status_code=303)
        run = SyncRun(
            status="preparing",
            progress_stage="En cola",
            progress_updated_at=utcnow(),
        )
        db.add(run)
        db.commit()
    background_tasks.add_task(create_preview, run.id)
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


@app.get("/runs/{run_id}")
def run_detail(run_id: int, request: Request, db: Session = Depends(get_db)):
    run = db.scalar(
        select(SyncRun).options(selectinload(SyncRun.items)).where(SyncRun.id == run_id)
    )
    if run is None:
        raise HTTPException(404, "Ejecución no encontrada")
    return templates.TemplateResponse(
        request=request,
        name="run.html",
        context=page_context(request, db, run=run, can_resume=can_resume_run(run)),
    )


@app.get("/runs/{run_id}/status")
def run_status(run_id: int, db: Session = Depends(get_db)):
    run = db.get(SyncRun, run_id)
    if run is None:
        raise HTTPException(404, "Ejecución no encontrada")
    return run_status_payload(run)


@app.post("/runs/{run_id}/cancel")
def cancel_preview(run_id: int, db: Session = Depends(get_db)):
    with SYNC_START_LOCK:
        run = db.get(SyncRun, run_id)
        if run is None:
            raise HTTPException(404, "Ejecución no encontrada")
        if run.status != "preparing":
            raise HTTPException(409, "Solo se puede cancelar una vista previa en preparación")
        run.cancel_requested = True
        run.progress_stage = "Cancelación solicitada"
        run.progress_updated_at = utcnow()
        db.commit()
    return run_status_payload(run)


@app.post("/runs/{run_id}/apply")
def start_apply(run_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    with SYNC_START_LOCK:
        run = db.get(SyncRun, run_id)
        if run is None:
            raise HTTPException(404, "Ejecución no encontrada")
        if run.status != "preview_ready":
            raise HTTPException(409, "Esta vista previa ya no se puede aplicar")
        other_active = db.scalar(
            select(SyncRun).where(SyncRun.status.in_(ACTIVE_STATUSES), SyncRun.id != run_id).limit(1)
        )
        if other_active:
            raise HTTPException(409, "Existe otra ejecución activa")
        run.status = "applying"
        run.finished_at = None
        run.progress_stage = "Preparando envío a Walmart"
        run.progress_current = 0
        run.progress_total = run.changed_count
        run.progress_sku = ""
        run.progress_updated_at = utcnow()
        db.commit()
    background_tasks.add_task(apply_preview, run.id)
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


@app.post("/runs/{run_id}/resume")
def resume_apply(run_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    with SYNC_START_LOCK:
        run = db.scalar(
            select(SyncRun).options(selectinload(SyncRun.items)).where(SyncRun.id == run_id)
        )
        if run is None:
            raise HTTPException(404, "Ejecución no encontrada")
        if not can_resume_run(run):
            raise HTTPException(409, "Esta ejecución no se puede reanudar")
        other_active = db.scalar(
            select(SyncRun).where(SyncRun.status.in_(ACTIVE_STATUSES), SyncRun.id != run_id).limit(1)
        )
        if other_active:
            raise HTTPException(409, "Existe otra ejecución activa")
        run.status = "applying"
        run.finished_at = None
        run.error_message = ""
        run.progress_stage = "Reanudando seguimiento de Walmart"
        run.progress_current = run.applied_count + run.omitted_count
        run.progress_total = run.changed_count
        run.progress_sku = ""
        run.progress_updated_at = utcnow()
        db.commit()
    background_tasks.add_task(apply_preview, run.id)
    return RedirectResponse(f"/runs/{run.id}", status_code=303)
