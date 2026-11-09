"""OAuth 2.0 authorization server for marketplace apps.

Implements the authorization code grant (RFC 6749 §4.1) with optional PKCE
(RFC 7636, S256 only), refresh token rotation and token revocation (RFC 7009).
Raw codes and tokens are only ever returned to the caller; the database stores
SHA-256 hashes.
"""

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.exceptions import Conflict
from apps.webhooks import dispatcher
from apps.webhooks.models import WebhookDelivery

from .models import (
    SUPPORTED_SCOPES,
    AccessToken,
    App,
    AuthorizationCode,
    Installation,
    hash_secret,
    new_token,
)


class OAuthError(Exception):
    """Error surfaced in RFC 6749 §5.2 format by the token endpoint."""

    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    scopes: list[str]
    installation_id: int

    def as_response(self) -> dict:
        return {
            "access_token": self.access_token,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "refresh_token": self.refresh_token,
            "scope": " ".join(self.scopes),
            "installation_id": self.installation_id,
        }


# --------------------------------------------------------------------------- #
# Install / authorize
# --------------------------------------------------------------------------- #
def _with_query(url: str, **params) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query.update({k: v for k, v in params.items() if v})
    return urlunsplit(parts._replace(query=urlencode(query)))


def start_install(
    app: App,
    user,
    *,
    redirect_uri: str,
    scopes: list[str] | None = None,
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
) -> tuple[Installation, str]:
    """Admin consents to install ``app``. Returns the installation and the redirect URL with the code."""
    if redirect_uri not in app.redirect_uris:
        raise OAuthError("invalid_request", "redirect_uri is not registered for this app.")

    requested = scopes or list(app.scopes)
    unknown = sorted(set(requested) - (set(app.scopes) & set(SUPPORTED_SCOPES)))
    if unknown:
        raise OAuthError("invalid_scope", f"Scopes not allowed for this app: {', '.join(unknown)}.")

    if code_challenge and code_challenge_method != "S256":
        raise OAuthError("invalid_request", "Only the S256 code_challenge_method is supported.")

    raw_code = new_token("acc")
    try:
        with transaction.atomic():
            # A pending install that was never completed is replaced, an active one is a conflict.
            Installation.objects.filter(app=app, status=Installation.Status.PENDING).update(
                status=Installation.Status.UNINSTALLED, uninstalled_at=timezone.now()
            )
            installation = Installation.objects.create(app=app, installed_by=user, scopes=sorted(set(requested)))
            AuthorizationCode.objects.create(
                installation=installation,
                code_hash=hash_secret(raw_code),
                redirect_uri=redirect_uri,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                expires_at=timezone.now() + timedelta(seconds=settings.OAUTH_AUTH_CODE_TTL),
            )
    except IntegrityError:
        raise Conflict(f"{app.name} is already installed.", code="already_installed") from None

    return installation, _with_query(redirect_uri, code=raw_code, state=state)


# --------------------------------------------------------------------------- #
# Token endpoint
# --------------------------------------------------------------------------- #
def _authenticate_client(client_id: str, client_secret: str) -> App:
    app = App.objects.filter(client_id=client_id or "").first()
    if app is None or not app.check_client_secret(client_secret):
        raise OAuthError("invalid_client", "Client authentication failed.", status=401)
    return app


def _verify_pkce(code: AuthorizationCode, verifier: str) -> None:
    if not code.code_challenge:
        return
    if not verifier:
        raise OAuthError("invalid_grant", "code_verifier is required.")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    if not hmac.compare_digest(expected, code.code_challenge):
        raise OAuthError("invalid_grant", "code_verifier does not match code_challenge.")


def _issue_tokens(installation: Installation) -> TokenPair:
    access, refresh = new_token("aat"), new_token("art")
    now = timezone.now()
    AccessToken.objects.create(
        installation=installation,
        token_hash=hash_secret(access),
        refresh_token_hash=hash_secret(refresh),
        scopes=installation.scopes,
        expires_at=now + timedelta(seconds=settings.OAUTH_ACCESS_TOKEN_TTL),
        refresh_expires_at=now + timedelta(seconds=settings.OAUTH_REFRESH_TOKEN_TTL),
    )
    return TokenPair(access, refresh, settings.OAUTH_ACCESS_TOKEN_TTL, installation.scopes, installation.pk)


