#!/usr/bin/env python
"""Detect breaking changes between two OpenAPI 3 documents.

Used in CI to stop a pull request that would break existing v2 clients::

    python scripts/openapi_diff.py base.yaml head.yaml --prefix /api/v2/

Exit code 1 if any breaking change is found. What counts as breaking, from a
client's point of view:

* an operation or path disappears, or a success status code is dropped
* a request gains a new required parameter or body field
* a parameter or request field is removed (clients still send it)
* a response field is removed, changes type, or becomes nullable
* a request field narrows (type change, enum value removed)

Additions (new paths, optional params, new response fields) are reported as
non-breaking.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

HTTP_METHODS = ("get", "put", "post", "patch", "delete", "head", "options")
MAX_DEPTH = 12


@dataclass
class Report:
    breaking: list[str] = field(default_factory=list)
    additive: list[str] = field(default_factory=list)

    def br(self, msg: str) -> None:
        self.breaking.append(msg)

    def add(self, msg: str) -> None:
        self.additive.append(msg)


def load(path: str | Path) -> dict:
    text = Path(path).read_text()
    return json.loads(text) if str(path).endswith(".json") else yaml.safe_load(text)


class Resolver:
    def __init__(self, spec: dict):
        self.spec = spec

    def __call__(self, node, seen: frozenset = frozenset()):
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if ref in seen:
                return {}
            seen = seen | {ref}
            target = self.spec
            for part in ref.lstrip("#/").split("/"):
                target = target[part]
            node = target
        if isinstance(node, dict) and "allOf" in node:
            merged: dict = {"type": "object", "properties": {}, "required": []}
            for part in node["allOf"]:
                part = self(part, seen)
                merged["properties"].update(part.get("properties", {}))
                merged["required"] += part.get("required", [])
                for key in ("nullable", "enum", "items"):
                    if key in part:
                        merged[key] = part[key]
                if "type" in part and not part.get("properties"):
                    merged["type"] = part["type"]
            for key, value in node.items():
                if key != "allOf":
                    merged[key] = value
            return merged
        return node or {}


def _type(schema: dict) -> str | None:
    if "type" in schema:
        return schema["type"] if isinstance(schema["type"], str) else "|".join(sorted(schema["type"]))
    if "properties" in schema:
        return "object"
    return None


def _nullable(schema: dict) -> bool:
    return bool(schema.get("nullable")) or (isinstance(schema.get("type"), list) and "null" in schema["type"])


def compare_schema(old, new, ro: Resolver, rn: Resolver, where: str, direction: str, rep: Report, depth=0):
    """direction: 'request' (client sends) or 'response' (client reads)."""
    if depth > MAX_DEPTH:
        return
    old, new = ro(old), rn(new)
    if not old or not new:
        return

    to, tn = _type(old), _type(new)
    if to and tn and to != tn:
        rep.br(f"{where}: type changed {to} -> {tn}")
        return

    if direction == "response" and not _nullable(old) and _nullable(new):
        rep.br(f"{where}: response value became nullable")
    if direction == "request" and _nullable(old) and not _nullable(new):
        rep.br(f"{where}: request value no longer accepts null")

    old_enum, new_enum = set(map(str, old.get("enum", []))), set(map(str, new.get("enum", [])))
    if old_enum and new_enum:
        removed, added = old_enum - new_enum, new_enum - old_enum
        if removed and direction == "request":
            rep.br(f"{where}: enum values no longer accepted: {sorted(removed)}")
        elif removed:
            rep.add(f"{where}: enum values no longer returned: {sorted(removed)}")
        if added and direction == "response":
            rep.add(f"{where}: new enum values may be returned: {sorted(added)}")

    old_props, new_props = old.get("properties", {}), new.get("properties", {})
    old_req, new_req = set(old.get("required", [])), set(new.get("required", []))
    for name in sorted(set(old_props) | set(new_props)):
        path = f"{where}.{name}"
        if name in old_props and name not in new_props:
            if direction == "response":
                rep.br(f"{path}: response field removed")
            elif not ro(old_props[name]).get("readOnly"):
                rep.br(f"{path}: request field removed")
        elif name in new_props and name not in old_props:
            if direction == "request" and name in new_req:
                rep.br(f"{path}: new required request field")
            else:
                rep.add(f"{path}: field added")
        else:
            if direction == "request" and name in new_req and name not in old_req:
                rep.br(f"{path}: request field became required")
            if direction == "response" and name in old_req and name not in new_req:
                rep.br(f"{path}: response field no longer guaranteed")
            compare_schema(old_props[name], new_props[name], ro, rn, path, direction, rep, depth + 1)

    if "items" in old and "items" in new:
        compare_schema(old["items"], new["items"], ro, rn, f"{where}[]", direction, rep, depth + 1)


def _params(op: dict, path_item: dict, resolve: Resolver) -> dict[tuple[str, str], dict]:
    params = {}
    for raw in [*path_item.get("parameters", []), *op.get("parameters", [])]:
        p = resolve(raw)
        params[(p["in"], p["name"])] = p
    return params


def _json_schema(container: dict, resolve: Resolver) -> dict | None:
    container = resolve(container or {})
    content = container.get("content", {})
    for media, body in content.items():
        if "json" in media:
            return body.get("schema")
    return None


def compare_operation(key: str, old: dict, new: dict, old_item, new_item, ro, rn, rep: Report) -> None:
    old_params, new_params = _params(old, old_item, ro), _params(new, new_item, rn)
    for loc_name in sorted(set(old_params) | set(new_params)):
        label = f"{key} {loc_name[0]} param '{loc_name[1]}'"
        if loc_name not in new_params:
            rep.br(f"{label}: removed")
        elif loc_name not in old_params:
            (rep.br if new_params[loc_name].get("required") else rep.add)(
                f"{label}: added{' as required' if new_params[loc_name].get('required') else ''}"
            )
        else:
            o, n = old_params[loc_name], new_params[loc_name]
            if n.get("required") and not o.get("required"):
                rep.br(f"{label}: became required")
            compare_schema(o.get("schema", {}), n.get("schema", {}), ro, rn, label, "request", rep)

    old_body, new_body = _json_schema(old.get("requestBody"), ro), _json_schema(new.get("requestBody"), rn)
    if old_body and new_body:
        compare_schema(old_body, new_body, ro, rn, f"{key} request", "request", rep)
    elif new_body and not old_body and ro(new.get("requestBody", {})).get("required"):
        rep.br(f"{key}: now requires a request body")

    old_resp, new_resp = old.get("responses", {}), new.get("responses", {})
    for code in sorted(old_resp):
        if not str(code).startswith("2"):
            continue
        if code not in new_resp:
            rep.br(f"{key}: success response {code} removed")
            continue
        o, n = _json_schema(old_resp[code], ro), _json_schema(new_resp[code], rn)
        if o and n:
            compare_schema(o, n, ro, rn, f"{key} response {code}", "response", rep)
        elif o and not n:
            rep.br(f"{key}: response {code} no longer has a JSON body")


def diff(old_spec: dict, new_spec: dict, prefix: str = "") -> Report:
    rep = Report()
    ro, rn = Resolver(old_spec), Resolver(new_spec)
    old_paths = {p: v for p, v in old_spec.get("paths", {}).items() if p.startswith(prefix)}
    new_paths = {p: v for p, v in new_spec.get("paths", {}).items() if p.startswith(prefix)}

    for path in sorted(set(old_paths) | set(new_paths)):
        if path not in new_paths:
            rep.br(f"{path}: path removed")
            continue
        if path not in old_paths:
            rep.add(f"{path}: path added")
            continue
        old_item, new_item = old_paths[path], new_paths[path]
        for method in HTTP_METHODS:
            key = f"{method.upper()} {path}"
            if method in old_item and method not in new_item:
                rep.br(f"{key}: operation removed")
            elif method in new_item and method not in old_item:
                rep.add(f"{key}: operation added")
            elif method in old_item:
                compare_operation(key, old_item[method], new_item[method], old_item, new_item, ro, rn, rep)
    return rep


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("base", help="OpenAPI document clients rely on today (e.g. from main)")
    parser.add_argument("head", help="OpenAPI document produced by this change")
    parser.add_argument("--prefix", default="", help="Only compare paths starting with this prefix")
    args = parser.parse_args(argv)

    rep = diff(load(args.base), load(args.head), args.prefix)
    for line in rep.additive:
        print(f"  + {line}")
    for line in rep.breaking:
        print(f"  ! BREAKING {line}")
    if rep.breaking:
        print(f"\n{len(rep.breaking)} breaking change(s). Ship them in a new API version instead.")
        return 1
    print(f"\nNo breaking changes ({len(rep.additive)} additive).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
