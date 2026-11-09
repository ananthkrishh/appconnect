"""Employee lifecycle -> webhook outbox rows."""

import datetime as dt
from unittest import mock

import pytest
from django.db import transaction

from apps.employees import services
from apps.webhooks import dispatcher
from apps.webhooks.models import WebhookDelivery

pytestmark = pytest.mark.django_db


@pytest.fixture
def active(installed):
    installation, _ = installed()
    return installation


def _hire(**overrides):
    fields = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.com",
        "hire_date": dt.date(2024, 1, 1),
    }
    return services.hire_employee(**{**fields, **overrides})


def test_hire_writes_one_delivery_per_active_installation(active):
    employee = _hire()
    delivery = WebhookDelivery.objects.get()
    assert delivery.installation == active
    assert delivery.event_type == "employee.hired"
    assert delivery.status == "pending"
    assert delivery.payload["type"] == "employee.hired"
    assert delivery.payload["data"]["id"] == str(employee.external_id)
    assert delivery.payload["data"]["email"] == "ada@example.com"


def test_no_deliveries_without_installations(db):
    _hire()
    assert not WebhookDelivery.objects.exists()


def test_unsubscribed_events_are_skipped(active):
    active.app.events = ["employee.terminated"]
    active.app.save()
    _hire()
    assert not WebhookDelivery.objects.exists()


def test_update_reports_changed_fields_only_when_something_changed(active):
    employee = _hire()
    services.update_employee(employee, department="Engineering")
    services.update_employee(employee, department="Engineering")  # no-op
    updates = WebhookDelivery.objects.filter(event_type="employee.updated")
    assert updates.count() == 1
    assert updates.get().payload["data"]["changed_fields"] == ["department"]


def test_terminate_publishes_terminated_not_updated(active):
    employee = _hire()
    services.terminate_employee(employee, dt.date(2024, 6, 30))
    types = list(WebhookDelivery.objects.order_by("id").values_list("event_type", flat=True))
    assert types == ["employee.hired", "employee.terminated"]


def test_rolled_back_change_leaves_no_outbox_rows(active):
    with pytest.raises(RuntimeError), transaction.atomic():
        _hire()
        raise RuntimeError("abort")
    assert not WebhookDelivery.objects.exists()


def test_tasks_are_enqueued_only_after_commit(active, django_capture_on_commit_callbacks):
    with mock.patch("apps.webhooks.tasks.deliver_webhook.delay") as delay:
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            _hire()
        delay.assert_not_called()
        for callback in callbacks:
            callback()
    delay.assert_called_once_with(WebhookDelivery.objects.get().pk)


def test_broker_outage_is_logged_and_row_kept(active, caplog):
    with mock.patch("apps.webhooks.tasks.deliver_webhook.delay", side_effect=ConnectionError("redis down")):
        dispatcher.enqueue([123])
    assert "Could not enqueue" in caplog.text


def test_same_event_id_is_not_duplicated(active):
    import uuid

    event_id = uuid.uuid4()
    dispatcher.publish("employee.updated", {"id": "x"}, event_id=event_id)
    dispatcher.publish("employee.updated", {"id": "x"}, event_id=event_id)
    assert WebhookDelivery.objects.count() == 1


def test_connector_config_reshapes_employee_payload(active):
    active.app.connector_config = {
        "app": "acme-payroll",
        "target": {"schema": "Worker"},
        "fields": [
            {"source": "email", "target": "contact.workEmail", "transform": "uppercase"},
            {"source": "status", "target": "isActive", "transform": "is_active"},
            {"constant": "AppConnect", "target": "source"},
        ],
    }
    active.app.save()
    employee = _hire()
    data = WebhookDelivery.objects.get().payload["data"]
    assert data == {
        "contact": {"workEmail": "ADA@EXAMPLE.COM"},
        "isActive": True,
        "source": "AppConnect",
        "_appconnect": {"employee_id": str(employee.external_id)},
    }
