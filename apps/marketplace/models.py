import hashlib
import hmac
import secrets

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

SUPPORTED_SCOPES = ("employees:read", "employees:write")
SUPPORTED_EVENTS = ("employee.hired", "employee.updated", "employee.terminated", "app.uninstalled")


def hash_secret(raw: str) -> str:
    """SHA-256 is enough here: every secret we hash is 256 bits of randomness."""
    return hashlib.sha256(raw.encode()).hexdigest()


def new_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def _default_scopes():
    return ["employees:read"]


def _default_events():
    return list(SUPPORTED_EVENTS)


class App(models.Model):
    """A third-party app listed in the marketplace (an OAuth client)."""

    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    developer = models.CharField(max_length=120)
    homepage_url = models.URLField(blank=True)
    client_id = models.CharField(max_length=64, unique=True, editable=False)
    client_secret_hash = models.CharField(max_length=64, editable=False)
    redirect_uris = models.JSONField(default=list)
    webhook_url = models.URLField()
    scopes = models.JSONField(default=_default_scopes, help_text="Scopes the app may request.")
    events = models.JSONField(default=_default_events, help_text="Webhook events the app subscribes to.")
    connector_config = models.JSONField(
        null=True,
        blank=True,
        help_text="Field mapping from the canonical employee to the app's schema (see apps.connectors).",
    )
    is_published = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @classmethod
    def register(cls, **fields) -> tuple["App", str]:
        """Create an app and return it with its client secret (shown once, stored hashed)."""
        raw_secret = new_token("acs")
        app = cls(client_id=new_token("aci")[:64], client_secret_hash=hash_secret(raw_secret), **fields)
        app.full_clean()
        app.save()
        return app, raw_secret

    def check_client_secret(self, raw: str) -> bool:
        return hmac.compare_digest(self.client_secret_hash, hash_secret(raw or ""))


class Installation(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending authorization"
        ACTIVE = "active", "Active"
        UNINSTALLED = "uninstalled", "Uninstalled"

    app = models.ForeignKey(App, on_delete=models.PROTECT, related_name="installations")
    installed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    scopes = models.JSONField(default=list)
    webhook_secret = models.CharField(max_length=80, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    uninstalled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One live installation per app for the organisation.
            models.UniqueConstraint(
                fields=["app"],
                condition=Q(status__in=["pending", "active"]),
                name="one_live_installation_per_app",
            )
        ]

    def __str__(self) -> str:
        return f"{self.app.slug} ({self.status})"

    def save(self, *args, **kwargs):
        if not self.webhook_secret:
            self.webhook_secret = new_token("whsec")
        super().save(*args, **kwargs)

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE


class AuthorizationCode(models.Model):
    installation = models.ForeignKey(Installation, on_delete=models.CASCADE, related_name="codes")
    code_hash = models.CharField(max_length=64, unique=True)
    redirect_uri = models.URLField()
    code_challenge = models.CharField(max_length=128, blank=True)
    code_challenge_method = models.CharField(max_length=10, blank=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"auth code for installation {self.installation_id}"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at


class AccessToken(models.Model):
    installation = models.ForeignKey(Installation, on_delete=models.CASCADE, related_name="tokens")
    token_hash = models.CharField(max_length=64, unique=True)
    refresh_token_hash = models.CharField(max_length=64, unique=True)
    scopes = models.JSONField(default=list)
    expires_at = models.DateTimeField()
    refresh_expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Lets permissions tell app tokens apart from DRF user tokens without imports.
    is_app_token = True

    class Meta:
        indexes = [models.Index(fields=["installation", "revoked_at"])]

    def __str__(self) -> str:
        return f"access token for installation {self.installation_id}"

    @property
    def is_valid(self) -> bool:
        return self.revoked_at is None and timezone.now() < self.expires_at
