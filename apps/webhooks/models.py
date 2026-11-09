import uuid

from django.db import models


class WebhookDelivery(models.Model):
    """One event destined for one installation: the outbox row and its delivery log.

    ``idempotency_key`` is generated once and sent on every attempt (including
    manual and scheduled replays) so receivers can safely dedupe retries.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    installation = models.ForeignKey("marketplace.Installation", on_delete=models.CASCADE, related_name="deliveries")
    event_id = models.UUIDField(db_index=True)
    event_type = models.CharField(max_length=50)
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    payload = models.JSONField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    replay_count = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name_plural = "webhook deliveries"
        constraints = [
            models.UniqueConstraint(fields=["installation", "event_id"], name="one_delivery_per_event_per_installation")
        ]
        indexes = [models.Index(fields=["status", "next_attempt_at"])]

    def __str__(self) -> str:
        return f"{self.event_type} -> installation {self.installation_id} ({self.status})"
