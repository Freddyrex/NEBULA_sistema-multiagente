import asyncio
import json
from types import SimpleNamespace

from nebula.agentic import agents
from nebula.agentic.schemas import AGENT_VERDICT_SCHEMA


class _FakeLLM:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeTask:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeCrew:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def kickoff(self):
        payload = {
            "agent": "PerformanceAnalyst",
            "pattern": "client_selection",
            "decision": "change",
            "confidence": 0.9,
            "rationale": "n4 is a straggler",
            "evidence": [{"source": "findings", "ref": "spike", "node": "n4"}],
            "proposed_params": {"node": "n4"},
        }
        return SimpleNamespace(raw=json.dumps(payload))


def test_crewai_backend_parses_structured_output(monkeypatch):
    parts = (_FakeAgent, _FakeCrew, _FakeLLM, SimpleNamespace(sequential="sequential"), _FakeTask)
    monkeypatch.setattr(agents, "_crewai", lambda: parts)
    monkeypatch.setattr(agents, "crewai_available", lambda: True)
    backend = agents.CrewAIBackend("test-key")

    reply = asyncio.run(
        backend.complete_json(
            [{"role": "system", "content": "be strict"}, {"role": "user", "content": "state"}],
            schema=AGENT_VERDICT_SCHEMA,
            model="gemini-3.5-flash-lite",
            timeout_s=5,
        )
    )

    assert reply.error is None
    assert reply.data["pattern"] == "client_selection"
    assert reply.data["proposed_params"] == {"node": "n4"}


def test_crewai_backend_degrades_without_key(monkeypatch):
    monkeypatch.setattr(agents, "crewai_available", lambda: True)
    reply = asyncio.run(
        agents.CrewAIBackend("").complete_json(
            [], schema={}, model="gemini-3.5-flash-lite", timeout_s=1
        )
    )

    assert reply.data is None
    assert reply.error == "no_api_key"
