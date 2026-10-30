"""Resiliencia Gemini: clave por fichero + guard anti-413 + transporte REST."""
import asyncio
import json
import os
import tempfile

import httpx

from nebula.frontend.smac_chat import (
    MAX_SNAPSHOT_BYTES,
    resolve_gemini_key,
    shrink_snapshot_for_llm,
)


def test_resolve_gemini_key_precedence_and_file():
    assert resolve_gemini_key("  AQ.x  ") == "AQ.x"
    old_env, old_file = os.environ.get("GEMINI_API_KEY"), os.environ.get("GEMINI_APIKEY_FILE")
    try:
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("GEMINI_APIKEY_FILE", None)
        with tempfile.TemporaryDirectory() as empty:
            here = os.getcwd()
            os.chdir(empty)
            try:
                assert resolve_gemini_key("") == ""
            finally:
                os.chdir(here)
        with tempfile.TemporaryDirectory() as tmp:
            keyfile = os.path.join(tmp, "mykey")
            with open(keyfile, "w") as f:
                f.write("  AQ.filekey  \n")
            os.environ["GEMINI_APIKEY_FILE"] = keyfile
            from nebula.frontend.smac_chat import resolve_gemini_key as r2

            assert r2("") == "AQ.filekey"
    finally:
        if old_env is not None:
            os.environ["GEMINI_API_KEY"] = old_env
        else:
            os.environ.pop("GEMINI_API_KEY", None)
        if old_file is not None:
            os.environ["GEMINI_APIKEY_FILE"] = old_file
        else:
            os.environ.pop("GEMINI_APIKEY_FILE", None)


def _fat_state():
    # Must exceed MAX_SNAPSHOT_BYTES (32 kB) so the shrinker actually runs.
    return {
        "scenario": "d", "status": "running",
        "metrics_summary": {
            "metrics": {
                "accuracy": {"first": 0.5, "final": 0.9, "delta": 0.4,
                             "best": {"round": 9, "value": 0.9},
                             "ranking_final": [{"node_id": i, "value": 0.9} for i in range(100)],
                             "outliers": [{"node_id": i} for i in range(100)]},
            },
            "series": {"accuracy": [[r, 0.5] for r in range(7000)]},
        },
        "runtime": {"metrics_summary": {"series": {"loss": [[r, 0.1] for r in range(7000)]}}},
        "recent_decisions": [{"d": "x" * 2000} for _ in range(40)],
        "nodes": [{"idx": str(i)} for i in range(80)],
        "containers": [{"n": i} for i in range(80)],
        "findings": [{"type": "accuracy_plateau", "summary": "plateau"}],
        "suggestions": [{"title": "Cambiar FedAvg → Krum"}],
    }


def test_shrink_keeps_value_drops_bulk():
    fat = _fat_state()
    assert len(json.dumps(fat)) > MAX_SNAPSHOT_BYTES
    slim = shrink_snapshot_for_llm(fat)
    assert len(json.dumps(slim)) <= MAX_SNAPSHOT_BYTES
    assert slim["metrics_summary"]["metrics"]["accuracy"]["final"] == 0.9
    assert slim["metrics_summary"]["metrics"]["accuracy"]["best"] == {"round": 9, "value": 0.9}
    assert "series" not in slim["metrics_summary"]
    assert len(slim["metrics_summary"]["metrics"]["accuracy"]["ranking_final"]) == 3
    assert len(slim["recent_decisions"]) == 5
    assert len(slim["nodes"]) == 20
    assert slim["findings"][0]["type"] == "accuracy_plateau"
    assert slim["suggestions"][0]["title"].startswith("Cambiar")
    # el original cacheado no se muta
    assert len(fat["metrics_summary"]["metrics"]["accuracy"]["ranking_final"]) == 100


def test_shrink_small_state_passthrough_and_invalid():
    small = {"scenario": "d", "metrics_summary": {"metrics": {}}}
    assert shrink_snapshot_for_llm(small)["scenario"] == "d"
    assert shrink_snapshot_for_llm(None) == {}
    assert shrink_snapshot_for_llm("x") == {}


# --- transporte Gemini (smac_gemini) ------------------------------------------

from nebula.core.smac_gemini import (  # noqa: E402
    _build_request_body,
    _thinking_config,
    extract_json_object,
    gemini_json_completion,
    gemini_schema_from_response_format,
)

_MESSAGES = [
    {"role": "system", "content": "You output JSON."},
    {"role": "user", "content": "hola"},
]

_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "smac",
        "strict": False,
        "schema": {
            "type": "object",
            "properties": {
                "reply": {"type": "string", "maxLength": 500},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["reply"],
            "additionalProperties": False,
        },
    },
}


