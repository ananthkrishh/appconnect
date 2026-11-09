import base64
import datetime as dt
import hashlib
import secrets
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.cache import cache
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from apps.employees.models import Employee
from apps.marketplace import oauth
from apps.marketplace.models import App

REDIRECT_URI = "https://partner.example.com/oauth/callback"
WEBHOOK_URL = "https://partner.example.com/webhooks"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()  # throttling state
    yield


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def staff_user(django_user_model):
    return django_user_model.objects.create_user("admin", password="pw", is_staff=True)


@pytest.fixture
def staff_api(staff_user):
    client = APIClient()
    token, _ = Token.objects.get_or_create(user=staff_user)
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


@pytest.fixture
def make_employee(db):
    counter = iter(range(1, 10_000))

    def factory(**overrides):
        n = next(counter)
        fields = {
            "first_name": f"First{n}",
            "last_name": f"Last{n}",
            "email": f"person{n}@example.com",
            "department": "Engineering",
            "job_title": "Engineer",
            "hire_date": dt.date(2023, 1, 1),
        }
        fields.update(overrides)
        return Employee.objects.create(**fields)

    return factory


@pytest.fixture
def make_app(db):
    def factory(**overrides):
        fields = {
            "slug": "acme-payroll",
            "name": "Acme Payroll",
            "developer": "Acme",
            "redirect_uris": [REDIRECT_URI],
            "webhook_url": WEBHOOK_URL,
            "scopes": ["employees:read", "employees:write"],
        }
        fields.update(overrides)
        return App.register(**fields)

    return factory


@pytest.fixture
def registered_app(make_app):
    app, secret = make_app()
    app.raw_secret = secret
    return app


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def code_from(redirect_url: str) -> str:
    return parse_qs(urlsplit(redirect_url).query)["code"][0]


@pytest.fixture
def installed(registered_app, staff_user):
    """An active installation plus a live token pair for it."""

    def factory(scopes=None, app=None):
        target = app or registered_app
        installation, url = oauth.start_install(
            target, staff_user, redirect_uri=REDIRECT_URI, scopes=scopes or ["employees:read"]
        )
        pair = oauth.exchange_code(
            client_id=target.client_id,
            client_secret=target.raw_secret,
            code=code_from(url),
            redirect_uri=REDIRECT_URI,
        )
        installation.refresh_from_db()
        return installation, pair

    return factory


@pytest.fixture
def app_api():
    def factory(access_token: str):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
        return client

    return factory
