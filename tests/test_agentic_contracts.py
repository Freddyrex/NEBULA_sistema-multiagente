import pytest

from nebula.agentic.context import build_context_pack, build_node_context_pack
from nebula.agentic.guardrails import GuardrailEngine
from nebula.agentic.patterns import canonical_algorithm, get_pattern, is_allowed
from nebula.agentic.prompts import specialist_messages
from nebula.agentic.schemas import (
    AGENT_VERDICT_SCHEMA,
    CrewDecision,
    Evidence,
    GuardrailReport,
    PatternDecision,
    parse_decision,
)
from nebula.agentic.strategies import fast_path_decision
from nebula.agentic.trace import TraceStore


def _state() -> dict:
    return {
        "round": 7,
        "status": "running",
        "nodes": [
            {"id": "n1", "online": True, "paused": False},
            {"id": "n2", "online": True, "paused": False},
            {"id": "n3", "online": True, "paused": False},
        ],
        "metrics_summary": {"final": {"accuracy": 0.71}, "findings": []},
        "config": {"aggregator": "FedAvg", "api_key": "secret-value", "rounds": 10},
    }


def _evidence() -> list[Evidence]:
    return [Evidence(source="metrics_digest", ref="final.accuracy", value=0.71)]


def test_context_pack_is_deterministic_and_redacts_secrets():
    first = build_context_pack(_state(), scenario="demo", user="alice")
    second = build_context_pack(_state(), scenario="demo", user="bob")

    assert first.state_hash == second.state_hash
    assert "api_key" not in first.config
    assert first.round == 7
    assert len(first.nodes) == 3
    assert first.provenance


def test_node_context_pack_uses_node_snapshot():
    pack = build_node_context_pack(
        {
            "round": 3,
            "node": "abc123",
            "agents": {"satraining": {"findings": [{"type": "accuracy_plateau", "severity": "high"}]}},
        },
        scenario="demo",
        node="abc123",
    )

    assert pack.round == 3
    assert pack.findings[0]["type"] == "accuracy_plateau"
    assert pack.scenario["user"] == "abc123"
    assert pack.nodes[0]["id"] == "abc123"
    assert pack.guardrails["min_clients"] == 2


def test_trace_store_rejects_scenario_path_traversal(tmp_path):
    for scenario in ("../escape", "a/b", "..", "."):
        store = TraceStore(log_dir=str(tmp_path), scenario=scenario)
        assert store.path is None


def test_agent_schema_only_advertises_available_patterns():
    pattern = AGENT_VERDICT_SCHEMA["properties"]["pattern"]
    params = AGENT_VERDICT_SCHEMA["properties"]["proposed_params"]

    assert "client_selection" in pattern["enum"]
    assert "message_compression" not in pattern["enum"]
    assert "node" in params["properties"]


def test_specialist_prompt_has_single_system_message():
    ctx = build_context_pack(_state(), scenario="demo")
    messages = specialist_messages("performance", ctx)

    assert [m["role"] for m in messages] == ["system", "user"]


def test_patterns_whitelist_and_unavailable_levers():
    allowed, reason = is_allowed("aggregation_algorithm", {"algorithm": "krum"})
    assert allowed and reason == "ok"
    assert canonical_algorithm("krum") == "Krum"

    allowed, reason = is_allowed("heterogeneous_data_handler", {})
    assert not allowed and "no disponible" in reason
    assert get_pattern("message_compression").available is False


def test_fast_path_uses_single_actionable_suggestion():
    ctx = build_context_pack(_state(), scenario="demo")
    decision = fast_path_decision(
        ctx,
        [{"kind": "pause_node", "params": {"node": "n3"}, "reason": "straggler"}],
    )

    assert decision is not None
    assert decision.patterns["client_selection"].params["node"] == "n3"
    assert decision.guardrails.passed


def test_fast_path_requires_cost_for_structural_change():
    ctx = build_context_pack(_state(), scenario="demo")
    decision = fast_path_decision(
        ctx,
        [{"kind": "propose_architecture_change", "params": {"change": {"kind": "rounds", "value": 20}}}],
    )

    assert decision is None


def test_guardrails_veto_change_without_actionable_params():
    decision = CrewDecision(
        patterns={"client_selection": PatternDecision(value="change", confidence=0.8)},
        global_rationale="missing node",
        confidence=0.8,
        evidence=_evidence(),
    )
    report = GuardrailEngine().validate(decision, build_context_pack(_state(), scenario="demo"))

    assert not report.passed
    assert any("params" in veto for veto in report.vetoes)


def test_guardrails_veto_unavailable_pattern_and_min_clients():
    ctx = build_context_pack(_state(), scenario="demo")
    engine = GuardrailEngine(min_clients=3)
    decision = CrewDecision(
        patterns={
            "heterogeneous_data_handler": PatternDecision(value="change", confidence=0.9),
        },
        global_rationale="add GAN",
        confidence=0.9,
        evidence=_evidence(),
        guardrails=GuardrailReport(passed=True),
    )

    report = engine.validate(decision, ctx, role="user")
    assert not report.passed
    assert any("whitelist" in veto for veto in report.vetoes)

    decision.patterns = {
        "client_selection": PatternDecision(
            value="change", params={"node": "n3"}, confidence=0.9, rationale="exclude"
        )
    }
    report = engine.validate(decision, ctx, role="user")
    assert not report.passed
    assert any("min_clients" in veto for veto in report.vetoes)


def test_parse_decision_keeps_unknown_pattern_for_guardrail_veto():
    unparsed = parse_decision({"patterns": {"made_up": {"value": "change"}}, "global_rationale": "x"})
    assert unparsed is not None
    assert "made_up" in unparsed.patterns
    parsed = parse_decision(
        {
            "patterns": {"aggregation_algorithm": {"value": "change", "params": {"algorithm": "krum"}}},
            "global_rationale": "use krum",
            "confidence": 0.8,
        }
    )
    assert parsed is not None and parsed.confidence == pytest.approx(0.8)