def exchange_code(*, client_id, client_secret, code, redirect_uri, code_verifier="") -> TokenPair:
    result = _exchange_code(client_id, client_secret, code, redirect_uri, code_verifier)
    if isinstance(result, OAuthError):
        raise result  # raised outside the transaction so the replay revocation is committed
    return result


@transaction.atomic
def _exchange_code(client_id, client_secret, code, redirect_uri, code_verifier) -> TokenPair | OAuthError:
    app = _authenticate_client(client_id, client_secret)
    auth_code = (
        AuthorizationCode.objects.select_for_update()
        .select_related("installation__app")
        .filter(code_hash=hash_secret(code or ""))
        .first()
    )
    if auth_code is None or auth_code.installation.app_id != app.pk:
        raise OAuthError("invalid_grant", "Authorization code is invalid.")
    if auth_code.used_at is not None:
        # Code replay: per RFC 6749 §4.1.2 revoke everything issued from it.
        AccessToken.objects.filter(installation=auth_code.installation, revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
        return OAuthError("invalid_grant", "Authorization code has already been used.")
    if auth_code.is_expired:
        raise OAuthError("invalid_grant", "Authorization code has expired.")
    if auth_code.redirect_uri != redirect_uri:
        raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request.")
    _verify_pkce(auth_code, code_verifier)

    installation = auth_code.installation
    if installation.status == Installation.Status.UNINSTALLED:
        raise OAuthError("invalid_grant", "Installation was cancelled.")

    auth_code.used_at = timezone.now()
    auth_code.save(update_fields=["used_at"])
    if installation.status == Installation.Status.PENDING:
        installation.status = Installation.Status.ACTIVE
        installation.activated_at = timezone.now()
        installation.save(update_fields=["status", "activated_at"])
    return _issue_tokens(installation)


@transaction.atomic
def refresh(*, client_id, client_secret, refresh_token) -> TokenPair:
    app = _authenticate_client(client_id, client_secret)
    token = (
        AccessToken.objects.select_for_update()
        .select_related("installation")
        .filter(refresh_token_hash=hash_secret(refresh_token or ""), installation__app=app)
        .first()
    )
    if (
        token is None
        or token.revoked_at is not None
        or token.refresh_expires_at <= timezone.now()
        or not token.installation.is_active
    ):
        raise OAuthError("invalid_grant", "Refresh token is invalid or expired.")
    token.revoked_at = timezone.now()  # rotation: each refresh token works once
    token.save(update_fields=["revoked_at"])
    return _issue_tokens(token.installation)


def revoke(*, client_id, client_secret, token: str) -> None:
    """RFC 7009: always succeeds for an authenticated client, even for unknown tokens."""
    app = _authenticate_client(client_id, client_secret)
    hashed = hash_secret(token or "")
    AccessToken.objects.filter(installation__app=app, revoked_at__isnull=True).filter(
        Q(token_hash=hashed) | Q(refresh_token_hash=hashed)
    ).update(revoked_at=timezone.now())


# --------------------------------------------------------------------------- #
# Uninstall
# --------------------------------------------------------------------------- #
@transaction.atomic
def uninstall(installation: Installation) -> Installation:
    installation = Installation.objects.select_for_update().get(pk=installation.pk)
    if installation.status == Installation.Status.UNINSTALLED:
        raise Conflict("App is already uninstalled.", code="already_uninstalled")

    was_active = installation.is_active
    now = timezone.now()
    AccessToken.objects.filter(installation=installation, revoked_at__isnull=True).update(revoked_at=now)

    WebhookDelivery.objects.filter(installation=installation, status=WebhookDelivery.Status.PENDING).update(
        status=WebhookDelivery.Status.CANCELLED, last_error="App uninstalled."
    )

    installation.status = Installation.Status.UNINSTALLED
    installation.uninstalled_at = now
    installation.save(update_fields=["status", "uninstalled_at"])

    if was_active:
        # Final notification so the app can clean up its side.
        dispatcher.publish_to(
            installation,
            "app.uninstalled",
            {"installation_id": installation.pk, "app": installation.app.slug},
        )
    return installation
