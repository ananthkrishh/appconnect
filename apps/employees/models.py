import uuid

from django.db import models
from django.db.models import F, Q


class Employee(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        ON_LEAVE = "on_leave", "On leave"
        TERMINATED = "terminated", "Terminated"

    class EmploymentType(models.TextChoices):
        FULL_TIME = "full_time", "Full time"
        PART_TIME = "part_time", "Part time"
        CONTRACTOR = "contractor", "Contractor"

    # Public, non-guessable id used by v2 and in webhook payloads.
    external_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    department = models.CharField(max_length=100, blank=True)
    job_title = models.CharField(max_length=150, blank=True)
    employment_type = models.CharField(max_length=20, choices=EmploymentType.choices, default=EmploymentType.FULL_TIME)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    hire_date = models.DateField()
    termination_date = models.DateField(null=True, blank=True)
    manager = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="reports")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["department"]),
            models.Index(fields=["updated_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(termination_date__isnull=True) | Q(termination_date__gte=F("hire_date")),
                name="employee_termination_after_hire",
            ),
            models.CheckConstraint(
                condition=~Q(status="terminated") | Q(termination_date__isnull=False),
                name="employee_terminated_has_date",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} <{self.email}>"

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()
