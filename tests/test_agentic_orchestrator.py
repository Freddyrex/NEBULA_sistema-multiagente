import asyncio
import json

from nebula.agentic.config import CrewConfig
from nebula.agentic.llm import LLMReply
from nebula.agentic.orchestrator import CrewOrchestrator
from nebula.agentic.trace import TraceStore


class FailingBackend:
    def __init__(self):
        self.calls = 0

    async def complete_json(self, *args, **kwargs):
        self.calls += 1
        return LLMReply(data=None, latency_ms=1, error="boom")


def _state() -> dict:
    return {
        "round": 4,
        "status": "running",
        "nodes": [
            {"id": "n1", "online": True, "paused": False},
            {"id": "n2", "online": True, "paused": False},
            {"id": "n3", "online": True, "paused": False},
        ],
        "metrics_summary": {"final": {"accuracy": 0.5}, "findings": []},
        "config": {"aggregator": "FedAvg"},
    }


def _suggestion() -> dict:
    return {
        "kind": "pause_node",
        "params": {"node": "n3"},
        "reason": "n3 is a straggler",
        "evidence": [{"source": "findings", "ref": "straggler", "node": "n3"}],
    }


def test_disabled_crew_returns_hold_without_calling_llm():
    backend = FailingBackend()
    result = asyncio.run(
        CrewOrchestrator(CrewConfig(enabled=False), backend=backend).decide(
            "demo", state=_state()
        )
    )

    assert result.strategy == "disabled"
    assert result.proposal is None
    assert backend.calls == 0


def test_fast_path_proposes_without_llm_and_caches(tmp_path):
    backend = FailingBackend()
    cfg = CrewConfig(enabled=True, strategy="auto", cache_ttl_s=60)
    orchestrator = CrewOrchestrator(
        cfg, backend=backend, trace_store=TraceStore(log_dir=str(tmp_path), scenario="demo")
    )
    state = _state()

    first = asyncio.run(orchestrator.decide("demo", state=state, suggestions=[_suggestion()]))
    second = asyncio.run(orchestrator.decide("demo", state=state, suggestions=[_suggestion()]))

    assert first.strategy == "fast_path"
    assert first.actionable and first.proposal["kind"] == "pause_node"
    assert second.cache_hit is True
    assert backend.calls == 0

    path = tmp_path / "demo" / "crew_trace.jsonl"
    assert path.exists()
    record = json.loads(path.read_text().splitlines()[0])
    assert record["strategy"] == "fast_path"
    assert "state_hash" in record


def test_rate_limit_falls_back_without_calling_llm(monkeypatch):
    import time

    import nebula.agentic.orchestrator as orchestrator_mod

    monkeypatch.setattr(orchestrator_mod, "_RPM_CALLS", orchestrator_mod.deque())
    orchestrator_mod._RPM_CALLS.append(time.monotonic())
    backend = FailingBackend()
    cfg = CrewConfig(enabled=True, strategy="voting", total_timeout_s=2, max_rpm=1, cache_ttl_s=0)

    result = asyncio.run(CrewOrchestrator(cfg, backend=backend).decide("demo", state=_state()))

    assert result.fallback == "rate_limit"
    assert backend.calls == 0


def test_flat_structural_params_are_normalized():
    assert CrewOrchestrator._change_from_params(
        {"kind": "rounds", "total_rounds": 12}
    ) == {"kind": "rounds", "total_rounds": 12}
    assert CrewOrchestrator._change_from_params(
        {"kind": "model", "model": "MNIST-CNN"}
    ) == {"kind": "model", "to_model": "MNIST-CNN"}
    assert CrewOrchestrator._change_from_params(
        {"change": {"kind": "epochs", "epochs": 3}}
    ) == {"kind": "epochs", "epochs": 3}
    assert CrewOrchestrator._change_from_params({"kind": "rounds"}) == {}


def test_llm_failure_falls_back_to_carry_over():
    backend = FailingBackend()
    cfg = CrewConfig(enabled=True, strategy="voting", total_timeout_s=2)
    result = asyncio.run(
        CrewOrchestrator(cfg, backend=backend).decide("demo", state=_state())
    )

    assert result.strategy in ("voting", "carry_over")
    assert result.proposal is None
    assert result.fallback
    assert backend.calls >= 1
