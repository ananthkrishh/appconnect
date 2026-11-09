"""Sending one webhook attempt and deciding what happens next.

Kept free of Celery so it can be unit tested directly; ``tasks.deliver_webhook``
is a thin wrapper that turns ``DeliveryResult.retry_in`` into ``self.retry``.
"""

import json
import logging
import random
from dataclasses import dataclass
from datetime import timedelta

import httpx
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import WebhookDelivery
from .signing import SIGNATURE_HEADER, build_signature_header

logger = logging.getLogger(__name__)

USER_AGENT = "AppConnect-Webhooks/1.0"
# 4xx codes that are worth retrying; every other 4xx is the receiver rejecting the payload.
RETRYABLE_4XX = {408, 409, 425, 429}
# Events still sent to an installation after it is uninstalled.
POST_UNINSTALL_EVENTS = {"app.uninstalled"}


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    retry_in: int | None = None
    status_code: int | None = None


def compute_backoff(attempt: int, *, base: int | None = None, cap: int | None = None, rng=random) -> int:
    """Exponential backoff with equal jitter: uniform(c/2, c) where c = min(cap, base * 2**(attempt-1))."""
    base = settings.WEBHOOK_BACKOFF_BASE if base is None else base
    cap = settings.WEBHOOK_BACKOFF_CAP if cap is None else cap
    ceiling = min(cap, base * (2 ** max(attempt - 1, 0)))
    return max(1, int(rng.uniform(ceiling / 2, ceiling)))


def _retry_after_seconds(response: httpx.Response) -> int | None:
    value = response.headers.get("Retry-After", "")
    return min(int(value), settings.WEBHOOK_BACKOFF_CAP) if value.isdigit() else None


def serialize_body(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def build_request(delivery: WebhookDelivery, *, timestamp: int | None = None) -> tuple[bytes, dict]:
    body = serialize_body(delivery.payload)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-AppConnect-Event": delivery.event_type,
        "X-AppConnect-Delivery": str(delivery.pk),
        "Idempotency-Key": str(delivery.idempotency_key),
        SIGNATURE_HEADER: build_signature_header(delivery.installation.webhook_secret, body, timestamp),
    }
    return body, headers


def _claim(delivery_id: int) -> WebhookDelivery | None:
    """Lock the row briefly, check it is still sendable and record the attempt."""
    with transaction.atomic():
        delivery = (
            WebhookDelivery.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("installation__app")
            .filter(pk=delivery_id)
            .first()
        )
        if delivery is None or delivery.status != WebhookDelivery.Status.PENDING:
            return None
        installation = delivery.installation
        if not installation.is_active and delivery.event_type not in POST_UNINSTALL_EVENTS:
            delivery.status = WebhookDelivery.Status.CANCELLED
            delivery.last_error = "Installation is not active."
            delivery.save(update_fields=["status", "last_error"])
            return None
        delivery.attempts += 1
        delivery.last_attempt_at = timezone.now()
        delivery.save(update_fields=["attempts", "last_attempt_at"])
        return delivery


def _finish(delivery: WebhookDelivery, *, status_code=None, error="", retryable=False, retry_hint=None):
    now = timezone.now()
    delivery.last_status_code = status_code
    delivery.last_error = error[:2000]
    fields = ["status", "last_status_code", "last_error", "next_attempt_at", "delivered_at"]

    if status_code is not None and 200 <= status_code < 300:
        delivery.status = WebhookDelivery.Status.SUCCEEDED
        delivery.delivered_at = now
        delivery.next_attempt_at = None
        delivery.save(update_fields=fields)
        return DeliveryResult(delivery.status, status_code=status_code)

    if retryable and delivery.attempts < settings.WEBHOOK_MAX_ATTEMPTS:
        delay = retry_hint or compute_backoff(delivery.attempts)
        delivery.next_attempt_at = now + timedelta(seconds=delay)
        delivery.save(update_fields=fields)
        return DeliveryResult(delivery.status, retry_in=delay, status_code=status_code)

    delivery.status = WebhookDelivery.Status.FAILED
    delivery.next_attempt_at = None
    delivery.save(update_fields=fields)
    logger.warning("Webhook delivery %s failed permanently: %s", delivery.pk, delivery.last_error)
    return DeliveryResult(delivery.status, status_code=status_code)


def attempt_delivery(delivery_id: int, *, client: httpx.Client | None = None) -> DeliveryResult:
    delivery = _claim(delivery_id)
    if delivery is None:
        current = WebhookDelivery.objects.filter(pk=delivery_id).values_list("status", flat=True).first()
        return DeliveryResult(current or "missing")

    body, headers = build_request(delivery)
    owns_client = client is None
    client = client or httpx.Client(timeout=settings.WEBHOOK_TIMEOUT, follow_redirects=False)
    try:
        response = client.post(delivery.installation.app.webhook_url, content=body, headers=headers)
    except httpx.TimeoutException as exc:
        return _finish(delivery, error=f"Timeout: {exc}", retryable=True)
    except httpx.HTTPError as exc:
        return _finish(delivery, error=f"{type(exc).__name__}: {exc}", retryable=True)
    finally:
        if owns_client:
            client.close()

    code = response.status_code
    if 200 <= code < 300:
        return _finish(delivery, status_code=code)
    retryable = code >= 500 or code in RETRYABLE_4XX
    return _finish(
        delivery,
        status_code=code,
        error=f"HTTP {code}: {response.text[:500]}",
        retryable=retryable,
        retry_hint=_retry_after_seconds(response) if code in (429, 503) else None,
    )


def replay_deliveries(
    *,
    since=None,
    limit: int = 500,
    include_stale_pending: bool = True,
    dry_run: bool = False,
    ids: list[int] | None = None,
) -> list[int]:
    """Reset failed (and stuck pending) deliveries for active installations and re-enqueue them.

    The idempotency key is preserved, so a receiver that did process an
    earlier attempt can recognise the replay.
    """
    from django.db.models import Q

    from apps.marketplace.models import Installation

    now = timezone.now()
    criteria = Q(status=WebhookDelivery.Status.FAILED)
    if include_stale_pending:
        stale_before = now - timedelta(seconds=settings.WEBHOOK_STALE_AFTER)
        criteria |= Q(status=WebhookDelivery.Status.PENDING, next_attempt_at__lt=stale_before)
    qs = WebhookDelivery.objects.filter(criteria, installation__status=Installation.Status.ACTIVE)
    if since is not None:
        qs = qs.filter(created_at__gte=since)
    if ids is not None:
        qs = qs.filter(pk__in=ids)
    ids = list(qs.order_by("created_at").values_list("pk", flat=True)[:limit])
    if dry_run or not ids:
        return ids

    with transaction.atomic():
        for delivery in WebhookDelivery.objects.select_for_update().filter(pk__in=ids):
            if delivery.status == WebhookDelivery.Status.FAILED:
                delivery.replay_count += 1
                delivery.attempts = 0
            delivery.status = WebhookDelivery.Status.PENDING
            delivery.next_attempt_at = now
            delivery.save(update_fields=["status", "attempts", "replay_count", "next_attempt_at"])
        from .dispatcher import enqueue

        transaction.on_commit(lambda: enqueue(ids))
    return ids
