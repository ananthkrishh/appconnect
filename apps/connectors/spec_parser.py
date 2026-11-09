"""Read a third-party OpenAPI 3 spec and find the object we should send employees as."""

import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_PERSON_HINT = re.compile(r"employee|worker|person|people|user|member|staff|hire", re.I)
_MAX_DEPTH = 3


@dataclass(frozen=True)
class TargetSchema:
    name: str
    operation: str  # e.g. "POST /workers"
    fields: dict[str, dict]  # dotted path -> {"type", "format", "enum", "required", "description"}


def load_spec(source: str | Path | dict) -> dict:
    if isinstance(source, dict):
        spec = source
    else:
        text = Path(source).read_text()
        spec = json.loads(text) if str(source).endswith(".json") else yaml.safe_load(text)
    if not isinstance(spec, dict) or "paths" not in spec:
        raise ValueError("Not an OpenAPI document: missing 'paths'.")
    return spec


def _resolve(spec: dict, node: dict, seen: frozenset = frozenset()) -> dict:
    ref = node.get("$ref") if isinstance(node, dict) else None
    if not ref:
        return node
    if ref in seen:
        return {}
    if not ref.startswith("#/"):
        raise ValueError(f"External $ref not supported: {ref}")
    target = spec
    for part in ref[2:].split("/"):
        target = target[part]
    return _resolve(spec, target, seen | {ref})


def _merge_all_of(spec: dict, schema: dict) -> dict:
    schema = _resolve(spec, schema)
    if "allOf" not in schema:
        return schema
    merged = {"type": "object", "properties": {}, "required": []}
    for part in schema["allOf"]:
        part = _merge_all_of(spec, part)
        merged["properties"].update(part.get("properties", {}))
        merged["required"] += part.get("required", [])
    merged["properties"].update(schema.get("properties", {}))
    merged["required"] += schema.get("required", [])
    return merged


def flatten_schema(spec: dict, schema: dict, prefix: str = "", parent_required: bool = True, depth: int = 0):
    """Flatten nested object properties into dotted paths (read-only fields skipped)."""
    schema = _merge_all_of(spec, schema)
    required = set(schema.get("required", []))
    fields: dict[str, dict] = {}
    for name, prop in schema.get("properties", {}).items():
        prop = _merge_all_of(spec, prop)
        if prop.get("readOnly"):
            continue
        path = f"{prefix}{name}"
        is_required = parent_required and name in required
        if prop.get("type") == "object" or "properties" in prop:
            if depth < _MAX_DEPTH:
                fields.update(flatten_schema(spec, prop, f"{path}.", is_required, depth + 1))
            continue
        fields[path] = {
            k: v
            for k, v in {
                "type": prop.get("type", "string"),
                "format": prop.get("format"),
                "enum": prop.get("enum"),
                "required": is_required,
                "description": (prop.get("description") or "")[:200] or None,
            }.items()
            if v not in (None, False) or k == "required"
        }
    return fields


def _request_schema(operation: dict) -> dict | None:
    content = (operation.get("requestBody") or {}).get("content", {})
    for media in ("application/json", *content.keys()):
        if media in content and "schema" in content[media]:
            return content[media]["schema"]
    return None


def find_target_schema(spec: dict, schema_name: str | None = None) -> TargetSchema:
    """Pick the create-person request body.

    With ``schema_name`` the named component is used. Otherwise the best
    ``POST`` operation whose path or schema name looks like a person is chosen.
    """
    candidates: list[tuple[int, str, str, dict]] = []
    for path, item in spec.get("paths", {}).items():
        operation = item.get("post")
        if not operation:
            continue
        raw = _request_schema(operation)
        if raw is None:
            continue
        ref_name = raw.get("$ref", "").rsplit("/", 1)[-1] or f"{path} body"
        if schema_name and ref_name != schema_name:
            continue
        score = 2 * bool(_PERSON_HINT.search(ref_name)) + bool(_PERSON_HINT.search(path))
        candidates.append((score, ref_name, f"POST {path}", raw))

    if schema_name and not candidates:
        component = spec.get("components", {}).get("schemas", {}).get(schema_name)
        if component is None:
            raise ValueError(f"Schema '{schema_name}' not found in spec.")
        return TargetSchema(schema_name, "", flatten_schema(spec, component))

    candidates.sort(key=lambda c: c[0], reverse=True)
    if not candidates or (candidates[0][0] == 0 and not schema_name):
        raise ValueError("Could not find a POST operation that creates a person; pass the schema name explicitly.")
    _, name, operation, raw = candidates[0]
    return TargetSchema(name, operation, flatten_schema(spec, raw))
