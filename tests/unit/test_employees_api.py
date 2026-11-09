import datetime as dt

import pytest

from apps.employees.models import Employee

pytestmark = pytest.mark.django_db

NEW_HIRE = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "department": "Engineering",
    "hire_date": "2024-05-01",
}


# --------------------------------------------------------------------------- v1
def test_v1_list_is_page_numbered_with_flat_shape(staff_api, make_employee):
    for _ in range(3):
        make_employee()
    body = staff_api.get("/api/v1/employees/?page_size=2").json()
    assert body["count"] == 3
    assert len(body["results"]) == 2
    assert body["next"].endswith("page=2&page_size=2")
    assert set(body["results"][0]) == {
        "id",
        "first_name",
        "last_name",
        "email",
        "department",
        "title",
        "status",
        "hire_date",
    }
    assert isinstance(body["results"][0]["id"], int)


def test_v1_create_maps_title_to_job_title(staff_api):
    response = staff_api.post("/api/v1/employees/", {**NEW_HIRE, "title": "Analyst"}, format="json")
    assert response.status_code == 201
    assert Employee.objects.get(email="ada@example.com").job_title == "Analyst"


def test_v1_cannot_create_terminated_employee(staff_api):
    response = staff_api.post("/api/v1/employees/", {**NEW_HIRE, "status": "terminated"}, format="json")
    assert response.status_code == 400
    assert "status" in response.json()["error"]["details"]


def test_v1_patch_status_terminated_sets_termination_date(staff_api, make_employee):
    employee = make_employee()
    response = staff_api.patch(f"/api/v1/employees/{employee.pk}/", {"status": "terminated"}, format="json")
    assert response.status_code == 200
    employee.refresh_from_db()
    assert employee.status == "terminated"
    assert employee.termination_date is not None


def test_v1_ordering(staff_api, make_employee):
    make_employee(last_name="Zed")
    make_employee(last_name="Abe")
    names = [e["last_name"] for e in staff_api.get("/api/v1/employees/?ordering=last_name").json()["results"]]
    assert names == ["Abe", "Zed"]


def test_delete_is_not_allowed(staff_api, make_employee):
    employee = make_employee()
    assert staff_api.delete(f"/api/v1/employees/{employee.pk}/").status_code == 405


# --------------------------------------------------------------------------- v2
def test_v2_create_returns_uuid_and_read_only_lifecycle_fields(staff_api):
    response = staff_api.post("/api/v2/employees/", {**NEW_HIRE, "status": "terminated"}, format="json")
    assert response.status_code == 201
    body = response.json()
    employee = Employee.objects.get(email="ada@example.com")
    assert body["id"] == str(employee.external_id)
    assert body["status"] == "active"  # read-only in v2
    assert body["full_name"] == "Ada Lovelace"


def test_v2_list_uses_cursor_pagination(staff_api, make_employee):
    for _ in range(3):
        make_employee()
    first = staff_api.get("/api/v2/employees/?page_size=2").json()
    assert set(first) == {"next", "previous", "results"}
    assert "cursor=" in first["next"]
    second = staff_api.get(first["next"]).json()
    assert len(first["results"]) + len(second["results"]) == 3


def test_v2_filters_and_search(staff_api, make_employee):
    make_employee(department="Finance", employment_type="contractor")
    make_employee(department="finance", first_name="Grace")
    make_employee(department="Design", status="on_leave")

    def ids(query):
        return [e["first_name"] for e in staff_api.get(f"/api/v2/employees/?{query}").json()["results"]]

    assert len(ids("department=FINANCE")) == 2
    assert len(ids("employment_type=contractor")) == 1
    assert len(ids("status=active,on_leave")) == 3
    assert ids("search=grace") == ["Grace"]


def test_v2_updated_since_filter_rejects_garbage(staff_api, make_employee):
    make_employee()
    assert len(staff_api.get("/api/v2/employees/?updated_since=2000-01-01T00:00:00Z").json()["results"]) == 1
    response = staff_api.get("/api/v2/employees/?updated_since=yesterday")
    assert response.status_code == 400


def test_v2_terminate_action(staff_api, make_employee):
    employee = make_employee()
    url = f"/api/v2/employees/{employee.external_id}/terminate/"
    response = staff_api.post(url, {"termination_date": "2024-12-31"}, format="json")
    assert response.status_code == 200
    assert response.json()["status"] == "terminated"
    assert response.json()["termination_date"] == "2024-12-31"

    again = staff_api.post(url, {}, format="json")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "invalid_state_transition"


def test_v2_terminate_before_hire_date_is_rejected(staff_api, make_employee):
    employee = make_employee(hire_date=dt.date(2024, 6, 1))
    response = staff_api.post(
        f"/api/v2/employees/{employee.external_id}/terminate/", {"termination_date": "2024-01-01"}, format="json"
    )
    assert response.status_code == 400


def test_terminated_employee_cannot_be_reactivated_via_v1(staff_api, make_employee):
    employee = make_employee(status="terminated", termination_date=dt.date(2024, 2, 1))
    response = staff_api.patch(f"/api/v1/employees/{employee.pk}/", {"status": "active"}, format="json")
    assert response.status_code == 409


def test_v2_manager_validation(staff_api, make_employee):
    manager = make_employee()
    gone = make_employee(status="terminated", termination_date=dt.date(2024, 2, 1))
    report = make_employee()
    url = f"/api/v2/employees/{report.external_id}/"

    ok = staff_api.patch(url, {"manager_id": str(manager.external_id)}, format="json")
    assert ok.status_code == 200
    assert ok.json()["manager_id"] == str(manager.external_id)

    assert staff_api.patch(url, {"manager_id": str(report.external_id)}, format="json").status_code == 400
    assert staff_api.patch(url, {"manager_id": str(gone.external_id)}, format="json").status_code == 400


def test_non_staff_users_are_forbidden(api, django_user_model):
    user = django_user_model.objects.create_user("viewer", password="pw")
    api.force_authenticate(user)
    response = api.get("/api/v2/employees/")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
