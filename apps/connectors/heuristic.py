"""Deterministic baseline mapper.

Used when Ollama is unreachable and as a sanity check on LLM output. It matches
target field names to canonical fields by synonym table, then by string
similarity. It never invents enum translations, so enum targets are left for
the LLM or a human.
"""

import difflib
import re

from .config_schema import CANONICAL_FIELDS

SYNONYMS: dict[str, tuple[str, ...]] = {
    "first_name": ("first_name", "firstname", "given_name", "givenname", "forename", "fname"),
    "last_name": ("last_name", "lastname", "family_name", "familyname", "surname", "lname"),
    "full_name": ("full_name", "fullname", "display_name", "displayname", "name", "legal_name"),
    "email": ("email", "work_email", "email_address", "emailaddress", "mail", "business_email"),
    "department": ("department", "dept", "team", "division", "org_unit", "business_unit"),
    "job_title": ("job_title", "jobtitle", "title", "position", "role", "designation"),
    "employment_type": ("employment_type", "worker_type", "employee_type", "contract_type"),
    "status": ("status", "employment_status", "state", "is_active", "active", "enabled"),
    "hire_date": ("hire_date", "start_date", "startdate", "date_of_joining", "joined_on", "hired_at"),
    "termination_date": ("termination_date", "end_date", "enddate", "terminated_at", "last_day"),
    "manager_id": ("manager_id", "supervisor_id", "reports_to", "line_manager_id"),
    "id": ("external_id", "externalid", "employee_id", "employeeid", "source_id", "hris_id", "remote_id"),
}


def _norm(name: str) -> str:
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()
    return re.sub(r"[^a-z0-9]+", "_", snake).strip("_")


def _match(leaf: str) -> tuple[str | None, float]:
    norm = _norm(leaf)
    squashed = norm.replace("_", "")
    for source, names in SYNONYMS.items():
        if norm in names or squashed in names:
            return source, 0.9
    best, score = None, 0.0
    for source in CANONICAL_FIELDS:
        ratio = difflib.SequenceMatcher(None, norm, source).ratio()
        if ratio > score:
            best, score = source, ratio
    return (best, round(score * 0.8, 2)) if score >= 0.8 else (None, 0.0)


def heuristic_mapping(app_slug: str, schema_name: str, operation: str, target_fields: dict[str, dict]) -> dict:
    fields, unmapped = [], []
    for path, spec in target_fields.items():
        leaf = path.rsplit(".", 1)[-1]
        source, confidence = _match(leaf)
        if source is None or spec.get("enum"):
            unmapped.append(path)
            continue
        if source == "status" and spec.get("type") == "boolean":
            entry = {"source": "status", "target": path, "transform": "is_active"}
        elif source in ("hire_date", "termination_date") and spec.get("type") == "string":
            entry = {"source": source, "target": path, "transform": "iso_date"}
        elif spec.get("type") not in (None, "string"):
            unmapped.append(path)
            continue
        else:
            entry = {"source": source, "target": path}
            if source == "email":
                entry["transform"] = "lowercase"
        entry["confidence"] = confidence
        fields.append(entry)
    return {
        "app": app_slug,
        "target": {"schema": schema_name, "operation": operation},
        "fields": fields,
        "unmapped": unmapped,
    }
