"""Publishing domain events to installed apps (transactional outbox).

``publish`` is called inside the same database transaction as the change that
caused the event. It writes one ``WebhookDelivery`` row per subscribed
installation; the Celery task that sends each one is only enqueued after the
transaction commits. If the transaction rolls back, nothing is sent. If Redis is
down at commit time, the rows are still there and the replay job picks them up.
"""

import logging
import uuid

from django.db import transaction
from django.utils import timezone

from apps.connectors.transform import apply_mapping

from .models import WebhookDelivery

logger = logging.getLogger(__name__)


def _envelope(installation, event_type: str, event_id: uuid.UUID, data: dict) -> dict:
    config = installation.app.connector_config
    if config and event_type.startswith("employee."):
        data = {**apply_mapping(config, data), "_appconnect": {"employee_id": data.get("id")}}
    return {
        "id": str(event_id),
        "type": event_type,
        "created_at": timezone.now().isoformat(),
        "installation_id": installation.pk,
        "data": data,
    }


def enqueue(delivery_ids: list[int]) -> None:
    from .tasks import deliver_webhook

    for delivery_id in delivery_ids:
        try:
            deliver_webhook.delay(delivery_id)
        except Exception:  # broker outage: the row stays pending and is replayed later
            logger.exception("Could not enqueue webhook delivery %s", delivery_id)


def _create_deliveries(installations, event_type: str, data: dict, event_id: uuid.UUID) -> list[WebhookDelivery]:
    deliveries = []
    for installation in installations:
        delivery, created = WebhookDelivery.objects.get_or_create(
            installation=installation,
            event_id=event_id,
            defaults={
                "event_type": event_type,
                "payload": _envelope(installation, event_type, event_id, data),
                "next_attempt_at": timezone.now(),
            },
        )
        if created:
            deliveries.append(delivery)
    ids = [d.pk for d in deliveries]
    if ids:
        transaction.on_commit(lambda: enqueue(ids))
    return deliveries


def publish(event_type: str, data: dict, *, event_id: uuid.UUID | None = None) -> list[WebhookDelivery]:
    """Fan an event out to every active installation subscribed to it."""
    from apps.marketplace.models import Installation

    installations = [
        inst
        for inst in Installation.objects.select_related("app").filter(status=Installation.Status.ACTIVE)
        if event_type in inst.app.events and "employees:read" in inst.scopes
    ]
    return _create_deliveries(installations, event_type, data, event_id or uuid.uuid4())


def publish_to(installation, event_type: str, data: dict) -> list[WebhookDelivery]:
    """Send an event to a single installation regardless of its subscriptions (lifecycle events)."""
    return _create_deliveries([installation], event_type, data, uuid.uuid4())
