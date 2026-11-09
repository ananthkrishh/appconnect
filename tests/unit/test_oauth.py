from datetime import timedelta
from urllib.parse import urlencode

import pytest
from django.utils import timezone

from apps.marketplace import oauth
from apps.marketplace.models import AccessToken, AuthorizationCode, Installation
from apps.webhooks.models import WebhookDelivery
from tests.conftest import REDIRECT_URI, code_from, pkce_pair

pytestmark = pytest.mark.django_db


FORM = "application/x-www-form-urlencoded"


def _form(api, url, data):
    """OAuth clients send form-encoded bodies (RFC 6749 §4.1.3)."""
    return api.post(url, urlencode(data), content_type=FORM)


def _token(api, app, **data):
    return _form(api, "/oauth/token/", {"client_id": app.client_id, "client_secret": app.raw_secret, **data})


# --------------------------------------------------------------------- discover
def test_staff_can_browse_marketplace(staff_api, registered_app, make_app):
    make_app(slug="hidden", name="Hidden", is_published=False)
    body = staff_api.get("/api/v2/apps/").json()
    assert [a["slug"] for a in body["results"]] == ["acme-payroll"]
    assert body["results"][0]["installed"] is False
    detail = staff_api.get("/api/v2/apps/acme-payroll/").json()
    assert detail["scopes"] == ["employees:read", "employees:write"]
    assert staff_api.get("/api/v2/apps/?search=payroll").json()["results"]


# ---------------------------------------------------------------- full grant
def test_install_and_exchange_code_with_pkce(staff_api, api, registered_app):
    verifier, challenge = pkce_pair()
    response = staff_api.post(
        "/api/v2/apps/acme-payroll/install/",
        {
            "redirect_uri": REDIRECT_URI,
            "scopes": ["employees:read"],
            "state": "xyz",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
        format="json",
    )
    assert response.status_code == 201
    body = response.json()
    assert body["installation"]["status"] == "pending"
    assert body["redirect_url"].startswith(REDIRECT_URI)
    assert "state=xyz" in body["redirect_url"]
    assert body["webhook_secret"].startswith("whsec_")

    code = code_from(body["redirect_url"])
    bad = _token(api, registered_app, grant_type="authorization_code", code=code, redirect_uri=REDIRECT_URI)
    assert bad.status_code == 400
    assert bad.json() == {"error": "invalid_grant", "error_description": "code_verifier is required."}

    ok = _token(
        api,
        registered_app,
        grant_type="authorization_code",
        code=code,
        redirect_uri=REDIRECT_URI,
        code_verifier=verifier,
    )
    assert ok.status_code == 200, ok.json()
    tokens = ok.json()
    assert tokens["token_type"] == "Bearer"
    assert tokens["scope"] == "employees:read"
    assert ok["Cache-Control"] == "no-store"
    assert Installation.objects.get().status == "active"
    assert staff_api.get("/api/v2/apps/acme-payroll/").json()["installed"] is True


def test_wrong_pkce_verifier_is_rejected(registered_app, staff_user):
    _, challenge = pkce_pair()
    _, url = oauth.start_install(
        registered_app, staff_user, redirect_uri=REDIRECT_URI, code_challenge=challenge, code_challenge_method="S256"
    )
    with pytest.raises(oauth.OAuthError, match="does not match"):
        oauth.exchange_code(
            client_id=registered_app.client_id,
            client_secret=registered_app.raw_secret,
            code=code_from(url),
            redirect_uri=REDIRECT_URI,
            code_verifier="wrong",
        )


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"redirect_uri": "https://evil.example.com/cb"}, "invalid_request"),
        ({"redirect_uri": REDIRECT_URI, "code_challenge": "abc"}, "invalid_request"),
    ],
)
def test_install_rejects_bad_requests(staff_api, registered_app, payload, code):
    response = staff_api.post("/api/v2/apps/acme-payroll/install/", payload, format="json")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == code


def test_install_rejects_scope_app_did_not_request(staff_api, make_app):
    make_app(slug="reader", scopes=["employees:read"])
    response = staff_api.post(
        "/api/v2/apps/reader/install/", {"redirect_uri": REDIRECT_URI, "scopes": ["employees:write"]}, format="json"
    )
    assert response.json()["error"]["code"] == "invalid_scope"


def test_installing_twice_conflicts_but_pending_is_replaced(staff_api, installed, registered_app, staff_user):
    oauth.start_install(registered_app, staff_user, redirect_uri=REDIRECT_URI)
    oauth.start_install(registered_app, staff_user, redirect_uri=REDIRECT_URI)  # replaces pending
    assert Installation.objects.filter(status="pending").count() == 1

    Installation.objects.all().delete()
    installed()
    response = staff_api.post("/api/v2/apps/acme-payroll/install/", {"redirect_uri": REDIRECT_URI}, format="json")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_installed"


def test_code_reuse_revokes_issued_tokens(registered_app, staff_user):
    _, url = oauth.start_install(registered_app, staff_user, redirect_uri=REDIRECT_URI)
    args = {
        "client_id": registered_app.client_id,
        "client_secret": registered_app.raw_secret,
        "code": code_from(url),
        "redirect_uri": REDIRECT_URI,
    }
    oauth.exchange_code(**args)
    with pytest.raises(oauth.OAuthError, match="already been used"):
        oauth.exchange_code(**args)
    assert not AccessToken.objects.filter(revoked_at__isnull=True).exists()


