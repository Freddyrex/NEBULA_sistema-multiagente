#!/usr/bin/env python3
"""Reproducible crew latency/strategy benchmark.

Offline (default) exercises the deterministic fast-path across synthetic
scenarios. ``--live`` calls Gemini every iteration, so it needs an API key and
is intentionally capped by ``--iterations``.

    python scripts/benchmark_crew.py --iterations 50
    python scripts/benchmark_crew.py --live --iterations 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nebula.agentic.config import CrewConfig
from nebula.agentic.orchestrator import CrewOrchestrator


def _state(i: int) -> dict:
    return {
        "round": i,
        "status": "running",
        "nodes": [{"id": f"n{j}", "online": True, "paused": False} for j in range(1, 6)],
        "metrics_summary": {"final": {"accuracy": 0.5 + (i % 7) / 100}, "findings": []},
        "config": {"aggregator": "FedAvg"},
        "suggestions": [
            {
                "kind": "pause_node",
                "params": {"node": f"n{1 + (i % 5)}"},
                "reason": "synthetic straggler",
                "evidence": [{"source": "findings", "ref": "synthetic"}],
            }
        ],
    }


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]


async def _run(args) -> dict:
    cfg = CrewConfig(
        enabled=True,
        strategy=args.strategy,
        cache_ttl_s=0 if args.live else args.cache_ttl,
        total_timeout_s=args.timeout,
    )
    orchestrator = CrewOrchestrator(cfg, api_key=os.environ.get("GEMINI_API_KEY", ""))
    latencies: list[float] = []
    strategies: dict[str, int] = {}
    blocked = 0
    exclusions: Counter[str] = Counter()
    for i in range(max(1, args.iterations)):
        state = _state(i)
        started = time.perf_counter()
        result = await orchestrator.decide(
            f"bench-{i}",
            state=state,
            strategy=args.strategy,
            trigger="benchmark",
            suggestions=state["suggestions"],
            role="admin",
        )
        latencies.append((time.perf_counter() - started) * 1000)
        strategies[result.strategy] = strategies.get(result.strategy, 0) + 1
        blocked += int(result.blocked)
        if result.proposal and result.proposal.get("kind") == "pause_node":
            exclusions[str((result.proposal.get("params") or {}).get("node") or "?")] += 1
    total_exclusions = sum(exclusions.values())
    return {
        "mode": "live" if args.live else "offline-fast-path",
        "iterations": len(latencies),
        "p50_ms": round(statistics.median(latencies), 3),
        "p95_ms": round(_p95(latencies), 3),
        "max_ms": round(max(latencies), 3),
        "blocked": blocked,
        "strategies": strategies,
        "exclusions": dict(sorted(exclusions.items())),
        "max_exclusion_ratio": round(max(exclusions.values(), default=0) / total_exclusions, 4)
        if total_exclusions
        else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--strategy", default="auto")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--cache-ttl", type=float, default=0.0)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
