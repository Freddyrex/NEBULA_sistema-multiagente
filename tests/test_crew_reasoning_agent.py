import asyncio

from nebula.agentic.orchestrator import CrewResult
from nebula.agentic.schemas import CrewDecision, GuardrailReport, PatternDecision
from nebula.core.situationalawareness.awareness.cognitive.crew_reasoning_agent import (
    CrewReasoning,
    _as_decision,
)


def _result(kind: str, params: dict) -> CrewResult:
    decision = CrewDecision(
        patterns={"aggregation_algorithm": PatternDecision(value="change", params=params, confidence=0.8)},
        global_rationale="rationale",
        confidence=0.8,
        guardrails=GuardrailReport(passed=True),
    )
    return CrewResult(
        decision=decision,
        strategy="voting",
        proposal={"kind": kind, "params": params, "description": "test"},
    )


def test_as_decision_maps_select_aggregator():
    decision = _as_decision(_result("select_aggregator", {"algorithm": "Krum", "node": "n1"}))
    assert decision["decision"] == "select_aggregator"
    assert decision["parameters"]["algorithm"] == "Krum"
    assert decision["confidence"] == 0.8


def test_as_decision_maps_pause_and_topology():
    paused = _as_decision(_result("pause_node", {"node": "n2"}))
    assert paused["decision"] == "adjust_training_policy"
    assert paused["parameters"]["pause_node"] == "n2"

    topology = _as_decision(
        _result("propose_architecture_change", {"change": {"kind": "topology", "value": "ring"}})
    )
    assert topology["decision"] == "request_topology_change"


def test_as_decision_flags_new_simulation_for_review():
    decision = _as_decision(
        _result("propose_new_simulation", {"changes": {"topology": {"kind": "topology"}}})
    )
    assert decision["decision"] == "flag_for_human_review"


def test_blocked_result_never_becomes_a_command():
    result = _result("pause_node", {"node": "n2"})
    result.blocked = True
    assert _as_decision(result) is None


def test_disabled_agent_without_key_degrades(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    agent = CrewReasoning({"enabled": True, "verbose": False})
    assert agent.enabled is False
    result = _result("pause_node", {"node": "n2"})
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        command = agent._command_from_result(result)
    finally:
        loop.close()
        asyncio.set_event_loop(None)
    assert command is not None