def test_expired_code_and_mismatched_redirect(registered_app, staff_user):
    _, url = oauth.start_install(registered_app, staff_user, redirect_uri=REDIRECT_URI)
    args = {"client_id": registered_app.client_id, "client_secret": registered_app.raw_secret, "code": code_from(url)}
    with pytest.raises(oauth.OAuthError, match="redirect_uri"):
        oauth.exchange_code(**args, redirect_uri="https://other.example.com")
    AuthorizationCode.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(oauth.OAuthError, match="expired"):
        oauth.exchange_code(**args, redirect_uri=REDIRECT_URI)


def test_invalid_client_gets_401(api, registered_app):
    response = _form(
        api,
        "/oauth/token/",
        {"grant_type": "authorization_code", "client_id": registered_app.client_id, "client_secret": "nope"},
    )
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_client"
    assert "WWW-Authenticate" in response


def test_token_endpoint_validates_parameters(api):
    response = _form(api, "/oauth/token/", {"grant_type": "password"})
    assert response.json()["error"] == "invalid_request"


def test_code_from_another_client_is_rejected(registered_app, make_app, staff_user):
    other, other_secret = make_app(slug="other")
    _, url = oauth.start_install(registered_app, staff_user, redirect_uri=REDIRECT_URI)
    with pytest.raises(oauth.OAuthError, match="invalid"):
        oauth.exchange_code(
            client_id=other.client_id, client_secret=other_secret, code=code_from(url), redirect_uri=REDIRECT_URI
        )


# ------------------------------------------------------------ refresh/revoke
def test_refresh_rotates_tokens(api, installed, registered_app):
    _, pair = installed()
    first = _token(api, registered_app, grant_type="refresh_token", refresh_token=pair.refresh_token)
    assert first.status_code == 200
    replay = _token(api, registered_app, grant_type="refresh_token", refresh_token=pair.refresh_token)
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


def test_revoke_endpoint(api, app_api, installed, registered_app):
    _, pair = installed()
    assert app_api(pair.access_token).get("/api/v2/employees/").status_code == 200
    response = _form(
        api,
        "/oauth/revoke/",
        {"client_id": registered_app.client_id, "client_secret": registered_app.raw_secret, "token": pair.access_token},
    )
    assert response.status_code == 200
    assert app_api(pair.access_token).get("/api/v2/employees/").status_code == 401
    assert api.post("/oauth/revoke/", {}, format="json").json()["error"] == "invalid_request"
    bad_client = _form(api, "/oauth/revoke/", {"client_id": "x", "client_secret": "y", "token": "z"})
    assert bad_client.status_code == 401


# --------------------------------------------------------------- app tokens
def test_scopes_are_enforced_for_app_tokens(app_api, installed, make_employee):
    employee = make_employee()
    _, pair = installed(scopes=["employees:read"])
    client = app_api(pair.access_token)
    assert client.get("/api/v2/employees/").status_code == 200
    denied = client.patch(f"/api/v2/employees/{employee.external_id}/", {"department": "X"}, format="json")
    assert denied.status_code == 403
    assert "employees:write" in denied.json()["error"]["message"]


def test_write_scope_allows_updates(app_api, installed, make_employee):
    employee = make_employee()
    _, pair = installed(scopes=["employees:read", "employees:write"])
    response = app_api(pair.access_token).patch(
        f"/api/v2/employees/{employee.external_id}/", {"department": "Ops"}, format="json"
    )
    assert response.status_code == 200


def test_app_tokens_cannot_use_marketplace_admin_endpoints(app_api, installed):
    _, pair = installed()
    client = app_api(pair.access_token)
    assert client.get("/api/v2/apps/").status_code == 403
    assert client.get("/api/v2/installations/").status_code == 403


def test_expired_or_malformed_bearer_tokens(app_api, api, installed):
    _, pair = installed()
    AccessToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert app_api(pair.access_token).get("/api/v2/employees/").status_code == 401
    api.credentials(HTTP_AUTHORIZATION="Bearer a b")
    assert api.get("/api/v2/employees/").status_code == 401


# ------------------------------------------------------------------ uninstall
def test_uninstall_revokes_tokens_cancels_pending_and_notifies(staff_api, app_api, installed, make_employee):
    installation, pair = installed()
    from apps.employees.services import update_employee

    update_employee(make_employee(), department="Ops")  # leaves a pending delivery
    response = staff_api.delete(f"/api/v2/installations/{installation.pk}/")
    assert response.status_code == 204

    installation.refresh_from_db()
    assert installation.status == "uninstalled"
    assert app_api(pair.access_token).get("/api/v2/employees/").status_code == 401
    statuses = dict(WebhookDelivery.objects.values_list("event_type", "status"))
    assert statuses == {"employee.updated": "cancelled", "app.uninstalled": "pending"}

    again = staff_api.delete(f"/api/v2/installations/{installation.pk}/")
    assert again.status_code == 409


def test_installations_list_filters(staff_api, installed):
    installed()
    body = staff_api.get("/api/v2/installations/?status=active&app=acme-payroll").json()
    assert body["count"] == 1
    assert body["results"][0]["installed_by"] == "admin"
