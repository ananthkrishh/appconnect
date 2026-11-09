import pytest
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework.test import APIRequestFactory

from apps.core.exceptions import exception_handler
from apps.core.views import not_found, server_error

pytestmark = pytest.mark.django_db


def test_unauthenticated_request_uses_standard_error_envelope(api):
    response = api.get("/api/v2/employees/", HTTP_X_REQUEST_ID="trace-123")
    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "code": "not_authenticated",
            "message": "Authentication credentials were not provided.",
            "details": {},
            "request_id": "trace-123",
        }
    }
    assert response["X-Request-ID"] == "trace-123"


def test_request_id_is_generated_when_missing_or_invalid(api):
    response = api.get("/healthz/", HTTP_X_REQUEST_ID="bad id with spaces")
    assert response.status_code == 200
    assert len(response["X-Request-ID"]) == 32


def test_validation_errors_are_nested_under_details(staff_api):
    response = staff_api.post("/api/v2/employees/", {"first_name": "A"}, format="json")
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert set(error["details"]) >= {"last_name", "email", "hire_date"}


@pytest.mark.parametrize("bad_id", ["abc", "00000000-0000-0000-0000-000000000000", "123"])
def test_malformed_or_unknown_ids_are_404(staff_api, bad_id):
    response = staff_api.get(f"/api/v2/employees/{bad_id}/")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_v1_responses_carry_deprecation_headers(staff_api):
    response = staff_api.get("/api/v1/employees/")
    assert response.status_code == 200
    assert response["Deprecation"] == "true"
    assert "Sunset" in response
    assert "successor-version" in response["Link"]
    assert "Deprecation" not in staff_api.get("/api/v2/employees/")


def test_marketplace_does_not_exist_in_v1(staff_api):
    assert staff_api.get("/api/v1/apps/").status_code == 404


def test_healthz(api):
    assert api.get("/healthz/").json() == {"status": "ok", "database": True}


def _ctx():
    request = APIRequestFactory().get("/")
    request.request_id = "rid"
    return {"request": request}


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (Http404(), 404, "not_found"),
        (PermissionDenied(), 403, "permission_denied"),
        (DjangoValidationError({"email": ["bad"]}), 400, "validation_error"),
        (DjangoValidationError("bad"), 400, "validation_error"),
        (RuntimeError("boom"), 500, "internal_error"),
    ],
)
def test_exception_handler_normalises_django_and_unknown_errors(exc, status, code):
    response = exception_handler(exc, _ctx())
    assert response.status_code == status
    assert response.data["error"]["code"] == code
    assert response.data["error"]["request_id"] == "rid"


def test_json_404_and_500_handlers():
    request = APIRequestFactory().get("/nope")
    assert not_found(request).status_code == 404
    assert server_error(request).status_code == 500


def test_schema_endpoints_serve_each_version(staff_api):
    v1 = staff_api.get("/api/v1/schema/", HTTP_ACCEPT="application/json").json()
    v2 = staff_api.get("/api/v2/schema/", HTTP_ACCEPT="application/json").json()
    assert set(v1["paths"]) == {"/api/v1/employees/", "/api/v1/employees/{id}/"}
    assert "/api/v2/apps/{id}/install/" in v2["paths"]
    assert staff_api.get("/api/v2/docs/").status_code == 200
