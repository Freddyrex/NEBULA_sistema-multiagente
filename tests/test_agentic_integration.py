import asyncio

from nebula.agentic.config import CrewConfig
from nebula.agentic.orchestrator import CrewResult
from nebula.agentic.schemas import CrewDecision, GuardrailReport, PatternDecision
from nebula.frontend import agentic_context, smac_api, smac_chat


def _deps() -> smac_api.SmacDeps:
    return smac_api.SmacDeps(
        config_dir=None,
        log_dir=None,
        databases_dir=None,
        controller_host="127.0.0.1",
        controller_port=5000,
        controller_name="test",
        api_key="key",
    )


def _crew_result() -> CrewResult:
    decision = CrewDecision(
        patterns={"client_selection": PatternDecision(value="change", params={"node": "n3"}, confidence=0.8)},
        global_rationale="n3 is a straggler",
        confidence=0.8,
        guardrails=GuardrailReport(passed=True),
    )
    return CrewResult(
        decision=decision,
        strategy="fast_path",
        proposal={"kind": "pause_node", "params": {"node": "n3"}, "description": "pause n3"},
        latency_ms=12,
    )


def _patch_common(monkeypatch):
    pending = [{"id": "act1", "kind": "pause_node", "params": {"node": "n3"}}]
    monkeypatch.setattr(smac_api, "get_panel_state", lambda *a, **k: {"round": 1, "nodes": []})
    monkeypatch.setattr(smac_api, "check_rate_limit", lambda *a, **k: True)
    monkeypatch.setattr(smac_api, "register_pending_actions", lambda *a, **k: pending)
    monkeypatch.setattr(smac_api, "audit_event", lambda *a, **k: None)
    monkeypatch.setattr(smac_api, "append_exchange", lambda *a, **k: None)
    monkeypatch.setattr(smac_api, "_refresh_cached_conversation", lambda *a, **k: None)
    monkeypatch.setattr(smac_api, "_run_crew_for", None)
    return pending


def test_duplicate_pending_action_is_reused():
    smac_chat._pending_actions.clear()
    proposal = {"kind": "pause_node", "params": {"node": "n3"}, "description": "pause n3"}

    first = smac_chat.register_pending_actions([proposal], "demo", "alice")
    second = smac_chat.register_pending_actions([proposal], "demo", "alice")

    assert first[0]["id"] == second[0]["id"]
    assert len(smac_chat._pending_actions) == 1
    smac_chat._pending_actions.clear()


def test_orchestrator_reuse_preserves_cache_across_requests(monkeypatch):
    monkeypatch.setattr(agentic_context, "_ORCHESTRATORS", {})
    deps = _deps()
    cfg = CrewConfig(enabled=True, cache_ttl_s=60)

    first = agentic_context._orchestrator_for(deps, "demo", cfg)
    second = agentic_context._orchestrator_for(deps, "demo", cfg)
    changed = agentic_context._orchestrator_for(
        deps, "demo", CrewConfig(enabled=True, cache_ttl_s=0)
    )

    assert first is second
    assert changed is not first


def test_ask_routes_decision_intent_to_crew(monkeypatch):
    pending = _patch_common(monkeypatch)

    async def fake_crew(*args, **kwargs):
        return _crew_result()

    monkeypatch.setattr(smac_api, "_run_crew_for", fake_crew)

    status, payload = asyncio.run(
        smac_api.handle_ask(
            _deps(), {"user": "alice", "role": "admin"}, "analiza y recomienda", "demo"
        )
    )

    assert status == 200
    assert payload["actions"] == pending
    assert payload["crew"]["strategy"] == "fast_path"
    assert "pause" in payload["reply"].lower() or "pausar" in payload["reply"].lower()


def test_ask_ignores_casual_message(monkeypatch):
    _patch_common(monkeypatch)
    called = {"crew": False}

    async def fake_crew(*args, **kwargs):
        called["crew"] = True
        return _crew_result()

    async def fake_gemini(*args, **kwargs):
        return {"reply": "hola"}

    monkeypatch.setattr(smac_api, "_run_crew_for", fake_crew)
    monkeypatch.setattr(smac_api, "ask_gemini", fake_gemini)

    status, payload = asyncio.run(
        smac_api.handle_ask(_deps(), {"user": "alice"}, "hola, gracias", "demo")
    )

    assert status == 200
    assert called["crew"] is False
    assert payload["reply"] == "hola"


def test_analyze_returns_crew_payload(monkeypatch):
    _patch_common(monkeypatch)

    async def fake_crew(*args, **kwargs):
        return _crew_result()

    monkeypatch.setattr(smac_api, "_run_crew_for", fake_crew)

    status, payload = asyncio.run(
        smac_api.handle_analyze(_deps(), {"user": "alice"}, "demo")
    )

    assert status == 200
    assert payload["actions"][0]["id"] == "act1"
    assert payload["crew"]["proposal"]["kind"] == "pause_node"
