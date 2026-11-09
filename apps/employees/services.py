"""Employee lifecycle operations.

Views never save employees directly; they go through these functions so every
hire, update and termination publishes exactly one domain event in the same
database transaction as the change (transactional outbox, see
``apps.webhooks.dispatcher``).
"""

import datetime as dt

from django.db import transaction
from django.utils import timezone

from apps.core.exceptions import InvalidStateTransition
from apps.webhooks import dispatcher

from .models import Employee

EVENT_HIRED = "employee.hired"
EVENT_UPDATED = "employee.updated"
EVENT_TERMINATED = "employee.terminated"
EMPLOYEE_EVENTS = (EVENT_HIRED, EVENT_UPDATED, EVENT_TERMINATED)

# Fields whose change is worth telling installed apps about.
_TRACKED_FIELDS = (
    "first_name",
    "last_name",
    "email",
    "department",
    "job_title",
    "employment_type",
    "status",
    "hire_date",
    "termination_date",
    "manager_id",
)


def employee_payload(employee: Employee) -> dict:
    """Canonical event representation (mirrors the v2 API shape)."""
    return {
        "id": str(employee.external_id),
        "first_name": employee.first_name,
        "last_name": employee.last_name,
        "full_name": employee.full_name,
        "email": employee.email,
        "department": employee.department,
        "job_title": employee.job_title,
        "employment_type": employee.employment_type,
        "status": employee.status,
        "hire_date": employee.hire_date.isoformat() if employee.hire_date else None,
        "termination_date": employee.termination_date.isoformat() if employee.termination_date else None,
        "manager_id": str(employee.manager.external_id) if employee.manager_id else None,
    }


@transaction.atomic
def hire_employee(**fields) -> Employee:
    employee = Employee.objects.create(**fields)
    dispatcher.publish(EVENT_HIRED, employee_payload(employee))
    return employee


@transaction.atomic
def update_employee(employee: Employee, **changes) -> Employee:
    locked = Employee.objects.select_for_update().get(pk=employee.pk)
    before = {f: getattr(locked, f) for f in _TRACKED_FIELDS}

    if locked.status == Employee.Status.TERMINATED and changes.get("status") not in (
        None,
        Employee.Status.TERMINATED,
    ):
        raise InvalidStateTransition("Terminated employees cannot be reactivated; rehire instead.")

    becoming_terminated = (
        changes.get("status") == Employee.Status.TERMINATED and locked.status != Employee.Status.TERMINATED
    )
    if becoming_terminated and not changes.get("termination_date"):
        changes["termination_date"] = timezone.localdate()

    for field, value in changes.items():
        setattr(locked, field, value)
    locked.full_clean()  # also enforces the model's CheckConstraints
    locked.save()

    changed = sorted(f for f in _TRACKED_FIELDS if getattr(locked, f) != before[f])
    if becoming_terminated:
        dispatcher.publish(EVENT_TERMINATED, employee_payload(locked))
    elif changed:
        payload = employee_payload(locked)
        payload["changed_fields"] = [f.removesuffix("_id") if f == "manager_id" else f for f in changed]
        dispatcher.publish(EVENT_UPDATED, payload)
    return locked


def terminate_employee(employee: Employee, termination_date: dt.date | None = None) -> Employee:
    if employee.status == Employee.Status.TERMINATED:
        raise InvalidStateTransition("Employee is already terminated.")
    return update_employee(
        employee,
        status=Employee.Status.TERMINATED,
        termination_date=termination_date or timezone.localdate(),
    )
