"""Shared Groq completion helper for SMAC (official SDK first, httpx fallback).

Both the node-side :class:`GroqReasoning` agent and the frontend chatbot use this
helper, following the official Groq SDK usage::

    from groq import Groq

    client = Groq()
    completion = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[{"role": "user", "content": "..."}],
        temperature=0.2,
        max_completion_tokens=4096,
        top_p=1,
        reasoning_effort="medium",
        stream=False,
    )

The async variant (:class:`groq.AsyncGroq`) is preferred. When the ``groq`` package
is not installed (e.g. older container images), the helper transparently falls back
to the OpenAI-compatible HTTPS endpoint via ``httpx`` with an equivalent payload —
same semantics, no caller changes needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

GROQ_BASE_URL = "https://api.groq.com/openai/v1/chat/completions"


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


async def groq_json_completion(
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
    """Request a structured JSON completion from Groq.

    Defaults (§4): SMAC_TEMPERATURE=0.2 (bajo para JSON), SMAC_MAX_TOKENS=4096
    vía `nebula.config.defaults` en los callers; aquí solo fallback seguro.

    Args:
        messages (list): Chat messages (system + user).
        model (str): Groq model id (from configuration, never hardcoded by callers).
        api_key (str): Groq API key (empty disables the call).
        response_format (dict): JSON-schema response format (structured outputs).
        reasoning_effort (str): ``low`` / ``medium`` / ``high`` (gpt-oss, qwen3…).
        temperature (float): Sampling temperature (Groq default 1).
        top_p (float): Nucleus sampling (Groq default 1).
        max_completion_tokens (int): Max tokens to generate (Groq default 2048).
        timeout_s (float): Transport timeout in seconds.
        http_client (httpx.AsyncClient | None): Injected client for the httpx
            fallback path (used by tests with ``MockTransport``).
        _use_sdk (bool): Force the httpx fallback when False (tests / override).

    Returns:
        dict | None: Parsed JSON object, or None on any failure (safe
        degradation: callers proceed without the LLM).
    """
    if not api_key:
        return None
    if _use_sdk:
        try:
            from groq import AsyncGroq
        except ImportError:
            AsyncGroq = None
        if AsyncGroq is not None:
            return await _sdk_completion(
                AsyncGroq,
                messages=messages,
                model=model,
                api_key=api_key,
                response_format=response_format,
                reasoning_effort=reasoning_effort,
                temperature=temperature,
                top_p=top_p,
                max_completion_tokens=max_completion_tokens,
                timeout_s=timeout_s,
            )
    # Fallback: OpenAI-compatible endpoint (older images without the SDK).
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


async def _sdk_completion(
    AsyncGroq,
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
) -> dict[str, Any] | None:
    """Official SDK path (mirrors Groq's documented usage)."""
    try:
        client = AsyncGroq(api_key=api_key, timeout=timeout_s)
        try:
            completion = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_completion_tokens=max_completion_tokens,
                top_p=top_p,
                reasoning_effort=reasoning_effort,
                response_format=response_format,
                stream=False,
            )
        finally:
            await client.close()
        choices = getattr(completion, "choices", None) or []
        if not choices:
            logging.warning("SMAC | Groq SDK reply had no choices")
            return None
        data = extract_json_object(choices[0].message.content)
    except Exception as e:
        # No key material here: only the error type/message (the SDK
        # does not echo auth headers in exceptions).
        logging.warning(f"SMAC | Groq SDK query failed: {type(e).__name__}: {e}")
        return None
    else:
        if data is None:
            logging.warning("SMAC | Groq SDK reply was not valid JSON (possibly truncated)")
        return data


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
    """httpx fallback against the OpenAI-compatible endpoint.

    Presupuesto de 3 intentos compartido: ante 429 se espera (3s, 12s) y ante
    respuesta no-JSON se pide una muestra nueva. Ante 401 la key es
    inválida/revocada y reintentar no sirve (se avisa en el log).
    """
    import random as _random
    GROQ_RETRY_DELAYS = (3.0, 12.0)  # espera tras los intentos 1 y 2 (+jitter)
    for attempt in (1, 2, 3):
        try:
            payload = {
                "model": model,
                "reasoning_effort": reasoning_effort,
                "temperature": temperature,
                "max_completion_tokens": max_completion_tokens,
                "top_p": top_p,
                "response_format": response_format,
                "messages": messages,
                "stream": False,
            }
            if http_client is None:
                async with httpx.AsyncClient(timeout=timeout_s) as client:
                    response = await client.post(
                        GROQ_BASE_URL,
                        headers={"Authorization": f"Bearer {api_key}"},
                        json=payload,
                    )
            else:
                response = await http_client.post(
                    GROQ_BASE_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
            response.raise_for_status()
            body = response.json()
            choices = body.get("choices") if isinstance(body, dict) else None
            if not choices:
                logging.warning("SMAC | Groq HTTPS reply had no choices")
                return None
            data = extract_json_object(choices[0]["message"]["content"])
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 401:
                logging.warning(
                    "SMAC | Groq: 401 Unauthorized — API key inválida o revocada, "
                    "regenera GROQ_API_KEY (no se reintenta)"
                )
                return None
            if status in (429, 503) and attempt <= 2:
                _wait = GROQ_RETRY_DELAYS[attempt - 1] + _random.uniform(0, 1.0)
                logging.warning(
                    f"SMAC | Groq: {status} — reintento en {_wait:.1f}s (jitter)"
                )
                await asyncio.sleep(_wait)
                continue
            logging.warning(f"SMAC | Groq HTTPS fallback failed: {type(e).__name__}: {e}")
            return None
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if attempt <= 2:
                _wait = GROQ_RETRY_DELAYS[attempt - 1] + _random.uniform(0, 1.0)
                logging.warning(f"SMAC | Groq timeout/transporte — reintento en {_wait:.1f}s: {e}")
                await asyncio.sleep(_wait)
                continue
            logging.warning(f"SMAC | Groq HTTPS fallback failed: {type(e).__name__}: {e}")
            return None
        except Exception as e:
            logging.warning(f"SMAC | Groq HTTPS fallback failed: {type(e).__name__}: {e}")
            return None
        else:
            if data is None:
                if attempt <= 2:
                    logging.warning("SMAC | Groq HTTPS reply was not valid JSON — reintento")
                    continue
                logging.warning("SMAC | Groq HTTPS reply was not valid JSON (possibly truncated)")
            return data
    return None
