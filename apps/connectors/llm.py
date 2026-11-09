"""Minimal Ollama chat client with JSON-schema constrained output."""

import json

import httpx
from django.conf import settings


class LLMUnavailable(Exception):
    """Ollama could not be reached or returned something unusable."""


class OllamaClient:
    def __init__(self, base_url: str | None = None, model: str | None = None, timeout: float | None = None):
        self.base_url = (base_url or settings.OLLAMA_URL).rstrip("/")
        self.model = model or settings.OLLAMA_MODEL
        self.timeout = timeout or settings.OLLAMA_TIMEOUT

    def chat_json(self, messages: list[dict], schema: dict) -> dict:
        """Ask the model for a reply that conforms to ``schema`` (Ollama structured outputs)."""
        body = {
            "model": self.model,
            "messages": messages,
            "format": schema,
            "stream": False,
            "options": {"temperature": 0, "seed": 7},
        }
        try:
            response = httpx.post(f"{self.base_url}/api/chat", json=body, timeout=self.timeout)
            response.raise_for_status()
            content = response.json()["message"]["content"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise LLMUnavailable(f"Ollama request failed: {exc}") from exc
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMUnavailable(f"Model did not return JSON: {content[:200]}") from exc
        if not isinstance(parsed, dict):
            raise LLMUnavailable("Model returned JSON that is not an object.")
        return parsed
