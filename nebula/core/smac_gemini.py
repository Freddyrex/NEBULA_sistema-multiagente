"""Shared Gemini completion helper for SMAC (REST structured outputs, httpx).

Both the node-side :class:`~nebula.core.situationalawareness.awareness.cognitive.gemini_reasoning_agent.GeminiReasoning`
agent and the frontend chatbot go through this helper, calling the official
Gemini REST API (``generateContent``)::

    POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
    Header: x-goog-api-key: <GEMINI_API_KEY>

    {
      "contents": [{"role": "user", "parts": [{"text": "..."}]}],
      "systemInstruction": {"parts": [{"text": "..."}]},
      "generationConfig": {
        "temperature": 0.2,
        "topP": 1.0,
        "maxOutputTokens": 4096,
        "responseMimeType": "application/json",
        "responseJsonSchema": {...},
        "thinkingConfig": {"thinkingLevel": "medium"}
      }
    }

Unlike the previous Groq transport, no official-SDK path is used: the Google
SDKs historically lagged the new ``AQ.`` authorization keys emitted by AI
Studio, while the documented REST contract (``x-goog-api-key``) works with
both standard (``AIza``) and authorization (``AQ.``) keys. One transport,
directly testable with ``httpx.MockTransport``.

Safe degradation contract (unchanged): every failure returns ``None`` and the
callers proceed without the LLM.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import Any

import httpx

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

#: Backoff after attempts 1 and 2 (+jitter) on throttling/overload.
GEMINI_RETRY_DELAYS = (3.0, 12.0)

#: HTTP statuses worth retrying (RESOURCE_EXHAUSTED / OVERLOADED / UNAVAILABLE).
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})

#: JSON-Schema keys the Gemini structured-output subset rejects (HTTP 400).
#: They are stripped recursively before sending; callers already validate the
#: resulting object (e.g. ``maxLength`` is enforced by _validate_decision()).
_SCHEMA_DROP_KEYS = frozenset(
    {
        "additionalProperties",
        "maxLength",
        "minLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "nullable",
        "$schema",
        "$defs",
        "definitions",
        "strict",
        "default",
    }
)


def extract_json_object(content: str | None) -> dict[str, Any] | None:
    """Robustly extract a JSON object from LLM output.

    Tries direct parsing first, then strips markdown fences/prose and retries
    with the largest ``{...}`` span. Returns None when unrecoverable (e.g.
    genuinely truncated output).
    """
    if not content or not isinstance(content, str):
        return None
    try:
        data = json.loads(content)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, ValueError):
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def gemini_schema_from_response_format(response_format: dict[str, Any] | None) -> dict[str, Any] | None:
    """Convert an OpenAI-style ``response_format`` into a Gemini JSON schema.

    Callers keep the previous wire format
    (``{"type": "json_schema", "json_schema": {"name": ..., "schema": {...}}}``)
    so no scenario config or prompt had to change; this adapter extracts the
    inner schema and strips the keys Gemini's structured-output subset rejects.
    """
    if not isinstance(response_format, dict):
        return None
    schema = response_format.get("schema")
    if schema is None:
        inner = response_format.get("json_schema")
        if isinstance(inner, dict):
            schema = inner.get("schema")
    if not isinstance(schema, dict):
        return None
    return _sanitize_schema(schema)


def _sanitize_schema(schema: Any) -> Any:
    """Recursively drop unsupported keys without dropping property names.

    ``_SCHEMA_DROP_KEYS`` contains JSON-Schema keywords such as ``pattern``.
    Under ``properties`` those same strings are user-defined field names, so
    they must be preserved (e.g. the crew's ``AgentVerdict.pattern`` field).
    """
    if isinstance(schema, dict):
        cleaned: dict[str, Any] = {}
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                cleaned[key] = {
                    name: _sanitize_schema(subschema) for name, subschema in value.items()
                }
            elif key not in _SCHEMA_DROP_KEYS:
                cleaned[key] = _sanitize_schema(value)
        return cleaned
    if isinstance(schema, list):
        return [_sanitize_schema(item) for item in schema]
    return schema


def _thinking_config(model: str, reasoning_effort: str) -> dict[str, Any] | None:
    """Map Groq-style ``reasoning_effort`` onto the Gemini thinking controls.

    Gemini 3/4 models expose ``thinkingLevel`` (``low``/``medium``/``high``);
    the 2.5 series uses ``thinkingBudget`` instead; older models have no
    thinking controls and get no config at all (dynamic default).
    """
    match = re.search(r"gemini-(\d+)(?:\.(\d+))?", model or "")
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    effort = (reasoning_effort or "medium").lower()
    if major >= 3:
        level = effort if effort in ("minimal", "low", "medium", "high") else "medium"
        return {"thinkingLevel": level}
    if major == 2 and minor >= 5:
        budget = {"low": 0, "medium": -1, "high": 24576}.get(effort, -1)
        return {"thinkingBudget": budget}
    return None


def _build_request_body(
    *,
    messages: list[dict[str, str]],
    model: str,
    response_format: dict[str, Any],
    reasoning_effort: str,
    temperature: float,
    top_p: float,
    max_completion_tokens: int,
) -> dict[str, Any]:
    """Translate the OpenAI-style chat payload into a Gemini generateContent body."""
    system_parts: list[dict[str, str]] = []
    contents: list[dict[str, Any]] = []
    for message in messages or []:
        role = (message or {}).get("role", "user")
        text = (message or {}).get("content", "") or ""
        if role == "system":
            system_parts.append({"text": text})
            continue
        contents.append({"role": "model" if role in ("assistant", "model") else "user", "parts": [{"text": text}]})
    if not contents and system_parts:
        # System-only payload: fold it into a user turn so the request stays valid.
        contents.append({"role": "user", "parts": [{"text": "\n".join(p["text"] for p in system_parts)}]})
        system_parts = []
    body: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "topP": top_p,
            "maxOutputTokens": max_completion_tokens,
        },
    }
    if system_parts:
        body["systemInstruction"] = {"parts": system_parts}
    schema = gemini_schema_from_response_format(response_format)
    if schema is not None:
        body["generationConfig"]["responseMimeType"] = "application/json"
        body["generationConfig"]["responseJsonSchema"] = schema
    thinking = _thinking_config(model, reasoning_effort)
    if thinking is not None:
        body["generationConfig"]["thinkingConfig"] = thinking
    return body


def _text_from_body(body: dict[str, Any] | None) -> str | None:
    """Join the text parts of the first candidate (thought parts excluded)."""
    if not isinstance(body, dict):
        return None
    candidates = body.get("candidates") or []
    if not candidates:
        return None
    parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
    texts = [p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought") and "text" in p]
    return "".join(texts) or None


async def gemini_json_completion(
    *,
    messages: list[dict[str, str]],
    model: str,
    api_key: str,
    response_format: dict[str, Any],
    reasoning_effort: str = "medium",
    temperature: float = 0.2,
    top_p: float = 1.0,
    max_completion_tokens: int = 4096,
    timeout_s: float = 30.0,
    http_client: httpx.AsyncClient | None = None,
    _use_sdk: bool = True,
) -> dict[str, Any] | None:
    """Request a structured JSON completion from the Gemini API.

    Defaults (§4): SMAC_TEMPERATURE=0.2 (bajo para JSON), SMAC_MAX_TOKENS=4096
    vía `nebula.config.defaults` en los callers; aquí solo fallback seguro.

    Args:
        messages (list): Chat messages (system + user/assistant).
        model (str): Gemini model id (from configuration, never hardcoded by callers).
        api_key (str): Gemini API key (empty disables the call).
        response_format (dict): OpenAI-style JSON-schema response format
            (converted internally to Gemini's ``responseJsonSchema``).
        reasoning_effort (str): ``low`` / ``medium`` / ``high`` (→ thinkingLevel).
        temperature (float): Sampling temperature.
        top_p (float): Nucleus sampling (→ ``topP``).
        max_completion_tokens (int): Max tokens to generate (→ ``maxOutputTokens``).
        timeout_s (float): Transport timeout in seconds.
        http_client (httpx.AsyncClient | None): Injected client (tests with
            ``MockTransport``).
        _use_sdk (bool): Kept for caller/test compatibility; the Gemini
            transport is always REST (no SDK).

    Returns:
        dict | None: Parsed JSON object, or None on any failure (safe
        degradation: callers proceed without the LLM).
    """
    if not api_key:
        return None
    return await _https_completion(
        messages=messages,
        model=model,
        api_key=api_key,
        response_format=response_format,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        top_p=top_p,
        max_completion_tokens=max_completion_tokens,
        timeout_s=timeout_s,
        http_client=http_client,
    )


async def _https_completion(
    *,
    messages: list[dict[str, str]],
    model: str,
    api_key: str,
    response_format: dict[str, Any],
    reasoning_effort: str,
    temperature: float,
    top_p: float,
    max_completion_tokens: int,
    timeout_s: float,
    http_client: httpx.AsyncClient | None,
) -> dict[str, Any] | None:
    """Gemini REST call with a shared 3-attempt budget.

    429/5xx wait (3s, 12s) and a non-JSON reply re-asks for a fresh sample.
    401/403 means the key is invalid/revoked: retrying is pointless (logged).
    """
    url = f"{GEMINI_BASE_URL}/models/{model}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    body = _build_request_body(
        messages=messages,
        model=model,
        response_format=response_format,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        top_p=top_p,
        max_completion_tokens=max_completion_tokens,
    )
    for attempt in (1, 2, 3):
        try:
            if http_client is None:
                async with httpx.AsyncClient(timeout=timeout_s) as client:
                    response = await client.post(url, headers=headers, json=body)
            else:
                response = await http_client.post(url, headers=headers, json=body)
            response.raise_for_status()
            payload = response.json()
            text = _text_from_body(payload)
            if text is None:
                finish = ""
                candidates = payload.get("candidates") or [] if isinstance(payload, dict) else []
                if candidates:
                    finish = (candidates[0] or {}).get("finishReason") or ""
                logging.warning(f"SMAC | Gemini reply had no text (finishReason={finish or 'n/a'})")
                return None
            data = extract_json_object(text)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else None
            if status in (401, 403):
                logging.warning(
                    f"SMAC | Gemini: {status} — API key inválida o revocada, "
                    "regenera GEMINI_API_KEY (no se reintenta)"
                )
                return None
            if status in _RETRYABLE_STATUSES and attempt <= 2:
                _wait = GEMINI_RETRY_DELAYS[attempt - 1] + random.uniform(0, 1.0)
                detail = ""
                try:
                    detail = ((e.response.json() or {}).get("error") or {}).get("message", "")[:200]
                except Exception:
                    pass
                logging.warning(f"SMAC | Gemini: {status} — reintento en {_wait:.1f}s (jitter) {detail}")
                await asyncio.sleep(_wait)
                continue
            detail = ""
            try:
                detail = ((e.response.json() or {}).get("error") or {}).get("message", "")[:300]
            except Exception:
                pass
            logging.warning(f"SMAC | Gemini HTTP call failed: {status}: {detail or type(e).__name__}")
            return None
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if attempt <= 2:
                _wait = GEMINI_RETRY_DELAYS[attempt - 1] + random.uniform(0, 1.0)
                logging.warning(f"SMAC | Gemini timeout/transporte — reintento en {_wait:.1f}s: {e}")
                await asyncio.sleep(_wait)
                continue
            logging.warning(f"SMAC | Gemini call failed: {type(e).__name__}: {e}")
            return None
        except Exception as e:
            logging.warning(f"SMAC | Gemini call failed: {type(e).__name__}: {e}")
            return None
        else:
            if data is None:
                if attempt <= 2:
                    logging.warning("SMAC | Gemini reply was not valid JSON — reintento")
                    continue
                logging.warning("SMAC | Gemini reply was not valid JSON (possibly truncated)")
            return data
    return None
