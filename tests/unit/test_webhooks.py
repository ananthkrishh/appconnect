import json
import random
import uuid
from datetime import timedelta
from io import StringIO
from unittest import mock

import httpx
import pytest
import respx
from celery.exceptions import Retry
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from apps.webhooks import signing
from apps.webhooks.delivery import attempt_delivery, build_request, compute_backoff
from apps.webhooks.dispatcher import publish
from apps.webhooks.models import WebhookDelivery
from apps.webhooks.tasks import deliver_webhook
from tests.conftest import WEBHOOK_URL

# --------------------------------------------------------------------- signing


def test_signature_roundtrip():
    body = b'{"a":1}'
    header = signing.build_signature_header("whsec_x", body, timestamp=1_700_000_000)
    assert header.startswith("t=1700000000,v1=")
    assert signing.verify_signature("whsec_x", header, body, now=1_700_000_100)


@pytest.mark.parametrize(
    ("secret", "body", "now"),
    [
        ("whsec_other", b'{"a":1}', 1_700_000_000),  # wrong secret
        ("whsec_x", b'{"a":2}', 1_700_000_000),  # tampered body
        ("whsec_x", b'{"a":1}', 1_700_000_301),  # outside tolerance (replay)
    ],
)
def test_signature_rejections(secret, body, now):
    header = signing.build_signature_header("whsec_x", b'{"a":1}', timestamp=1_700_000_000)
    assert not signing.verify_signature(secret, header, body, now=now)


@pytest.mark.parametrize("header", ["", "t=abc,v1=", "v1=deadbeef", "garbage"])
def test_malformed_signature_headers(header):
    assert not signing.verify_signature("s", header, b"{}")


def test_multiple_signatures_allow_secret_rotation():
    body = b"{}"
    old = signing.compute_signature("old", 100, body)
    new = signing.compute_signature("new", 100, body)
    header = f"t=100,v1={old},v1={new}"
    assert signing.verify_signature("new", header, body, now=100)
    assert signing.verify_signature("old", header, body, now=100)


# --------------------------------------------------------------------- backoff


def test_backoff_grows_exponentially_and_is_capped():
    rng = random.Random(1)
    delays = [compute_backoff(n, base=5, cap=600, rng=rng) for n in range(1, 10)]
    assert 2 <= delays[0] <= 5
    assert 20 <= delays[3] <= 40
    assert all(d <= 600 for d in delays)
    assert delays[-1] >= 300


# -------------------------------------------------------------------- delivery


@pytest.fixture
def delivery(installed, make_employee):
    from apps.employees.services import update_employee

    installation, _ = installed()
    update_employee(make_employee(), department="Ops")
    return WebhookDelivery.objects.get(installation=installation)


@pytest.mark.django_db
@respx.mock
def test_successful_delivery_is_signed_and_idempotent(delivery):
    route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(204))
    result = attempt_delivery(delivery.pk)
    assert result.status == "succeeded"

    request = route.calls.last.request
    assert request.headers["Idempotency-Key"] == str(delivery.idempotency_key)
    assert request.headers["X-AppConnect-Event"] == "employee.updated"
    secret = delivery.installation.webhook_secret
    assert signing.verify_signature(secret, request.headers[signing.SIGNATURE_HEADER], request.content)
    assert json.loads(request.content)["data"]["changed_fields"] == ["department"]

    delivery.refresh_from_db()
    assert (delivery.attempts, delivery.last_status_code) == (1, 204)
    assert delivery.delivered_at is not None
    # Already delivered -> no second request.
    assert attempt_delivery(delivery.pk).status == "succeeded"
    assert route.call_count == 1


@pytest.mark.django_db
@respx.mock
def test_server_error_schedules_retry(delivery):
    respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(503, text="busy"))
    result = attempt_delivery(delivery.pk)
    assert result.status == "pending"
    assert result.retry_in >= 1
    delivery.refresh_from_db()
    assert delivery.next_attempt_at > timezone.now()
    assert "HTTP 503" in delivery.last_error


@pytest.mark.django_db
@respx.mock
def test_429_honours_retry_after(delivery):
    respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "42"}))
    assert attempt_delivery(delivery.pk).retry_in == 42


@pytest.mark.django_db
@respx.mock
def test_client_error_fails_without_retry(delivery):
    respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(422, text="bad payload"))
    result = attempt_delivery(delivery.pk)
    assert (result.status, result.retry_in) == ("failed", None)


@pytest.mark.django_db
@respx.mock
@pytest.mark.parametrize("exc", [httpx.ConnectTimeout("slow"), httpx.ConnectError("refused")])
def test_network_errors_are_retried(delivery, exc):
    respx.post(WEBHOOK_URL).mock(side_effect=exc)
    result = attempt_delivery(delivery.pk)
    assert result.status == "pending"
    assert result.retry_in is not None


