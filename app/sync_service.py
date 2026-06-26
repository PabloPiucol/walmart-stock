from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import time

from sqlalchemy import select

from app.clients import BsaleClient, RateLimitError, WalmartClient
from app.config import get_config
from app.database import SessionLocal
from app.models import SyncItem, SyncRun, utcnow
from app.settings_store import get_setting
from app.walmart_auth import obtain_walmart_token


ACTIVE_STATUSES = ("preparing", "applying")


class PreviewCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class BsaleStock:
    sku: str
    variant_id: str
    quantity: int


def normalize_sku(value: str | None) -> str:
    return (value or "").strip()


def _bsale_stock(client: BsaleClient, office_id: str) -> list[BsaleStock]:
    quantities: dict[str, int] = defaultdict(int)
    for stock in client.stocks(office_id):
        variant_id = str((stock.get("variant") or {}).get("id", ""))
        if variant_id:
            quantities[variant_id] += max(0, int(float(stock.get("quantityAvailable") or 0)))

    return [
        BsaleStock(
            sku=normalize_sku(variant.get("code")),
            variant_id=str(variant.get("id", "")),
            quantity=quantities.get(str(variant.get("id", "")), 0),
        )
        for variant in client.variants()
    ]


def _group_by_sku(values, sku_getter):
    grouped = defaultdict(list)
    missing = []
    for value in values:
        sku = normalize_sku(sku_getter(value))
        if sku:
            grouped[sku].append(value)
        else:
            missing.append(value)
    return grouped, missing


def build_preview_items(
    run_id: int,
    bsale_values: list[BsaleStock],
) -> list[SyncItem]:
    bsale_by_sku, bsale_missing = _group_by_sku(bsale_values, lambda value: value.sku)
    items: list[SyncItem] = []

    for value in bsale_missing:
        items.append(SyncItem(
            run_id=run_id,
            sku=f"SIN SKU / Bsale #{value.variant_id}",
            bsale_variant_id=value.variant_id,
            target_quantity=value.quantity,
            status="error",
            message="La variante de Bsale no tiene SKU",
        ))

    for sku in sorted(bsale_by_sku):
        matches: list[BsaleStock] = bsale_by_sku[sku]
        if len(matches) > 1:
            items.append(SyncItem(
                run_id=run_id,
                sku=sku,
                status="error",
                message=f"SKU duplicado en Bsale ({len(matches)})",
            ))
            continue
        source = matches[0]
        items.append(SyncItem(
            run_id=run_id,
            sku=sku,
            bsale_variant_id=source.variant_id,
            target_quantity=source.quantity,
            status="pending",
        ))
    return items


def _walmart_client(db) -> WalmartClient:
    return WalmartClient(obtain_walmart_token(db))


def _update_progress(
    db,
    run: SyncRun,
    stage: str,
    *,
    current: int | None = None,
    total: int | None = None,
    sku: str | None = None,
) -> None:
    run.progress_stage = stage
    if current is not None:
        run.progress_current = current
    if total is not None:
        run.progress_total = total
    if sku is not None:
        run.progress_sku = sku
    run.progress_updated_at = utcnow()
    db.commit()


def _raise_if_cancelled(db, run: SyncRun) -> None:
    db.refresh(run)
    if run.cancel_requested:
        raise PreviewCancelled


def _finish_cancelled(db, run: SyncRun) -> None:
    run.status = "cancelled"
    run.progress_stage = "Preparación cancelada"
    run.progress_updated_at = utcnow()
    run.finished_at = utcnow()
    db.commit()