def test_schema_conversion_strips_unsupported_keys():
    schema = gemini_schema_from_response_format(_FORMAT)
    assert schema["type"] == "object"
    assert "reply" in schema["properties"]
    assert "maxLength" not in schema["properties"]["reply"]
    assert "additionalProperties" not in schema
    assert "strict" not in schema
    # formatos raros no explotan
    assert gemini_schema_from_response_format(None) is None
    assert gemini_schema_from_response_format({}) is None
    assert gemini_schema_from_response_format({"type": "json_object"}) is None


def test_schema_conversion_keeps_property_names_that_look_like_keywords():
    schema = gemini_schema_from_response_format(
        {
            "type": "json_schema",
            "json_schema": {
                "name": "crew",
                "schema": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "pattern": "^[a-z_]+$"},
                        "value": {"type": "string"},
                    },
                    "required": ["pattern", "value"],
                },
            },
        }
    )

    assert set(schema["properties"]) == {"pattern", "value"}
    assert "pattern" not in schema["properties"]["pattern"]
    assert schema["required"] == ["pattern", "value"]


def test_thinking_config_by_model_family():
    assert _thinking_config("gemini-3.5-flash", "low") == {"thinkingLevel": "low"}
    assert _thinking_config("gemini-4-argon", "high") == {"thinkingLevel": "high"}
    assert _thinking_config("gemini-2.5-flash", "low") == {"thinkingBudget": 0}
    assert _thinking_config("gemini-2.5-flash", "medium") == {"thinkingBudget": -1}
    assert _thinking_config("gemini-2.0-flash", "high") is None
    assert _thinking_config("gemini-flash-latest", "low") is None
    # solo los modelos gemini-* exponen controles de thinking
    assert _thinking_config("gemma-4-26b-a4b-it", "low") is None
    assert _thinking_config("gemini-nano-banana-2.1", "low") is None


def test_request_body_maps_openai_style_payload():
    body = _build_request_body(
        messages=_MESSAGES,
        model="gemini-3.5-flash",
        response_format=_FORMAT,
        reasoning_effort="medium",
        temperature=0.2,
        top_p=1.0,
        max_completion_tokens=4096,
    )
    assert body["systemInstruction"]["parts"][0]["text"] == "You output JSON."
    assert body["contents"] == [{"role": "user", "parts": [{"text": "hola"}]}]
    cfg = body["generationConfig"]
    assert cfg["responseMimeType"] == "application/json"
    assert cfg["maxOutputTokens"] == 4096
    assert cfg["thinkingConfig"] == {"thinkingLevel": "medium"}
    assert cfg["responseJsonSchema"]["required"] == ["reply"]


def _ok_body(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {"parts": [{"text": text, "thought": True}, {"text": text}], "role": "model"},
                    "finishReason": "STOP",
                }
            ]
        },
    )


def _completion(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    try:
        return asyncio.run(
            gemini_json_completion(
                messages=_MESSAGES,
                model="gemini-3.5-flash",
                api_key="AQ.test",
                response_format=_FORMAT,
                http_client=client,
                **kwargs,
            )
        )
    finally:
        asyncio.run(client.aclose())


def test_happy_path_returns_json_and_sends_auth_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("x-goog-api-key")
        return _ok_body('{"reply": "hola", "confidence": 0.9}')

    data = _completion(handler)
    assert data == {"reply": "hola", "confidence": 0.9}
    assert seen["url"].endswith("/models/gemini-3.5-flash:generateContent")
    assert seen["auth"] == "AQ.test"


def test_no_key_disables_call_without_network():
    assert (
        asyncio.run(
            gemini_json_completion(
                messages=_MESSAGES, model="m", api_key="", response_format=_FORMAT
            )
        )
        is None
    )


def test_401_returns_none_without_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": {"code": 401, "message": "bad key"}})

    assert _completion(handler) is None
    assert calls["n"] == 1


def test_429_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"code": 429, "message": "quota"}})
        return _ok_body('{"reply": "ok"}')

    data = _completion(handler)
    assert data == {"reply": "ok"}
    assert calls["n"] == 2


def test_invalid_json_retries_then_none():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _ok_body("```json\nno es json\n```")

    assert _completion(handler) is None
    assert calls["n"] == 3


def test_candidate_without_text_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]},
        )

    assert _completion(handler) is None


def test_extract_json_object_still_robust():
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('prosa {"a": 1} final') == {"a": 1}
    assert extract_json_object("nada") is None
    assert extract_json_object(None) is None
