from datetime import UTC, datetime
import json

from sqlalchemy import Boolean, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(30), default="preparing", index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    finished_at: Mapped[datetime | None]
    applied_at: Mapped[datetime | None]
    feed_id: Mapped[str] = mapped_column(String(100), default="")
    feed_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    walmart_summary_json: Mapped[str] = mapped_column(Text, default="{}")
    progress_stage: Mapped[str] = mapped_column(String(100), default="")
    progress_current: Mapped[int] = mapped_column(default=0)
    progress_total: Mapped[int] = mapped_column(default=0)
    progress_sku: Mapped[str] = mapped_column(String(255), default="")
    progress_updated_at: Mapped[datetime | None]
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    total_count: Mapped[int] = mapped_column(default=0)
    changed_count: Mapped[int] = mapped_column(default=0)
    unchanged_count: Mapped[int] = mapped_column(default=0)
    error_count: Mapped[int] = mapped_column(default=0)
    applied_count: Mapped[int] = mapped_column(default=0)
    omitted_count: Mapped[int] = mapped_column(default=0)
    failure_count: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")

    items: Mapped[list["SyncItem"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="SyncItem.sku"
    )

    @property
    def feed_ids(self) -> list[str]:
        try:
            values = json.loads(self.feed_ids_json or "[]")
        except (TypeError, json.JSONDecodeError):
            values = []
        result = [str(value) for value in values if value]
        if self.feed_id and self.feed_id not in result:
            result.insert(0, self.feed_id)
        return result

    def add_feed_id(self, feed_id: str) -> None:
        values = self.feed_ids
        if feed_id not in values:
            values.append(feed_id)
        self.feed_ids_json = json.dumps(values, separators=(",", ":"))
        if not self.feed_id:
            self.feed_id = feed_id

    @property
    def walmart_summaries(self) -> dict[str, dict]:
        try:
            values = json.loads(self.walmart_summary_json or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return values if isinstance(values, dict) else {}

    @property
    def walmart_summary_totals(self) -> dict[str, int]:
        totals = {
            "received": 0,
            "succeeded": 0,
            "failed": 0,
            "detailed_errors": 0,
        }
        for summary in self.walmart_summaries.values():
            if not isinstance(summary, dict):
                continue
            totals["received"] += int(summary.get("items_received") or 0)
            totals["succeeded"] += int(summary.get("items_succeeded") or 0)
            totals["failed"] += int(summary.get("items_failed") or 0)
            totals["detailed_errors"] += int(summary.get("detailed_errors") or 0)
        return totals

    def set_walmart_summary(
        self,
        feed_id: str,
        *,
        status: str,
        items_received: int,
        items_succeeded: int,
        items_failed: int,
        detailed_errors: int,
    ) -> None:
        summaries = self.walmart_summaries
        summaries[feed_id] = {
            "status": status,
            "items_received": items_received,
            "items_succeeded": items_succeeded,
            "items_failed": items_failed,
            "detailed_errors": detailed_errors,
        }
        self.walmart_summary_json = json.dumps(summaries, separators=(",", ":"))


class SyncItem(Base):
    __tablename__ = "sync_items"
    __table_args__ = (Index("ix_sync_items_run_status", "run_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("sync_runs.id", ondelete="CASCADE"))
    sku: Mapped[str] = mapped_column(String(255), index=True)
    bsale_variant_id: Mapped[str] = mapped_column(String(100), default="")
    feed_id: Mapped[str] = mapped_column(String(100), default="", index=True)
    before_quantity: Mapped[int | None]
    target_quantity: Mapped[int | None]
    status: Mapped[str] = mapped_column(String(30))
    message: Mapped[str] = mapped_column(Text, default="")

    run: Mapped[SyncRun] = relationship(back_populates="items")