def create_preview(run_id: int) -> None:
    config = get_config()
    with SessionLocal() as db:
        run = db.get(SyncRun, run_id)
        if run is None:
            return
        try:
            _update_progress(db, run, "Validando configuración", current=0, total=0, sku="")
            office_id = get_setting(db, "bsale_office_id")
            if not config.bsale_access_token:
                raise RuntimeError("Falta configurar BSALE_ACCESS_TOKEN")
            if not office_id:
                raise RuntimeError("No hay una sucursal Bsale seleccionada")

            _raise_if_cancelled(db, run)
            _update_progress(db, run, "Consultando stock y variantes en Bsale")
            bsale_values = _bsale_stock(
                BsaleClient(config.bsale_api_url, config.bsale_access_token),
                office_id,
            )
            _raise_if_cancelled(db, run)
            _update_progress(
                db,
                run,
                "Construyendo vista previa",
                current=len(bsale_values),
                total=len(bsale_values),
                sku="",
            )
            items = build_preview_items(run_id, bsale_values)
            _raise_if_cancelled(db, run)

            db.add_all(items)
            run.total_count = len(items)
            run.changed_count = sum(item.status == "pending" for item in items)
            run.unchanged_count = 0
            run.error_count = sum(item.status == "error" for item in items)
            run.status = "preview_ready"
            run.progress_stage = "Vista previa lista"
            run.progress_updated_at = utcnow()
            run.finished_at = utcnow()
            db.commit()
        except PreviewCancelled:
            _finish_cancelled(db, run)
        except Exception as exc:
            db.refresh(run)
            if run.cancel_requested:
                _finish_cancelled(db, run)
            else:
                run.status = "failed"
                run.progress_stage = "Falló la preparación"
                run.progress_updated_at = utcnow()
                run.error_message = str(exc)
                run.finished_at = utcnow()
                db.commit()


def _mark_terminal_items(run: SyncRun, items: list[SyncItem], status) -> None:
    for item in items:
        item_status, message = status.item_statuses.get(item.sku, ("", ""))
        if item_status in {"SUCCESS", "PROCESSED"}:
            item.status = "applied"
            run.applied_count += 1
        elif item_status:
            item.status = "omitted"
            item.message = message or f"Walmart informó estado {item_status}"
            run.omitted_count += 1
        elif (
            len(status.item_statuses) == status.items_failed
            and status.items_succeeded + status.items_failed >= len(items)
        ):
            item.status = "applied"
            run.applied_count += 1
        else:
            raise RuntimeError(
                "Walmart no informó resultados individuales suficientes para el feed"
            )


