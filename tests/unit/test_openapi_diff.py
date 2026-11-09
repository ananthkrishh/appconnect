import copy
import json

import pytest

from scripts.openapi_diff import diff, main

BASE = {
    "openapi": "3.0.3",
    "paths": {
        "/api/v2/things/": {
            "get": {
                "parameters": [{"in": "query", "name": "status", "schema": {"type": "string"}}],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"type": "array", "items": {"$ref": "#/components/schemas/Thing"}}
                            }
                        }
                    }
                },
            },
            "post": {
                "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/ThingIn"}}}},
                "responses": {
                    "201": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}}}
                },
            },
        },
        "/api/v1/old/": {"get": {"responses": {"200": {"description": "ok"}}}},
    },
    "components": {
        "schemas": {
            "Thing": {
                "type": "object",
                "required": ["id", "name"],
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "kind": {"type": "string", "enum": ["a", "b"]},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
            },
            "ThingIn": {
                "allOf": [{"$ref": "#/components/schemas/Base"}],
                "properties": {
                    "kind": {"type": "string", "enum": ["a", "b"]},
                    "note": {"type": "string", "nullable": True},
                },
            },
            "Base": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}},
        }
    },
}


def changed(fn):
    spec = copy.deepcopy(BASE)
    fn(spec)
    return diff(BASE, spec, prefix="/api/v2/")


S = lambda spec: spec["components"]["schemas"]  # noqa: E731
GET = lambda spec: spec["paths"]["/api/v2/things/"]["get"]  # noqa: E731


def test_identical_specs_have_no_changes():
    rep = diff(BASE, BASE)
    assert rep.breaking == rep.additive == []


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda s: s["paths"].pop("/api/v2/things/"), "path removed"),
        (lambda s: s["paths"]["/api/v2/things/"].pop("post"), "operation removed"),
        (lambda s: S(s)["Thing"]["properties"].pop("name"), "response field removed"),
        (lambda s: S(s)["Thing"]["properties"]["id"].update(type="integer"), "type changed string -> integer"),
        (lambda s: S(s)["Thing"]["properties"]["name"].update(nullable=True), "became nullable"),
        (lambda s: S(s)["Thing"]["required"].remove("name"), "no longer guaranteed"),
        (lambda s: S(s)["Thing"]["properties"]["tags"]["items"].update(type="integer"), "tags[]: type changed"),
        (lambda s: S(s)["ThingIn"]["properties"]["kind"]["enum"].remove("b"), "no longer accepted"),
        (lambda s: S(s)["ThingIn"].setdefault("required", []).append("kind"), "became required"),
        (
            lambda s: (
                S(s)["ThingIn"]["properties"].update(extra={"type": "string"})
                or S(s)["ThingIn"].update(required=["extra"])
            ),
            "new required request field",
        ),
        (lambda s: S(s)["ThingIn"]["properties"].pop("kind"), "request field removed"),
        (lambda s: S(s)["ThingIn"]["properties"]["note"].pop("nullable"), "no longer accepts null"),
        (lambda s: GET(s)["parameters"].clear(), "param 'status': removed"),
        (lambda s: GET(s)["parameters"][0].update(required=True), "became required"),
        (lambda s: GET(s)["parameters"].append({"in": "query", "name": "q", "required": True}), "added as required"),
        (lambda s: GET(s)["responses"].update({"201": GET(s)["responses"].pop("200")}), "success response 200 removed"),
        (lambda s: GET(s)["responses"]["200"].pop("content"), "no longer has a JSON body"),
    ],
)
def test_breaking_changes(mutation, message):
    rep = changed(mutation)
    assert any(message in b for b in rep.breaking), rep.breaking


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda s: s["paths"].update({"/api/v2/new/": {"get": {"responses": {}}}}), "path added"),
        (lambda s: s["paths"]["/api/v2/things/"].update(patch={"responses": {}}), "operation added"),
        (lambda s: S(s)["Thing"]["properties"].update(extra={"type": "string"}), "field added"),
        (lambda s: S(s)["Thing"]["properties"]["kind"]["enum"].append("c"), "new enum values"),
        (lambda s: S(s)["Thing"]["properties"]["kind"]["enum"].remove("b"), "no longer returned"),
        (lambda s: GET(s)["parameters"].append({"in": "query", "name": "q"}), "param 'q': added"),
    ],
)
def test_additive_changes_do_not_break(mutation, message):
    rep = changed(mutation)
    assert rep.breaking == []
    assert any(message in a for a in rep.additive), rep.additive


def test_prefix_ignores_other_versions():
    assert changed(lambda s: s["paths"].pop("/api/v1/old/")).breaking == []


def test_main_exit_codes(tmp_path, capsys):
    base = tmp_path / "base.json"
    base.write_text(json.dumps(BASE))
    head = tmp_path / "head.json"
    broken = copy.deepcopy(BASE)
    broken["paths"].pop("/api/v2/things/")
    head.write_text(json.dumps(broken))
    assert main([str(base), str(base)]) == 0
    assert main([str(base), str(head), "--prefix", "/api/v2/"]) == 1
    assert "BREAKING" in capsys.readouterr().out
