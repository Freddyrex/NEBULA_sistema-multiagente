#!/usr/bin/env python3
"""One-shot CrewAI/Gemini compatibility spike.

Examples:
    python scripts/crewai_spike.py --scenario demo --live
    python scripts/crewai_spike.py --scenario demo --offline

Without ``--live`` it only validates imports, config and the deterministic
fast-path (no network, no API key needed).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nebula.agentic.config import load_crew_config
from nebula.agentic.context import build_context_pack
from nebula.agentic.orchestrator import CrewOrchestrator


def _state(scenario: str) -> dict:
    config_dir = os.environ.get("NEBULA_CONFIG_DIR", "")
    payload = {}
    path = Path(config_dir) / scenario / "scenario.json" if config_dir and scenario else None
    if path and path.exists():
        payload = json.loads(path.read_text())
    return {
        "round": 1,
        "status": "running",
        "nodes": [
            {"id": f"n{i}", "online": True, "paused": False} for i in range(1, 5)
        ],
        "metrics_summary": {"final": {"accuracy": 0.5}, "findings": []},
        "config": payload.get("scenario_args") or payload.get("config") or {},
        "suggestions": [
            {
                "kind": "pause_node",
                "params": {"node": "n4"},
                "reason": "spike: synthetic straggler",
                "evidence": [{"source": "findings", "ref": "spike", "node": "n4"}],
            }
        ],
    }


async def _run(args) -> dict:
    cfg = load_crew_config(args.config_dir, args.scenario)
    state = _state(args.scenario)
    ctx = build_context_pack(state, scenario=args.scenario, min_clients=cfg.min_clients)
    if args.offline:
        from nebula.agentic.strategies import fast_path_decision

        decision = fast_path_decision(ctx, state["suggestions"])
        return {
            "mode": "offline",
            "crewai_installed": _crewai_available(),
            "strategy": "fast_path",
            "decision": decision.to_dict() if decision else None,
        }
    orchestrator = CrewOrchestrator(cfg.with_strategy(args.strategy), api_key=os.environ.get("GEMINI_API_KEY", ""))
    result = await orchestrator.decide(
        args.scenario,
        state=state,
        strategy=args.strategy,
        trigger="spike",
        suggestions=state["suggestions"],
        role="admin",
    )
    return {
        "mode": "live",
        "crewai_installed": _crewai_available(),
        "strategy": result.strategy,
        "fallback": result.fallback,
        "blocked": result.blocked,
        "latency_ms": result.latency_ms,
        "proposal": result.proposal,
        "decision": result.decision.to_dict(),
    }


def _crewai_available() -> bool:
    try:
        import crewai  # noqa: F401
    except Exception:
        return False
    else:
        return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="demo")
    parser.add_argument("--config-dir", default=os.environ.get("NEBULA_CONFIG_DIR"))
    parser.add_argument("--strategy", default="auto")
    parser.add_argument("--offline", action="store_true", help="No LLM call")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args)), indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
