import json
from io import StringIO
from pathlib import Path

import httpx
import pytest
import respx
from django.core.management import CommandError, call_command
from django.test import override_settings

from apps.connectors.config_schema import validate_config
from apps.connectors.generator import generate_connector
from apps.connectors.heuristic import heuristic_mapping
from apps.connectors.llm import LLMUnavailable, OllamaClient
from apps.connectors.spec_parser import find_target_schema, load_spec
from apps.connectors.transform import apply_mapping

SPEC = Path(__file__).resolve().parents[2] / "examples" / "specs" / "acme_payroll.yaml"
OLLAMA = "http://ollama.test"

GOOD_CONFIG = {
    "app": "acme-payroll",
    "target": {"schema": "WorkerCreate", "operation": "POST /workers"},
    "fields": [
        {"source": "id", "target": "externalId", "confidence": 0.95},
        {"source": "first_name", "target": "givenName"},
        {"source": "last_name", "target": "familyName"},
        {"source": "email", "target": "contact.workEmail", "transform": "lowercase"},
        {"source": "hire_date", "target": "startDate", "transform": "iso_date"},
        {"source": "status", "target": "isActive", "transform": "is_active"},
        {
            "source": "employment_type",
            "target": "workerType",
            "transform": "map",
            "values": {"full_time": "EMPLOYEE_FT", "part_time": "EMPLOYEE_PT", "contractor": "CONTRACTOR"},
        },
    ],
    "unmapped": ["costCenter"],
}


@pytest.fixture
def target():
    return find_target_schema(load_spec(SPEC))


def _ollama_reply(content) -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(200, json={"message": {"role": "assistant", "content": text}})


# ------------------------------------------------------------------ spec parse
def test_finds_person_create_schema_and_flattens(target):
    assert target.name == "WorkerCreate"
    assert target.operation == "POST /workers"
    assert target.fields["contact.workEmail"]["required"] is True
    assert target.fields["contact.phone"]["required"] is False
    assert target.fields["workerType"]["enum"] == ["EMPLOYEE_FT", "EMPLOYEE_PT", "CONTRACTOR"]
    assert "id" not in target.fields  # readOnly fields are skipped


def test_explicit_schema_name_and_component_fallback():
    spec = load_spec(SPEC)
    assert find_target_schema(spec, "PayGroup").operation == "POST /pay-groups"
    worker = find_target_schema(spec, "Worker")  # not a request body: resolved via components + allOf
    assert worker.operation == ""
    assert "givenName" in worker.fields
    with pytest.raises(ValueError, match="not found"):
        find_target_schema(spec, "Nope")


def test_spec_errors(tmp_path):
    with pytest.raises(ValueError, match="paths"):
        load_spec({"openapi": "3.0.0"})
    no_person = {"paths": {"/invoices": {"post": {"requestBody": {"content": {"application/json": {"schema": {}}}}}}}}
    with pytest.raises(ValueError, match="person"):
        find_target_schema(no_person)
    json_spec = tmp_path / "spec.json"
    json_spec.write_text(json.dumps({"paths": {}}))
    assert load_spec(json_spec) == {"paths": {}}


def test_cyclic_and_external_refs():
    spec = {
        "paths": {
            "/users": {
                "post": {
                    "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/User"}}}}
                }
            }
        },
        "components": {
            "schemas": {
                "User": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "boss": {"$ref": "#/components/schemas/User"}},
                }
            }
        },
    }
    fields = find_target_schema(spec).fields
    assert "name" in fields
    assert "boss.boss.boss.name" in fields
    assert "boss.boss.boss.boss.name" not in fields  # recursion is depth limited
    spec["components"]["schemas"]["User"]["properties"]["x"] = {"$ref": "other.yaml#/X"}
    with pytest.raises(ValueError, match="External"):
        find_target_schema(spec)


# ------------------------------------------------------------------ validation
def test_good_config_is_valid(target):
    assert validate_config(GOOD_CONFIG, target.fields) == []


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda c: c["fields"].append({"source": "salary", "target": "x"}), "is not one of"),
        (lambda c: c["fields"].append({"source": "email", "target": "nope"}), "does not exist"),
        (lambda c: c["fields"].append({"source": "email", "target": "givenName"}), "more than once"),
        (lambda c: c["fields"].pop(0), "required target fields not mapped: externalId"),
        (lambda c: c["fields"][6].pop("values"), "values"),
        (lambda c: c["fields"][6].update(transform="none"), "enum"),
        (lambda c: c["fields"][5].update(transform="none"), "expects boolean"),
        (lambda c: c["fields"].append({"target": "costCenter"}), "is not valid under any"),
    ],
)
def test_invalid_configs(target, mutate, expected):
    config = json.loads(json.dumps(GOOD_CONFIG))
    mutate(config)
    errors = validate_config(config, target.fields)
    assert any(expected in e for e in errors), errors


def test_constant_types_are_checked(target):
    config = json.loads(json.dumps(GOOD_CONFIG))
    config["fields"][5] = {"constant": True, "target": "isActive"}
    config["fields"].append({"constant": 7, "target": "costCenter"})
    assert validate_config(config, target.fields) == [
        "fields/7: 'costCenter' expects string but mapping produces number"
    ]


# ------------------------------------------------------------------ heuristic
def test_heuristic_maps_everything_but_enums(target):
    config = heuristic_mapping("acme-payroll", target.name, target.operation, target.fields)
    mapped = {f["target"]: f["source"] for f in config["fields"]}
    assert mapped["givenName"] == "first_name"
    assert mapped["contact.workEmail"] == "email"
    assert mapped["externalId"] == "id"
    assert mapped["isActive"] == "status"
    assert "workerType" in config["unmapped"]
    assert validate_config(config, target.fields) == ["required target fields not mapped: workerType"]


