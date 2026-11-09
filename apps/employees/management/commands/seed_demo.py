import datetime as dt

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from rest_framework.authtoken.models import Token

from apps.employees.models import Employee
from apps.marketplace.models import App

EMPLOYEES = [
    ("Priya", "Raman", "Engineering", "Engineering Manager", "full_time", dt.date(2021, 3, 1)),
    ("Marcus", "Lee", "Engineering", "Backend Engineer", "full_time", dt.date(2022, 7, 18)),
    ("Sofia", "Alvarez", "People", "HR Business Partner", "full_time", dt.date(2020, 1, 6)),
    ("Daniel", "Okafor", "Finance", "Payroll Specialist", "part_time", dt.date(2023, 5, 22)),
    ("Hannah", "Schmidt", "Design", "Contract Designer", "contractor", dt.date(2024, 2, 12)),
]


class Command(BaseCommand):
    help = "Create a demo admin, API token, employees and a marketplace app (idempotent)."

    @transaction.atomic
    def handle(self, *args, **options):
        User = get_user_model()
        admin, created = User.objects.get_or_create(
            username="admin", defaults={"is_staff": True, "is_superuser": True, "email": "admin@example.com"}
        )
        if created:
            admin.set_password("admin")
            admin.save()
        token, _ = Token.objects.get_or_create(user=admin)

        manager = None
        for first, last, dept, title, kind, hired in EMPLOYEES:
            employee, _ = Employee.objects.get_or_create(
                email=f"{first}.{last}@example.com".lower(),
                defaults={
                    "first_name": first,
                    "last_name": last,
                    "department": dept,
                    "job_title": title,
                    "employment_type": kind,
                    "hire_date": hired,
                    "manager": manager,
                },
            )
            manager = manager or employee

        self.stdout.write(self.style.SUCCESS(f"Admin API token: {token.key}"))
        if not App.objects.filter(slug="acme-payroll").exists():
            app, secret = App.register(
                slug="acme-payroll",
                name="Acme Payroll",
                description="Runs payroll for your employees.",
                developer="Acme Inc.",
                homepage_url="https://acme.example.com",
                redirect_uris=["http://localhost:9000/oauth/callback"],
                webhook_url="http://localhost:9000/webhooks",
                scopes=["employees:read"],
            )
            self.stdout.write(self.style.SUCCESS(f"App acme-payroll client_id={app.client_id}"))
            self.stdout.write(self.style.SUCCESS(f"App acme-payroll client_secret={secret} (shown once)"))
