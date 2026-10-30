"""Small async LLM backend used by the crew.

Why not call ``gemini_json_completion`` directly? Its 3-attempt backoff (3 s +
12 s) is incompatible with the 8 s p95 budget chosen for the adaptive crew.
This backend performs one fast attempt (plus a single retry only when the
remaining budget allows it) and degrades to ``None`` on any failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from nebula.core.smac_gemini import extract_json_object, gemini_schema_from_response_format

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


@dataclass
class LLMReply:
    data: dict[str, Any] | None
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None


class LLMBackend(Protocol):
    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any],
        model: str,
        timeout_s: float,
        temperature: float = 0.2,
        max_tokens: int = 1200,
        reasoning_effort: str = "low",
    ) -> LLMReply: ...


def _split_messages(messages: list[dict[str, str]]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        text = str(message.get("content") or "")
        if role == "system":
            system_parts.append(text)
            continue
        contents.append({"role": "model" if role == "assistant" else "user", "parts": [{"text": text}]})
    return "\n\n".join(system_parts), contents


def _body(
    messages: list[dict[str, str]],
    *,
    schema: dict[str, Any],
    temperature: float,
    max_tokens: int,
    reasoning_effort: str,
) -> dict[str, Any]:
    system, contents = _split_messages(messages)
    response_format = {"type": "json_schema", "json_schema": {"name": "crew_output", "schema": schema}}
    generation: dict[str, Any] = {
        "temperature": temperature,
        "maxOutputTokens": max_tokens,
        "responseMimeType": "application/json",
        "responseJsonSchema": gemini_schema_from_response_format(response_format),
    }
    body: dict[str, Any] = {"contents": contents, "generationConfig": generation}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    effort = str(reasoning_effort or "").strip().lower()
    if effort in ("low", "medium", "high"):
        body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": effort}
    return body


class GeminiBackend:
    """Gemini REST backend with a hard per-call timeout and safe degradation."""

    def __init__(self, api_key: str, *, http_client: httpx.AsyncClient | None = None):
        self._api_key = api_key or ""
        self._http_client = http_client

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any],
        model: str,
        timeout_s: float,
        temperature: float = 0.2,
        max_tokens: int = 1200,
        reasoning_effort: str = "low",
    ) -> LLMReply:
        started = time.monotonic()
        if not self._api_key:
            return LLMReply(None, 0, error="no_api_key")
        url = f"{GEMINI_BASE_URL}/models/{model}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": self._api_key}
        payload = _body(
            messages,
            schema=schema,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
        retryable_status = {429, 500, 502, 503, 504}
        for attempt in range(2):
            remaining = max(1.0, timeout_s - (time.monotonic() - started))
            try:
                if self._http_client is None:
                    async with httpx.AsyncClient(timeout=remaining) as client:
                        response = await client.post(url, headers=headers, json=payload)
                else:
                    response = await self._http_client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
                text = None
                candidates = data.get("candidates") or [] if isinstance(data, dict) else []
                if candidates:
                    parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
                    text = "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)) or None
                parsed = extract_json_object(text)
                usage = data.get("usageMetadata") or {} if isinstance(data, dict) else {}
                latency = int((time.monotonic() - started) * 1000)
                if parsed is None:
                    return LLMReply(
                        None,
                        latency,
                        input_tokens=usage.get("promptTokenCount"),
                        output_tokens=usage.get("candidatesTokenCount"),
                        error="invalid_json",
                    )
                return LLMReply(
                    parsed,
                    latency,
                    input_tokens=usage.get("promptTokenCount"),
                    output_tokens=usage.get("candidatesTokenCount"),
                )
            except Exception as e:
                response = getattr(e, "response", None)
                status = getattr(response, "status_code", None)
                transient = status in retryable_status or (
                    status is None and isinstance(e, (httpx.TimeoutException, httpx.TransportError))
                )
                budget_left = timeout_s - (time.monotonic() - started)
                if attempt == 0 and transient and budget_left > 1.5:
                    await asyncio.sleep(min(0.5, budget_left / 2))
                    continue
                latency = int((time.monotonic() - started) * 1000)
                error_type = type(e).__name__
                logging.warning(
                    "SMAC crew | Gemini call failed (status=%s, error=%s)",
                    status if status is not None else "n/a",
                    error_type,
                )
                return LLMReply(None, latency, error=f"{status or error_type}")
        return LLMReply(None, int((time.monotonic() - started) * 1000), error="retry_exhausted")


def hash_prompt(messages: list[dict[str, str]]) -> str:
    import hashlib

    blob = json.dumps(messages, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


__all__ = ["GeminiBackend", "LLMBackend", "LLMReply", "hash_prompt"]
