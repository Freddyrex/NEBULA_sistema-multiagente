"""Frontend adapter for the agentic layer.

Bridges the SMAC panel snapshot, Gemini key and metrics/audit plumbing to the
framework-agnostic ``nebula.agentic`` package.
"""

from __future__ import annotations

import hashlib
from dataclasses import fields
from typing import Any

from nebula.agentic.config import CrewConfig, load_crew_config
from nebula.agentic.context import build_context_pack
from nebula.agentic.orchestrator import CrewOrchestrator, CrewResult, build_orchestrator
from nebula.agentic.trace import TraceStore

_ORCHESTRATORS: dict[tuple[Any, ...], CrewOrchestrator] = {}
_MAX_ORCHESTRATORS = 64


def _config_fingerprint(cfg: CrewConfig) -> tuple[Any, ...]:
    return tuple(getattr(cfg, field.name) for field in fields(cfg))


def _orchestrator_for(deps: Any, scenario: str, cfg: CrewConfig) -> CrewOrchestrator:
    """Reuse the orchestrator so its state-hash cache survives across requests."""
    from nebula.frontend.smac_chat import record_metric

    api_digest = hashlib.sha256((deps.api_key or "").encode()).hexdigest()[:16]
    key = (
        str(deps.config_dir or ""),
        scenario,
        str(deps.log_dir or ""),
        api_digest,
        _config_fingerprint(cfg),
    )
    orchestrator = _ORCHESTRATORS.get(key)
    if orchestrator is None:
        orchestrator = build_orchestrator(
            deps.config_dir,
            scenario,
            api_key=deps.api_key,
            state=None,
            log_dir=deps.log_dir,
            metric_callback=record_metric,
        )
        _ORCHESTRATORS[key] = orchestrator
        while len(_ORCHESTRATORS) > _MAX_ORCHESTRATORS:
            _ORCHESTRATORS.pop(next(iter(_ORCHESTRATORS)))
    return orchestrator


def _suggestions(state: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in state.get("suggestions") or []:
        if isinstance(item, dict):
            out.append(item)
    for item in ((state.get("architecture") or {}).get("suggestions") or []):
        if isinstance(item, dict):
            out.append(item)
    return out


def build_frontend_context(
    deps: Any,
    scenario: str,
    user: str,
    *,
    state: dict[str, Any] | None = None,
    include_conversation: bool = True,
):
    if state is None:
        from nebula.frontend.smac_api import get_panel_state

        state = get_panel_state(deps, scenario, user)
    cfg = load_crew_config(deps.config_dir, scenario)
    return build_context_pack(
        state,
        scenario=scenario,
        user=user,
        include_conversation=include_conversation,
        min_clients=cfg.min_clients,
        max_exclusion_ratio=cfg.max_exclusion_ratio,
    )


async def run_crew(
    deps: Any,
    scenario: str,
    user: str,
    *,
    state: dict[str, Any] | None = None,
    context: Any | None = None,
    role: str = "user",
    strategy: str | None = None,
    trigger: str = "manual",
) -> CrewResult:

    if state is None and context is None:
        from nebula.frontend.smac_api import get_panel_state

        state = get_panel_state(deps, scenario, user)
    cfg = load_crew_config(deps.config_dir, scenario)
    orchestrator = _orchestrator_for(deps, scenario, cfg)
    return await orchestrator.decide(
        scenario,
        state=state,
        context=context,
        strategy=strategy,
        trigger=trigger,
        user=user,
        role=role,
        suggestions=_suggestions(state or {}),
    )


def crew_result_payload(result: CrewResult) -> dict[str, Any]:
    return {
        "strategy": result.strategy,
        "fallback": result.fallback,
        "blocked": result.blocked,
        "cache_hit": result.cache_hit,
        "latency_ms": result.latency_ms,
        "tokens": {
            "in": int(result.meta.get("tokens_in") or 0),
            "out": int(result.meta.get("tokens_out") or 0),
        },
        "decision": result.decision.to_dict(),
        "proposal": result.proposal,
        "guardrails": result.guardrails.to_dict() if result.guardrails else None,
        "trace": result.trace.to_dict() if result.trace else None,
    }


def recent_traces(deps: Any, scenario: str, limit: int = 50) -> list[dict[str, Any]]:
    return TraceStore(log_dir=deps.log_dir, scenario=scenario).recent(limit)


def _architecture_detail(change: dict[str, Any], es: bool) -> str:
    ck = change.get("kind")
    if ck == "rounds":
        value = change.get("total_rounds")
        return f"ajustar las rondas a {value}" if es else f"adjust the rounds to {value}"
    if ck == "epochs":
        value = change.get("epochs")
        return f"ajustar las epochs locales a {value}" if es \
            else f"adjust the local epochs to {value}"
    if ck == "batch":
        value = change.get("batch_size")
        return f"cambiar el batch a {value}" if es else f"change the batch size to {value}"
    if ck == "model":
        value = change.get("to_model")
        return f"cambiar el modelo a {value}" if es else f"switch the model to {value}"
    return f"cambiar {ck}" if es else f"change {ck}"


def _proposal_detail(proposal: dict[str, Any], es: bool) -> str:
    kind = proposal.get("kind")
    params = proposal.get("params") or {}
    if kind == "select_aggregator":
        return f"cambiar el agregador a {params.get('algorithm')}" if es \
            else f"switch the aggregator to {params.get('algorithm')}"
    if kind == "pause_node":
        return f"pausar el nodo {params.get('node')}" if es else f"pause node {params.get('node')}"
    if kind == "propose_architecture_change":
        return _architecture_detail(params.get("change") or {}, es)
    if kind == "propose_new_simulation":
        return "lanzar una nueva simulación con esos cambios" if es \
            else "start a new simulation with those changes"
    return ""


def decision_reply(result: CrewResult, language: str = "es") -> str:
    decision = result.decision
    es = str(language).lower().startswith("es")
    if result.blocked:
        vetoes = "; ".join((result.guardrails.vetoes if result.guardrails else [])[:3])
        return (
            f"No te propongo ese cambio: lo han frenado los guardrails ({vetoes}). "
            "Mejor dejar la configuración como está por ahora."
            if es
            else f"I wouldn't make that change: the guardrails stopped it ({vetoes}). "
            "Better to leave the configuration as it is for now."
        )
    if not result.proposal:
        rationale = decision.global_rationale
        return (
            f"Con lo que veo, yo dejaría la configuración como está. {rationale}"
            if es
            else f"From what I can see, I'd keep the configuration as it is. {rationale}"
        )
    detail = _proposal_detail(result.proposal, es)
    rationale = decision.global_rationale
    if es:
        return (
            f"Mi propuesta: {detail}. {rationale} "
            "Si te encaja, lo aplicamos; nada se ejecuta hasta que confirmes."
        )
    return (
        f"My suggestion: {detail}. {rationale} "
        "If that works for you, we apply it; nothing runs until you confirm."
    )


__all__ = [
    "build_frontend_context",
    "crew_result_payload",
    "decision_reply",
    "recent_traces",
    "run_crew",
]
