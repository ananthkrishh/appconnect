"""End-to-end: marketplace install over OAuth -> HR changes -> signed webhooks -> uninstall.

Runs the real Celery task (eagerly), the real outbox and real HTTP client code;
only the third-party app's HTTP endpoint is faked with respx.
"""

import json
from urllib.parse import urlencode

import httpx
import pytest
import respx

from apps.webhooks.models import WebhookDelivery
from apps.webhooks.signing import SIGNATURE_HEADER, verify_signature
from config.celery import app as celery_app
from tests.conftest import REDIRECT_URI, WEBHOOK_URL, code_from, pkce_pair

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

CONNECTOR = {
    "app": "acme-payroll",
    "target": {"schema": "WorkerCreate", "operation": "POST /workers"},
    "fields": [
        {"source": "id", "target": "externalId"},
        {"source": "first_name", "target": "givenName"},
        {"source": "last_name", "target": "familyName"},
        {"source": "email", "target": "contact.workEmail", "transform": "lowercase"},
        {"source": "status", "target": "isActive", "transform": "is_active"},
    ],
}


@pytest.fixture(autouse=True)
def eager_celery():
    celery_app.conf.task_always_eager = True
    yield
    celery_app.conf.task_always_eager = False


class Receiver:
    """Fake third-party webhook endpoint: verifies signatures and dedupes on Idempotency-Key."""

    def __init__(self, secret: str):
        self.secret = secret
        self.events: list[dict] = []
        self.seen_keys: set[str] = set()
        self.fail_next = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fail_next:
            self.fail_next -= 1
            return httpx.Response(503)
        if not verify_signature(self.secret, request.headers[SIGNATURE_HEADER], request.content):
            return httpx.Response(401)
        key = request.headers["Idempotency-Key"]
        if key not in self.seen_keys:
            self.seen_keys.add(key)
            self.events.append(json.loads(request.content))
        return httpx.Response(200)


@respx.mock
def test_marketplace_install_to_uninstall(staff_api, api, app_api, registered_app):
    registered_app.connector_config = CONNECTOR
    registered_app.save()

    # 1. Admin discovers and installs the app (consent step, PKCE).
    assert staff_api.get("/api/v2/apps/").json()["results"][0]["slug"] == "acme-payroll"
    verifier, challenge = pkce_pair()
    install = staff_api.post(
        "/api/v2/apps/acme-payroll/install/",
        {"redirect_uri": REDIRECT_URI, "state": "s1", "code_challenge": challenge, "code_challenge_method": "S256"},
        format="json",
    ).json()
    receiver = Receiver(install["webhook_secret"])
    respx.post(WEBHOOK_URL).mock(side_effect=receiver)

    # 2. App backend exchanges the code for tokens.
    tokens = api.post(
        "/oauth/token/",
        urlencode(
            {
                "grant_type": "authorization_code",
                "client_id": registered_app.client_id,
                "client_secret": registered_app.raw_secret,
                "code": code_from(install["redirect_url"]),
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            }
        ),
        content_type="application/x-www-form-urlencoded",
    ).json()
    app_client = app_api(tokens["access_token"])

    # 3. HR hires someone -> employee.hired webhook, reshaped by the connector.
    hire = staff_api.post(
        "/api/v2/employees/",
        {"first_name": "Grace", "last_name": "Hopper", "email": "Grace@Example.com", "hire_date": "2024-02-01"},
        format="json",
    ).json()
    assert [e["type"] for e in receiver.events] == ["employee.hired"]
    assert receiver.events[0]["data"]["contact"] == {"workEmail": "grace@example.com"}
    assert receiver.events[0]["data"]["externalId"] == hire["id"]

    # 4. The app reads employees with its token.
    listed = app_client.get("/api/v2/employees/").json()["results"]
    assert [e["id"] for e in listed] == [hire["id"]]

    # 5. A failed delivery is retried until it lands; the receiver sees it once.
    receiver.fail_next = 2
    staff_api.patch(f"/api/v2/employees/{hire['id']}/", {"department": "Navy"}, format="json")
    update = WebhookDelivery.objects.get(event_type="employee.updated")
    assert (update.status, update.attempts) == ("succeeded", 3)
    assert [e["type"] for e in receiver.events][-1] == "employee.updated"

    # 6. Termination.
    staff_api.post(f"/api/v2/employees/{hire['id']}/terminate/", {}, format="json")
    assert receiver.events[-1]["type"] == "employee.terminated"
    assert receiver.events[-1]["data"]["isActive"] is False

    # 7. Uninstall: token dies, the app gets a final app.uninstalled event.
    staff_api.delete(f"/api/v2/installations/{install['installation']['id']}/")
    assert receiver.events[-1]["type"] == "app.uninstalled"
    assert app_client.get("/api/v2/employees/").status_code == 401

    # Nothing left pending, nothing delivered twice.
    assert not WebhookDelivery.objects.filter(status="pending").exists()
    assert len(receiver.events) == len(receiver.seen_keys) == 4


@respx.mock
def test_exhausted_delivery_is_recovered_by_replay_job(staff_api, installed, settings):
    from django.core.management import call_command

    settings.WEBHOOK_MAX_ATTEMPTS = 2
    installation, _ = installed()
    receiver = Receiver(installation.webhook_secret)
    receiver.fail_next = 2
    respx.post(WEBHOOK_URL).mock(side_effect=receiver)

    staff_api.post(
        "/api/v2/employees/",
        {"first_name": "Alan", "last_name": "Turing", "email": "alan@example.com", "hire_date": "2024-02-01"},
        format="json",
    )
    delivery = WebhookDelivery.objects.get()
    assert delivery.status == "failed"
    assert receiver.events == []

    # Receiver recovers; the scheduled job replays with the same idempotency key.
    call_command("replay_failed_webhooks")
    delivery.refresh_from_db()
    assert (delivery.status, delivery.replay_count) == ("succeeded", 1)
    assert receiver.seen_keys == {str(delivery.idempotency_key)}
