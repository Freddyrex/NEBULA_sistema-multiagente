#!/usr/bin/env python3
"""Deterministic context/fast-path latency baseline (no network).

This is the baseline the crew must beat or bound: it measures context building
and the deterministic fast-path that runs before any LLM call.

    python scripts/benchmark_smac.py --iterations 200
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nebula.agentic.context import build_context_pack
from nebula.agentic.strategies import fast_path_decision


def _state() -> dict:
    return {
        "round": 9,
        "status": "running",
        "nodes": [{"id": f"n{i}", "online": True, "paused": False} for i in range(8)],
        "metrics_summary": {"final": {"accuracy": 0.63}, "findings": [{"type": "accuracy_plateau"}]},
        "config": {"aggregator": "FedAvg", "rounds": 20, "epochs": 2},
        "suggestions": [
            {
                "kind": "pause_node",
                "params": {"node": "n7"},
                "reason": "straggler",
                "evidence": [{"source": "findings", "ref": "straggler", "node": "n7"}],
            }
        ],
    }


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = min(len(ordered) - 1, max(0, round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[idx]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=200)
    args = parser.parse_args()
    state = _state()
    samples: list[float] = []
    for _ in range(max(1, args.iterations)):
        started = time.perf_counter()
        ctx = build_context_pack(state, scenario="bench")
        fast_path_decision(ctx, state["suggestions"])
        samples.append((time.perf_counter() - started) * 1000)
    print(json.dumps({
        "iterations": len(samples),
        "p50_ms": round(statistics.median(samples), 3),
        "p95_ms": round(_percentile(samples, 95), 3),
        "max_ms": round(max(samples), 3),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
