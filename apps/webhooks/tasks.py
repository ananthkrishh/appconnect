from celery import shared_task

from .delivery import attempt_delivery


@shared_task(bind=True, name="webhooks.deliver", acks_late=True, max_retries=None)
def deliver_webhook(self, delivery_id: int) -> str:
    """Send one webhook; reschedule with backoff while ``attempt_delivery`` says to retry.

    The attempt budget lives on the ``WebhookDelivery`` row (``WEBHOOK_MAX_ATTEMPTS``),
    not in Celery, so it survives worker restarts and replays.
    """
    result = attempt_delivery(delivery_id)
    if result.retry_in is not None:
        raise self.retry(countdown=result.retry_in)
    return result.status
