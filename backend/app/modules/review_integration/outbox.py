from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.modules.final_cut_review.infra.sqlalchemy_models import OutboxConsumerReceiptModel, OutboxEventModel


Publisher = Callable[[dict[str, Any]], None]


class OutboxDispatcher:
    def __init__(self, session: Session, max_attempts: int = 5) -> None:
        self.session = session
        self.max_attempts = max_attempts

    def dispatch_once(self, publisher: Publisher, limit: int = 50) -> int:
        query = (
            select(OutboxEventModel)
            .where(
                OutboxEventModel.status.in_(["pending", "failed"]),
                OutboxEventModel.attempts < self.max_attempts,
            )
            .order_by(OutboxEventModel.sequence, OutboxEventModel.id)
            .limit(limit)
        )
        if self.session.bind is not None and self.session.bind.dialect.name != "sqlite":
            query = query.with_for_update(skip_locked=True)
        events = list(self.session.scalars(query))
        dispatched = 0
        for event in events:
            event.status = "publishing"
            event.attempts += 1
            self.session.flush()
            envelope = self._event_envelope(event)
            try:
                publisher(envelope)
            except Exception:
                event.status = "failed"
                self.session.flush()
                continue
            event.status = "dispatched"
            dispatched += 1
            self.session.flush()
        return dispatched

    def record_consumed(self, event_id: str, consumer_name: str) -> bool:
        receipt = OutboxConsumerReceiptModel(event_id=event_id, consumer_name=consumer_name)
        self.session.add(receipt)
        try:
            self.session.flush()
        except IntegrityError:
            self.session.rollback()
            return False
        return True

    @staticmethod
    def _event_envelope(event: OutboxEventModel) -> dict[str, Any]:
        envelope = {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "event_version": event.event_version,
            "occurred_at": event.occurred_at.isoformat(),
            "aggregate_type": event.aggregate_type,
            "aggregate_id": event.aggregate_id,
            "aggregate_version": event.aggregate_version,
            "sequence": event.sequence,
            "project_ref_id": event.project_ref_id,
            "review_item_id": event.review_item_id,
            "version_id": event.version_id,
            "issue_id": event.issue_id,
            "finalization_id": event.finalization_id,
            "package_id": event.package_id,
            "correlation_id": event.correlation_id,
            "causation_id": event.causation_id,
            "metadata": event.metadata_json,
            "payload": event.payload,
        }
        return {key: value for key, value in envelope.items() if value is not None}