@pytest.mark.django_db
@respx.mock
@override_settings(WEBHOOK_MAX_ATTEMPTS=2)
def test_attempts_are_capped(delivery):
    respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(500))
    assert attempt_delivery(delivery.pk).status == "pending"
    assert attempt_delivery(delivery.pk).status == "failed"
    delivery.refresh_from_db()
    assert delivery.attempts == 2
    assert delivery.next_attempt_at is None


@pytest.mark.django_db
def test_inactive_installation_cancels_delivery(delivery):
    delivery.installation.status = "uninstalled"
    delivery.installation.save()
    assert attempt_delivery(delivery.pk).status == "cancelled"


@pytest.mark.django_db
@respx.mock
def test_app_uninstalled_event_is_still_delivered(delivery, staff_api):
    route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200))
    staff_api.delete(f"/api/v2/installations/{delivery.installation_id}/")
    final = WebhookDelivery.objects.get(event_type="app.uninstalled")
    assert attempt_delivery(final.pk).status == "succeeded"
    assert json.loads(route.calls.last.request.content)["data"]["app"] == "acme-payroll"


@pytest.mark.django_db
def test_missing_delivery():
    assert attempt_delivery(999_999).status == "missing"


@pytest.mark.django_db
def test_build_request_body_is_canonical_json(delivery):
    body, headers = build_request(delivery, timestamp=1)
    assert body == json.dumps(delivery.payload, separators=(",", ":"), sort_keys=True).encode()
    assert headers[signing.SIGNATURE_HEADER].startswith("t=1,")


@pytest.mark.django_db
def test_task_retries_via_celery(delivery):
    from apps.webhooks.delivery import DeliveryResult

    with (
        mock.patch("apps.webhooks.tasks.attempt_delivery", return_value=DeliveryResult("pending", retry_in=30)),
        mock.patch.object(deliver_webhook, "retry", side_effect=Retry()) as retry,
        pytest.raises(Retry),
    ):
        deliver_webhook.run(delivery.pk)
    retry.assert_called_once_with(countdown=30)

    with mock.patch("apps.webhooks.tasks.attempt_delivery", return_value=DeliveryResult("succeeded")):
        assert deliver_webhook.run(delivery.pk) == "succeeded"


# ---------------------------------------------------------------------- replay


@pytest.fixture
def failed_delivery(delivery):
    delivery.status = WebhookDelivery.Status.FAILED
    delivery.attempts = 8
    delivery.save()
    return delivery


@pytest.mark.django_db
def test_replay_command_requeues_failed_and_stuck(failed_delivery):
    stuck = publish("employee.updated", {"id": "x"}, event_id=uuid.uuid4())[0]
    WebhookDelivery.objects.filter(pk=stuck.pk).update(next_attempt_at=timezone.now() - timedelta(hours=1))
    fresh = publish("employee.updated", {"id": "y"}, event_id=uuid.uuid4())[0]

    out = StringIO()
    call_command("replay_failed_webhooks", "--dry-run", stdout=out)
    assert "Would replay 2" in out.getvalue()

    with mock.patch("apps.webhooks.tasks.deliver_webhook.delay") as delay:
        out = StringIO()
        call_command("replay_failed_webhooks", "-v", "2", stdout=out)
    assert "Replayed 2" in out.getvalue()
    failed_delivery.refresh_from_db()
    assert failed_delivery.status == "pending"
    assert (failed_delivery.attempts, failed_delivery.replay_count) == (0, 1)
    assert fresh.pk not in [c.args[0] for c in delay.call_args_list]


@pytest.mark.django_db
def test_replay_skips_uninstalled_installations(failed_delivery):
    failed_delivery.installation.status = "uninstalled"
    failed_delivery.installation.save()
    out = StringIO()
    call_command("replay_failed_webhooks", "--no-stale", stdout=out)
    assert "Replayed 0" in out.getvalue()


@pytest.mark.django_db
def test_replay_api(staff_api, failed_delivery, delivery):
    url = f"/api/v2/webhook-deliveries/{failed_delivery.pk}/replay/"
    with mock.patch("apps.webhooks.tasks.deliver_webhook.delay"):
        response = staff_api.post(url)
    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    assert response.json()["idempotency_key"] == str(failed_delivery.idempotency_key)
    assert staff_api.post(url).json()["error"]["code"] == "not_replayable"


@pytest.mark.django_db
def test_replay_api_refuses_inactive_installation(staff_api, failed_delivery):
    failed_delivery.installation.status = "uninstalled"
    failed_delivery.installation.save()
    response = staff_api.post(f"/api/v2/webhook-deliveries/{failed_delivery.pk}/replay/")
    assert response.status_code == 409


@pytest.mark.django_db
def test_delivery_log_filters(staff_api, delivery):
    body = staff_api.get("/api/v2/webhook-deliveries/?status=pending&event_type=employee.updated").json()
    assert body["count"] == 1
    assert body["results"][0]["app"] == "acme-payroll"
    assert staff_api.get("/api/v2/webhook-deliveries/?status=failed").json()["count"] == 0