# ------------------------------------------------------------------ transform
def test_apply_mapping_builds_nested_payload():
    employee = {
        "id": "abc",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "Ada@Example.com",
        "hire_date": "2024-05-01",
        "status": "terminated",
        "employment_type": "part_time",
    }
    out = apply_mapping(GOOD_CONFIG, employee)
    assert out == {
        "externalId": "abc",
        "givenName": "Ada",
        "familyName": "Lovelace",
        "contact": {"workEmail": "ada@example.com"},
        "startDate": "2024-05-01",
        "isActive": False,
        "workerType": "EMPLOYEE_PT",
    }


@pytest.mark.parametrize(
    ("transform", "value", "expected"),
    [("uppercase", "ab", "AB"), ("us_date", "2024-05-01", "05/01/2024"), ("iso_date", None, None)],
)
def test_individual_transforms(transform, value, expected):
    config = {"fields": [{"source": "x", "target": "y", "transform": transform}]}
    assert apply_mapping(config, {"x": value}) == {"y": expected}


def test_unknown_transform_raises():
    with pytest.raises(ValueError):
        apply_mapping({"fields": [{"source": "x", "target": "y", "transform": "rot13"}]}, {"x": "a"})


# ------------------------------------------------------------------ LLM path
@override_settings(OLLAMA_URL=OLLAMA)
@respx.mock
def test_llm_draft_used_when_valid():
    route = respx.post(f"{OLLAMA}/api/chat").mock(return_value=_ollama_reply(GOOD_CONFIG))
    result = generate_connector(SPEC, "acme-payroll")
    assert result.source == "llm"
    assert result.config == GOOD_CONFIG
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "llama3"
    assert sent["format"]["required"] == ["app", "target", "fields"]
    assert "workerType" in sent["messages"][1]["content"]


@override_settings(OLLAMA_URL=OLLAMA)
@respx.mock
def test_invalid_llm_draft_is_repaired_with_feedback():
    broken = json.loads(json.dumps(GOOD_CONFIG))
    broken["fields"][6] = {"source": "employment_type", "target": "workerType", "transform": "none"}
    route = respx.post(f"{OLLAMA}/api/chat").mock(side_effect=[_ollama_reply(broken), _ollama_reply(GOOD_CONFIG)])
    result = generate_connector(SPEC, "acme-payroll")
    assert result.source == "llm-repaired"
    repair_prompt = json.loads(route.calls[1].request.content)["messages"][-1]["content"]
    assert "workerType" in repair_prompt
    assert "transform" not in next(f for f in result.config["fields"] if f["target"] == "externalId")


@override_settings(OLLAMA_URL=OLLAMA)
@respx.mock
def test_falls_back_to_heuristic_when_llm_keeps_failing():
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=_ollama_reply({"app": "x"}))
    result = generate_connector(SPEC, "acme-payroll")
    assert result.source == "heuristic"
    assert any("failed validation" in w for w in result.warnings)


@override_settings(OLLAMA_URL=OLLAMA)
@respx.mock
@pytest.mark.parametrize(
    "response",
    [httpx.Response(500), _ollama_reply("not json"), _ollama_reply("[1, 2]"), httpx.Response(200, json={})],
)
def test_llm_client_errors(response):
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=response)
    with pytest.raises(LLMUnavailable):
        OllamaClient().chat_json([], {})


@override_settings(OLLAMA_URL=OLLAMA)
@respx.mock
def test_unreachable_ollama_falls_back():
    respx.post(f"{OLLAMA}/api/chat").mock(side_effect=httpx.ConnectError("refused"))
    result = generate_connector(SPEC, "acme-payroll")
    assert result.source == "heuristic"
    assert any("Ollama request failed" in w for w in result.warnings)


# ------------------------------------------------------------------ command
@pytest.mark.django_db
def test_command_writes_file_and_refuses_to_save_invalid(tmp_path, registered_app):
    out_file = tmp_path / "acme.json"
    err = StringIO()
    call_command(
        "generate_connector", str(SPEC), "--app", "acme-payroll", "--no-llm", "--out", str(out_file), stderr=err
    )
    assert json.loads(out_file.read_text())["target"]["schema"] == "WorkerCreate"
    assert "via heuristic" in err.getvalue()
    with pytest.raises(CommandError, match="Refusing"):
        call_command("generate_connector", str(SPEC), "--app", "acme-payroll", "--no-llm", "--save", stderr=StringIO())


@pytest.mark.django_db
@override_settings(OLLAMA_URL=OLLAMA)
@respx.mock
def test_command_saves_valid_llm_config(registered_app):
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=_ollama_reply(GOOD_CONFIG))
    stdout = StringIO()
    call_command("generate_connector", str(SPEC), "--app", "acme-payroll", "--save", stdout=stdout, stderr=StringIO())
    registered_app.refresh_from_db()
    assert registered_app.connector_config == GOOD_CONFIG
    with pytest.raises(CommandError, match="No app"):
        call_command("generate_connector", str(SPEC), "--app", "ghost", "--save", stdout=StringIO(), stderr=StringIO())


def test_command_validate_mode_and_bad_spec(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(GOOD_CONFIG))
    out = StringIO()
    call_command("generate_connector", str(SPEC), "--app", "x", "--validate", str(good), stdout=out)
    assert "valid" in out.getvalue()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**GOOD_CONFIG, "fields": GOOD_CONFIG["fields"][1:]}))
    with pytest.raises(CommandError, match="externalId"):
        call_command("generate_connector", str(SPEC), "--app", "x", "--validate", str(bad))
    with pytest.raises(CommandError):
        call_command("generate_connector", str(tmp_path / "missing.yaml"), "--app", "x", "--no-llm")
