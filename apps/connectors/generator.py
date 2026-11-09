"""Draft a connector config for a third-party app from its OpenAPI spec.

Pipeline:

1. Parse the spec and locate the "create person" request schema.
2. Ask Llama 3 (via Ollama, JSON-schema constrained) to map canonical employee
   fields onto that schema.
3. Validate the draft: JSON Schema for shape, then semantic checks against the
   spec. If it fails, send the errors back to the model once to repair it.
4. If the LLM is unavailable or still wrong, fall back to the deterministic
   heuristic mapper so the command always produces a valid starting point.

The output is a reviewed-before-merge JSON config, never executable code.
"""

import json
import logging
from dataclasses import dataclass, field

from .config_schema import CANONICAL_FIELDS, CONNECTOR_CONFIG_SCHEMA, validate_config
from .heuristic import heuristic_mapping
from .llm import LLMUnavailable, OllamaClient
from .spec_parser import TargetSchema, find_target_schema, load_spec

logger = logging.getLogger(__name__)

# Ollama compiles the response schema into a sampling grammar; it does not need the dialect marker.
LLM_RESPONSE_SCHEMA = {k: v for k, v in CONNECTOR_CONFIG_SCHEMA.items() if k != "$schema"}

SYSTEM_PROMPT = """You map an HR system's employee record onto a third-party app's API schema.

Source fields (canonical employee, all strings; dates are YYYY-MM-DD):
{sources}

Transforms you may use:
- none: copy as-is
- lowercase / uppercase: change case
- iso_date: YYYY-MM-DD string
- us_date: MM/DD/YYYY string
- is_active: true when status == "active", else false (use for boolean "active"-style targets)
- map: translate values with a "values" table (required for enum targets; keys are source values)

Rules:
- Only use target paths from the provided list, each at most once.
- Map every required target. If no source fits a required target, use a "constant".
- Never invent source fields. Put targets you cannot map in "unmapped".
- Give each mapping a confidence between 0 and 1.
- Set "app" to "{app}" and "target" to {target}.
Return only JSON matching the response schema."""


@dataclass
class GenerationResult:
    config: dict
    source: str  # "llm", "llm-repaired" or "heuristic"
    target: TargetSchema
    warnings: list[str] = field(default_factory=list)


def _messages(app_slug: str, target: TargetSchema) -> list[dict]:
    sources = "\n".join(f"- {name} ({kind})" for name, kind in CANONICAL_FIELDS.items())
    target_ref = json.dumps({"schema": target.name, "operation": target.operation})
    return [
        {"role": "system", "content": SYSTEM_PROMPT.format(sources=sources, app=app_slug, target=target_ref)},
        {
            "role": "user",
            "content": "Target fields (dotted path -> type info):\n" + json.dumps(target.fields, indent=2),
        },
    ]


def _clean(config: dict) -> dict:
    """Drop no-op noise models like to emit (transform 'none', empty values tables)."""
    for item in config.get("fields", []):
        if item.get("transform") == "none":
            item.pop("transform")
        if item.get("values") == {}:
            item.pop("values")
    return config


def generate_connector(
    spec_source,
    app_slug: str,
    *,
    schema_name: str | None = None,
    client: OllamaClient | None = None,
    use_llm: bool = True,
    max_repairs: int = 1,
) -> GenerationResult:
    spec = load_spec(spec_source)
    target = find_target_schema(spec, schema_name)
    warnings: list[str] = []

    if use_llm:
        client = client or OllamaClient()
        messages = _messages(app_slug, target)
        try:
            for attempt in range(max_repairs + 1):
                draft = _clean(client.chat_json(messages, LLM_RESPONSE_SCHEMA))
                errors = validate_config(draft, target.fields)
                if not errors:
                    return GenerationResult(draft, "llm" if attempt == 0 else "llm-repaired", target, warnings)
                logger.info("LLM draft invalid (attempt %s): %s", attempt + 1, errors)
                messages += [
                    {"role": "assistant", "content": json.dumps(draft)},
                    {
                        "role": "user",
                        "content": "That config is invalid:\n- " + "\n- ".join(errors) + "\nReturn a corrected config.",
                    },
                ]
            warnings.append("LLM output failed validation after repair: " + "; ".join(errors))
        except LLMUnavailable as exc:
            warnings.append(str(exc))

    config = heuristic_mapping(app_slug, target.name, target.operation, target.fields)
    problems = validate_config(config, target.fields)
    if problems:
        warnings.extend(f"needs review: {p}" for p in problems)
    return GenerationResult(config, "heuristic", target, warnings)