def _sleep_with_deadline(seconds: float, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(min(max(0.0, seconds), remaining))


def _follow_feed(
    db,
    run: SyncRun,
    client: WalmartClient,
    feed_id: str,
    items: list[SyncItem],
    label: str,
    completed_count: int,
    config,
) -> None:
    deadline = time.monotonic() + config.walmart_feed_timeout_seconds
    rate_limit_attempt = 0
    while time.monotonic() < deadline:
        try:
            status = client.feed_status(feed_id, fetch_errors=False)
        except RateLimitError as exc:
            delay = (
                exc.retry_after
                if exc.retry_after is not None
                else min(30 * (2 ** min(rate_limit_attempt, 4)), 300)
            )
            rate_limit_attempt += 1
            run.progress_stage = f"Walmart limitó consultas de {label}; reintentando"
            run.progress_updated_at = utcnow()
            db.commit()
            _sleep_with_deadline(delay, deadline)
            continue

        rate_limit_attempt = 0
        if not status.available:
            run.progress_stage = f"Walmart aún no publica el estado de {label}"
            run.progress_updated_at = utcnow()
            db.commit()
            _sleep_with_deadline(config.walmart_feed_poll_seconds, deadline)
            continue

        run.progress_stage = f"Procesando {label} ({status.status or 'sin estado'})"
        run.progress_current = min(
            run.progress_total,
            completed_count + status.items_succeeded + status.items_failed,
        )
        run.progress_updated_at = utcnow()
        db.commit()
        if status.terminal:
            if status.items_failed:
                run.progress_stage = f"Consultando errores de {label}"
                run.progress_updated_at = utcnow()
                db.commit()
                status = type(status)(
                    status=status.status,
                    item_statuses=client.feed_errors(feed_id),
                    items_succeeded=status.items_succeeded,
                    items_failed=status.items_failed,
                    items_received=status.items_received,
                    available=status.available,
                )
            _mark_terminal_items(run, items, status)
            run.progress_current = completed_count + len(items)
            run.progress_updated_at = utcnow()
            db.commit()
            return
        _sleep_with_deadline(config.walmart_feed_poll_seconds, deadline)

    raise RuntimeError(
        f"Walmart no terminó de procesar el feed {feed_id} "
        f"dentro de {config.walmart_feed_timeout_seconds} segundos"
    )


def _submitted_feed_groups(db, run: SyncRun) -> list[tuple[str, list[SyncItem]]]:
    submitted = db.scalars(
        select(SyncItem)
        .where(SyncItem.run_id == run.id, SyncItem.status == "submitted")
        .order_by(SyncItem.sku)
    ).all()
    if submitted and any(not item.feed_id for item in submitted):
        if len(run.feed_ids) != 1:
            raise RuntimeError(
                "No se puede asociar productos enviados a sus feeds existentes"
            )
        for item in submitted:
            if not item.feed_id:
                item.feed_id = run.feed_ids[0]
        db.commit()

    grouped: dict[str, list[SyncItem]] = defaultdict(list)
    for item in submitted:
        grouped[item.feed_id].append(item)
    ordered_ids = [feed_id for feed_id in run.feed_ids if feed_id in grouped]
    ordered_ids.extend(feed_id for feed_id in grouped if feed_id not in ordered_ids)
    return [(feed_id, grouped[feed_id]) for feed_id in ordered_ids]


def _finish_application(db, run: SyncRun) -> None:
    run.status = "completed"
    run.progress_stage = "Aplicación terminada"
    run.progress_current = run.applied_count + run.omitted_count
    run.progress_total = run.changed_count
    run.progress_updated_at = utcnow()
    run.applied_at = utcnow()
    run.finished_at = utcnow()
    run.error_message = ""
    db.commit()


def apply_preview(run_id: int) -> None:
    config = get_config()
    with SessionLocal() as db:
        run = db.get(SyncRun, run_id)
        if run is None or run.status != "applying":
            return
        try:
            pending = list(db.scalars(
                select(SyncItem)
                .where(SyncItem.run_id == run_id, SyncItem.status == "pending")
                .order_by(SyncItem.sku)
            ).all())
            existing_groups = _submitted_feed_groups(db, run)
            if not pending and not existing_groups:
                raise RuntimeError("La ejecución no contiene productos pendientes ni enviados")
            _update_progress(
                db,
                run,
                "Obteniendo token de Walmart",
                current=run.applied_count + run.omitted_count,
                total=run.changed_count,
                sku="",
            )
            client = _walmart_client(db)
            run.progress_total = run.changed_count
            completed_count = run.applied_count + run.omitted_count

            for feed_number, (feed_id, items) in enumerate(existing_groups, start=1):
                _follow_feed(
                    db,
                    run,
                    client,
                    feed_id,
                    items,
                    f"feed existente {feed_number} de {len(existing_groups)}",
                    completed_count,
                    config,
                )
                completed_count += len(items)

            quantities = {item.sku: int(item.target_quantity or 0) for item in pending}
            batches = client.inventory_feed_batches(quantities) if quantities else []
            items_by_sku = {item.sku: item for item in pending}
            initial_feed_count = len(run.feed_ids)
            total_feed_count = initial_feed_count + len(batches)
            for batch_number, batch in enumerate(batches, start=1):
                batch_items = [items_by_sku[sku] for sku in batch]
                feed_number = initial_feed_count + batch_number
                _update_progress(
                    db,
                    run,
                    f"Enviando feed {feed_number} de {total_feed_count}",
                    current=completed_count,
                    total=run.changed_count,
                    sku="",
                )
                feed_id = client.submit_inventory_feed(batch)
                run.add_feed_id(feed_id)
                for item in batch_items:
                    item.status = "submitted"
                    item.feed_id = feed_id
                run.progress_stage = f"Esperando feed {feed_number} de {total_feed_count}"
                run.progress_updated_at = utcnow()
                db.commit()

                _follow_feed(
                    db,
                    run,
                    client,
                    feed_id,
                    batch_items,
                    f"feed {feed_number} de {total_feed_count}",
                    completed_count,
                    config,
                )
                completed_count += len(batch_items)

            _finish_application(db, run)
        except Exception as exc:
            run.status = "failed"
            run.progress_stage = "Falló la aplicación"
            run.progress_updated_at = utcnow()
            run.error_message = str(exc)
            run.finished_at = utcnow()
            db.commit()
