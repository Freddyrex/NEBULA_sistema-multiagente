"""Deterministic context construction for the agentic layer.

The builder never performs I/O: callers pass the already-built panel state (or a
node snapshot). That keeps the crew testable, prevents duplicate metrics reads
and lets the controller/frontend and the node share the same contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from nebula.agentic.schemas import ContextPack

_SECRET_KEYS = re.compile(r"(api[_-]?key|authorization|password|secret|token|cookie)", re.I)
_MAX_STRING = 1200


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "..."
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _SECRET_KEYS.search(str(key)):
                out[str(key)] = "[redacted]"
            else:
                out[str(key)] = _redact(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(v, depth + 1) for v in value[:64]]
    if isinstance(value, str):
        return value[:_MAX_STRING]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_MAX_STRING]


def _final_metrics(metrics_summary: dict[str, Any] | None) -> dict[str, Any]:
    metrics = (metrics_summary or {}).get("metrics") or {}
    names = (
        "accuracy",
        "f1_macro",
        "loss",
        "round_time_s",
        "training_time_s",
        "communication_time_s",
        "bytes_sent",
        "bytes_received",
        "cpu_percent",
        "ram_percent",
        "cpu_temp",
        "cpu_temp_est",
        "energy_total",
    )
    out: dict[str, Any] = dict.fromkeys(names)
    for name in names:
        entry = metrics.get(name)
        if isinstance(entry, dict) and entry.get("final") is not None:
            out[name] = entry.get("final")
    return out


def _node_payload(nodes: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in nodes or []:
        if not isinstance(raw, dict):
            continue
        state = str(raw.get("state") or raw.get("container_state") or "").lower()
        out.append(
            {
                "id": str(raw.get("id") or raw.get("idx") or raw.get("node") or "?"),
                "cpu": raw.get("cpu"),
                "ram": raw.get("ram"),
                "online": bool(raw.get("online")) if raw.get("online") is not None else state == "running",
                "paused": state == "paused" or bool(raw.get("paused")),
                "role": raw.get("role"),
            }
        )
    return out


def _findings(findings: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in (findings or [])[:32]:
        if not isinstance(raw, dict):
            continue
        out.append(
            {
                "id": raw.get("id"),
                "type": raw.get("type"),
                "severity": raw.get("severity"),
                "metric": raw.get("metric"),
                "nodes": list(raw.get("nodes") or [])[:8],
                "summary": str(raw.get("summary") or "")[:500],
                "evidence": list(raw.get("evidence") or [])[:8],
            }
        )
    return out


def _provenance(pack: ContextPack) -> list[dict[str, Any]]:
    out = [
        {"source": "scenario_config", "ref": "scenario.json", "round": None},
        {"source": "runtime", "ref": f"state:{pack.state_hash}", "round": pack.round},
    ]
    for finding in pack.findings[:10]:
        out.append(
            {
                "source": "findings",
                "ref": f"{finding.get('type')}:{finding.get('id') or 'x'}",
                "round": pack.round,
            }
        )
    if pack.metrics:
        out.append({"source": "metrics_digest", "ref": f"round:{pack.round}", "round": pack.round})
    if pack.decisions:
        out.append({"source": "decision_log", "ref": "decision_memory", "round": pack.round})
    return out


def _hash_material(
    *,
    scenario: str,
    round_number: int | None,
    config: dict[str, Any],
    nodes: list[dict[str, Any]],
    metrics: dict[str, Any],
    findings: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> str:
    material = {
        "scenario": scenario,
        "round": round_number,
        "config": config,
        "nodes": [(n.get("id"), n.get("online"), n.get("paused")) for n in nodes],
        "metrics": metrics,
        "findings": [(f.get("type"), f.get("severity"), tuple(f.get("nodes") or [])) for f in findings],
        "decisions": [
            (d.get("kind") or d.get("event"), d.get("decision"), d.get("algorithm"), d.get("node"))
            for d in decisions[-6:]
        ],
    }
    blob = json.dumps(material, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _config_payload(raw: dict[str, Any] | None) -> dict[str, Any]:
    cfg = raw or {}
    return {
        "n_nodes": cfg.get("n_nodes"),
        "topology": cfg.get("topology"),
        "aggregator": cfg.get("aggregation") or cfg.get("aggregator") or cfg.get("agg_algorithm"),
        "model": cfg.get("model"),
        "rounds": cfg.get("rounds"),
        "epochs": cfg.get("epochs"),
        "batch_size": cfg.get("batch_size"),
        "federation": cfg.get("federation"),
        "dataset": cfg.get("dataset"),
    }


def _decisions_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    decisions = state.get("recent_decisions") or []
    return [d for d in decisions if isinstance(d, dict)][-12:]


def build_context_pack(
    state: dict[str, Any],
    *,
    scenario: str,
    user: str = "",
    include_conversation: bool = True,
    min_clients: int = 2,
    max_exclusion_ratio: float = 0.4,
) -> ContextPack:
    """Build a redacted, deterministic ``ContextPack`` from a panel state."""
    state = state if isinstance(state, dict) else {}
    metrics_summary = state.get("metrics_summary") or {}
    metrics = _final_metrics(metrics_summary)
    nodes = _node_payload(state.get("nodes"))
    findings = _findings(state.get("findings") or metrics_summary.get("findings"))
    config = _config_payload(state.get("config") or state.get("scenario_config"))
    round_number = state.get("round")
    try:
        round_number = int(round_number) if round_number is not None else None
    except (TypeError, ValueError):
        round_number = None
    decisions = _decisions_from_state(state)
    conversation: list[dict[str, Any]] = []
    if include_conversation:
        raw_conv = state.get("conversation") or {}
        messages = raw_conv.get("thread") or raw_conv.get("messages") or []
        conversation = [m for m in messages if isinstance(m, dict)][-10:]
    costs: dict[str, Any] | None = None
    suggestions = list(state.get("suggestions") or [])
    suggestions += list((state.get("architecture") or {}).get("suggestions") or [])
    if suggestions:
        costs = {"candidates": [
            {
                "id": s.get("id"),
                "kind": s.get("kind"),
                "params": s.get("params") or {},
                "cost": (s.get("params") or {}).get("cost"),
                "reason": s.get("reason"),
                "evidence": list(s.get("evidence") or [])[:8],
            }
            for s in suggestions[:8] if isinstance(s, dict)
        ]}
    series = metrics_summary.get("series") if isinstance(metrics_summary.get("series"), dict) else {}
    state_hash = _hash_material(
        scenario=scenario,
        round_number=round_number,
        config=config,
        nodes=nodes,
        metrics=metrics,
        findings=findings,
        decisions=decisions,
    )
    pack = ContextPack(
        scenario={
            "name": scenario,
            "status": str(state.get("status") or "unknown"),
            "user": user or None,
        },
        round=round_number,
        config=config,
        nodes=nodes,
        metrics=metrics,
        series=_redact(series),
        findings=findings,
        costs=_redact(costs),
        decisions=_redact(decisions),
        conversation=_redact(conversation) if conversation else [],
        guardrails={
            "min_clients": max(1, int(min_clients)),
            "max_exclusion_ratio": max(0.0, min(1.0, float(max_exclusion_ratio))),
            "cost_required_for_structural": True,
        },
        provenance=[],
        state_hash=state_hash,
    )
    pack.provenance = _provenance(pack)
    return pack


def build_node_context_pack(
    snapshot: dict[str, Any],
    *,
    scenario: str,
    node: str = "",
    min_clients: int = 2,
    max_exclusion_ratio: float = 0.4,
) -> ContextPack:
    """Build a ``ContextPack`` from an anonymized node snapshot.

    Node snapshots contain agent summaries rather than the controller's panel
    state; the same contract is filled with whatever the node knows, and absent
    data stays ``None`` (never invented).
    """
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    agents = snapshot.get("agents") if isinstance(snapshot.get("agents"), dict) else {}
    metrics = _final_metrics(agents.get("satraining") if isinstance(agents.get("satraining"), dict) else {})
    findings: list[dict[str, Any]] = []
    for summary in agents.values():
        if not isinstance(summary, dict):
            continue
        for item in summary.get("findings") or []:
            if isinstance(item, dict):
                findings.append(item)
    round_number = snapshot.get("round")
    try:
        round_number = int(round_number) if round_number is not None else None
    except (TypeError, ValueError):
        round_number = None
    config = {
        "n_nodes": snapshot.get("n_nodes"),
        "topology": snapshot.get("topology"),
        "aggregator": snapshot.get("aggregator"),
        "model": snapshot.get("model"),
        "rounds": snapshot.get("rounds"),
        "epochs": snapshot.get("epochs"),
        "batch_size": snapshot.get("batch_size"),
    }
    nodes = _node_payload(snapshot.get("nodes"))
    if not nodes and snapshot.get("node"):
        nodes = [{"id": str(snapshot.get("node")), "online": True, "paused": False}]
    state_hash = _hash_material(
        scenario=scenario,
        round_number=round_number,
        config=config,
        nodes=nodes,
        metrics=metrics,
        findings=findings,
        decisions=[],
    )
    pack = ContextPack(
        scenario={"name": scenario, "status": str(snapshot.get("status") or "running"), "user": node or None},
        round=round_number,
        config=config,
        nodes=nodes,
        metrics=metrics,
        series={},
        findings=findings,
        costs=None,
        decisions=[],
        conversation=[],
        guardrails={
            "min_clients": max(1, int(min_clients)),
            "max_exclusion_ratio": max(0.0, min(1.0, float(max_exclusion_ratio))),
            "cost_required_for_structural": True,
        },
        provenance=[],
        state_hash=state_hash,
    )
    pack.provenance = _provenance(pack)
    return pack


__all__ = ["build_context_pack", "build_node_context_pack"]
