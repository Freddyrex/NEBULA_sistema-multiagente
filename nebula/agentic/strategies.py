"""Deterministic and LLM-backed decision strategies.

- ``fast_path``: no LLM, one clear suggestion from the deterministic interpreter.
- ``voting``: parallel specialists + deterministic weighted majority.
- ``role``: specialists selected by finding domain + coordinator synthesis.
- ``debate``: proposal + adversarial critique + bounded revision (opt-in).
- ``carry_over``: no LLM, keep the previous configuration.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from nebula.agentic.config import CrewConfig
from nebula.agentic.llm import LLMBackend, LLMReply, hash_prompt
from nebula.agentic.patterns import (
    canonical_algorithm,
    canonical_pattern_for_change,
    get_pattern,
    is_allowed,
)
from nebula.agentic.prompts import (
    coordinator_messages,
    critic_messages,
    revision_messages,
    specialist_messages,
)
from nebula.agentic.schemas import (
    AGENT_VERDICT_SCHEMA,
    CREW_DECISION_SCHEMA,
    AgentVerdict,
    ContextPack,
    CrewDecision,
    Evidence,
    GuardrailReport,
    PatternDecision,
    parse_agent_verdict,
    parse_decision,
)

_HOLD_RATIONALE = "sin evidencia suficiente para un cambio; mantener la configuración actual"


@dataclass
class StrategyResult:
    decision: CrewDecision | None
    strategy: str
    verdicts: list[AgentVerdict] = field(default_factory=list)
    fallback: str | None = None
    prompt_hash: str | None = None
    llm_calls: int = 0
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


def _token_totals(replies: list[LLMReply]) -> tuple[int, int]:
    """Sum prompt/completion tokens from a batch of LLM replies."""
    tin = sum(int(r.input_tokens or 0) for r in replies)
    tout = sum(int(r.output_tokens or 0) for r in replies)
    return tin, tout


def _evidence_from_ctx(ctx: ContextPack, *, limit: int = 4) -> list[Evidence]:
    out: list[Evidence] = []
    for finding in ctx.findings[:limit]:
        raw = (finding.get("evidence") or [{}])[0] if finding.get("evidence") else {}
        source = "findings"
        out.append(
            Evidence(
                source=source,
                ref=f"{finding.get('type')}:{finding.get('id') or 'x'}",
                metric=raw.get("metric") or finding.get("metric"),
                round=raw.get("round"),
                node=(finding.get("nodes") or [None])[0],
                value=raw.get("value"),
                detail=str(finding.get("summary") or "")[:500],
            )
        )
    if out:
        return out
    for name in ("accuracy", "round_time_s", "cpu_percent", "ram_percent"):
        value = (ctx.metrics or {}).get(name)
        if value is not None:
            out.append(
                Evidence(
                    source="metrics_digest",
                    ref=f"round:{ctx.round}",
                    metric=name,
                    round=ctx.round,
                    value=value,
                    detail="métrica final del digest",
                )
            )
            break
    if not out:
        out.append(
            Evidence(
                source="scenario_config",
                ref="scenario.json",
                round=ctx.round,
                detail="sin hallazgos ni métricas disponibles",
            )
        )
    return out


def hold_decision(ctx: ContextPack, rationale: str = _HOLD_RATIONALE) -> CrewDecision:
    pattern = "training_policy"
    if ctx.findings:
        for finding in ctx.findings:
            ftype = str(finding.get("type") or "")
            mapped = {
                "straggler": "aggregation_algorithm",
                "byzantine_suspected": "aggregation_algorithm",
                "ram_pressure": "client_selection",
                "cpu_pressure": "client_selection",
                "thermal_throttle_risk": "client_selection",
                "f1_recall_gap": "model",
                "low_recall": "model",
            }.get(ftype)
            if mapped:
                pattern = mapped
                break
    return CrewDecision(
        patterns={pattern: PatternDecision(value="hold", params={}, confidence=0.5, rationale=rationale)},
        global_rationale=rationale,
        confidence=0.5,
        requires_human_review=True,
        guardrails=GuardrailReport(passed=True, checks=[]),
        evidence=_evidence_from_ctx(ctx),
    )


def _suggestion_pattern(suggestion: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    kind = str(suggestion.get("kind") or "").strip()
    params = suggestion.get("params") if isinstance(suggestion.get("params"), dict) else {}
    change = params.get("change") if isinstance(params.get("change"), dict) else {}
    if kind == "select_aggregator":
        algorithm = canonical_algorithm(params.get("algorithm") or change.get("algorithm"))
        return "aggregation_algorithm", {"algorithm": algorithm}
    if kind == "pause_node":
        node = str(params.get("node") or "").strip()
        return ("client_selection", {"node": node}) if node else None
    if kind == "propose_architecture_change":
        pattern = canonical_pattern_for_change(change)
        if pattern is None:
            return None
        out = dict(params)
        out["change"] = change
        return pattern, out
    if kind == "propose_new_simulation":
        pattern = canonical_pattern_for_change(change)
        if pattern is None:
            return None
        out = dict(params)
        out["change"] = change
        return pattern, out
    return None


def _suggestion_evidence(suggestion: dict[str, Any], ctx: ContextPack) -> list[Evidence]:
    out: list[Evidence] = []
    for raw in suggestion.get("evidence") or []:
        if not isinstance(raw, dict):
            continue
        source = raw.get("source") or "findings"
        if source not in ("scenario_config", "metrics_digest", "metrics_series", "findings",
                          "cost_model", "decision_log", "runtime", "conversation"):
            source = "findings"
        out.append(
            Evidence(
                source=source,
                ref=str(raw.get("ref") or raw.get("detail") or "suggestion")[:200],
                metric=raw.get("metric"),
                round=raw.get("round"),
                node=raw.get("node"),
                value=raw.get("value"),
                detail=str(raw.get("detail") or "")[:500] or None,
            )
        )
    return out[:6] or _evidence_from_ctx(ctx)


def fast_path_decision(
    ctx: ContextPack, suggestions: list[dict[str, Any]] | None
) -> CrewDecision | None:
    """No-LLM decision when exactly one actionable suggestion is available."""
    if not suggestions:
        return None
    actionable: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for suggestion in suggestions:
        if not isinstance(suggestion, dict):
            continue
        mapped = _suggestion_pattern(suggestion)
        if mapped is None:
            continue
        pattern, params = mapped
        key = (pattern, json.dumps(params, sort_keys=True, default=str))
        if key in seen:
            continue
        seen.add(key)
        actionable.append({"pattern": pattern, "params": params, "suggestion": suggestion})
    if len(actionable) != 1:
        return None
    item = actionable[0]
    spec = get_pattern(item["pattern"])
    if spec is None or not spec.available:
        return None
    if spec.action_kind in ("propose_architecture_change", "propose_new_simulation") and not isinstance(
        item["params"].get("cost"), dict
    ):
        return None
    allowed, _reason = is_allowed(item["pattern"], item["params"])
    if not allowed:
        return None
    suggestion = item["suggestion"]
    evidence = _suggestion_evidence(suggestion, ctx)
    rationale = str(suggestion.get("reason") or suggestion.get("title") or "sugerencia determinista")
    return CrewDecision(
        patterns={
            item["pattern"]: PatternDecision(
                value="change",
                params=item["params"],
                confidence=0.85,
                rationale=rationale[:800],
            )
        },
        global_rationale=rationale[:2000],
        confidence=0.85,
        requires_human_review=True,
        guardrails=GuardrailReport(passed=True, checks=[]),
        evidence=evidence,
    )


def select_specialists(ctx: ContextPack, max_agents: int = 4) -> list[str]:
    """Pick specialist lenses from the finding types (deterministic)."""
    keys: list[str] = []
    types = {str(f.get("type") or "") for f in ctx.findings}
    if types & {"accuracy_plateau", "over_provisioned_rounds"}:
        keys.append("performance")
    if types & {"straggler"}:
        keys.append("network")
    if types & {"cpu_pressure", "ram_pressure", "thermal_throttle_risk"}:
        keys.append("resource")
    if types & {"f1_recall_gap", "low_recall"}:
        keys.append("data")
    if types & {"byzantine_suspected"}:
        keys.extend(["network", "data"])
    defaults = ["performance", "network", "resource", "data"]
    for key in defaults:
        if key not in keys:
            keys.append(key)
    deduped = list(dict.fromkeys(keys))
    return deduped[: max(2, min(5, max_agents))]


def _verdict_key(verdict: AgentVerdict) -> tuple[str, str]:
    return verdict.pattern, json.dumps(verdict.proposed_params or {}, sort_keys=True, default=str)


def _consensus(
    ctx: ContextPack,
    verdicts: list[AgentVerdict],
    *,
    strategy: str,
) -> CrewDecision | None:
    votes: dict[tuple[str, str], list[AgentVerdict]] = {}
    for verdict in verdicts:
        if verdict.decision == "hold":
            continue
        allowed, _reason = is_allowed(verdict.pattern, verdict.proposed_params)
        if not allowed:
            continue
        votes.setdefault(_verdict_key(verdict), []).append(verdict)
    if not votes:
        return None
    total_weight = sum(v.confidence for v in verdicts) or 1.0
    ranked = sorted(
        votes.items(),
        key=lambda item: (sum(v.confidence for v in item[1]), len(item[1])),
        reverse=True,
    )
    (pattern_id, _raw_params), supporters = ranked[0]
    weight = sum(v.confidence for v in supporters)
    second_weight = sum(v.confidence for v in ranked[1][1]) if len(ranked) > 1 else 0.0
    if weight < 0.5 * total_weight and len(supporters) < 2:
        return None
    if second_weight and weight - second_weight < 0.1 and len(supporters) < 2:
        return None
    representative = supporters[0]
    if representative.proposed_params and pattern_id == "aggregation_algorithm":
        representative.proposed_params["algorithm"] = canonical_algorithm(
            representative.proposed_params.get("algorithm")
        )
    evidence: list[Evidence] = []
    for verdict in supporters:
        evidence.extend(verdict.evidence)
        if len(evidence) >= 8:
            break
    rationale = " | ".join(dict.fromkeys(v.rationale for v in supporters))
    return CrewDecision(
        patterns={
            pattern_id: PatternDecision(
                value="change",
                params=representative.proposed_params,
                confidence=min(0.95, weight / max(total_weight, 0.001)),
                rationale=rationale[:800],
            )
        },
        global_rationale=rationale[:2000],
        confidence=min(0.95, weight / max(total_weight, 0.001)),
        requires_human_review=True,
        guardrails=GuardrailReport(passed=True, checks=[]),
        evidence=evidence[:24],
    )


async def _run_specialists(
    ctx: ContextPack,
    backend: LLMBackend,
    cfg: CrewConfig,
    keys: list[str],
    *,
    deadline: float,
) -> tuple[list[AgentVerdict], list[LLMReply], str | None]:
    prompts = [specialist_messages(key, ctx) for key in keys]
    timeout = max(1.0, min(cfg.agent_timeout_s, deadline))

    async def _one(messages: list[dict[str, str]]) -> LLMReply:
        return await backend.complete_json(
            messages,
            schema=AGENT_VERDICT_SCHEMA,
            model=cfg.specialist_model,
            timeout_s=timeout,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            reasoning_effort="low",
        )

    replies = await asyncio.gather(*(_one(p) for p in prompts), return_exceptions=True)
    verdicts: list[AgentVerdict] = []
    clean: list[LLMReply] = []
    for reply in replies:
        if isinstance(reply, Exception):
            clean.append(LLMReply(None, 0, error=type(reply).__name__))
            continue
        clean.append(reply)
        if reply.data is not None:
            parsed = parse_agent_verdict(reply.data)
            if parsed is not None:
                verdicts.append(parsed)
    prompt_hash = hash_prompt(prompts[0]) if prompts else None
    return verdicts, clean, prompt_hash


class VotingStrategy:
    def __init__(self, backend: LLMBackend, cfg: CrewConfig):
        self._backend = backend
        self._cfg = cfg

    async def decide(self, ctx: ContextPack, *, deadline: float) -> StrategyResult:
        keys = select_specialists(ctx, self._cfg.max_agents)
        verdicts, replies, prompt_hash = await _run_specialists(
            ctx, self._backend, self._cfg, keys, deadline=deadline
        )
        tin, tout = _token_totals(replies)
        decision = _consensus(ctx, verdicts, strategy="voting")
        if decision is None:
            return StrategyResult(
                decision=None,
                strategy="voting",
                verdicts=verdicts,
                fallback="no_consensus",
                prompt_hash=prompt_hash,
                llm_calls=len(replies),
                input_tokens=tin,
                output_tokens=tout,
                meta={"specialists": keys},
            )
        return StrategyResult(
            decision=decision,
            strategy="voting",
            verdicts=verdicts,
            prompt_hash=prompt_hash,
            llm_calls=len(replies),
            input_tokens=tin,
            output_tokens=tout,
            meta={"specialists": keys},
        )


class RoleStrategy:
    def __init__(self, backend: LLMBackend, cfg: CrewConfig):
        self._backend = backend
        self._cfg = cfg

    async def decide(self, ctx: ContextPack, *, deadline: float) -> StrategyResult:
        keys = select_specialists(ctx, self._cfg.max_agents)
        primary = [keys[0]]
        helper = keys[1:2]
        verdicts, replies, prompt_hash = await _run_specialists(
            ctx, self._backend, self._cfg, primary + helper, deadline=deadline
        )
        tin, tout = _token_totals(replies)
        if not verdicts:
            return StrategyResult(
                None,
                "role",
                fallback="no_verdict",
                llm_calls=len(replies),
                input_tokens=tin,
                output_tokens=tout,
            )
        if len(verdicts) == 1:
            decision = _consensus(ctx, verdicts, strategy="role")
            return StrategyResult(
                decision=decision,
                strategy="role",
                verdicts=verdicts,
                fallback=None if decision else "no_consensus",
                prompt_hash=prompt_hash,
                llm_calls=len(replies),
                input_tokens=tin,
                output_tokens=tout,
                meta={"specialists": primary + helper},
            )
        coordinator_timeout = max(1.0, min(self._cfg.agent_timeout_s, deadline))
        reply = await self._backend.complete_json(
            coordinator_messages(ctx, [v.to_dict() for v in verdicts]),
            schema=CREW_DECISION_SCHEMA,
            model=self._cfg.coordinator_model,
            timeout_s=coordinator_timeout,
            temperature=self._cfg.temperature,
            max_tokens=self._cfg.max_tokens,
            reasoning_effort="low",
        )
        tin += int(reply.input_tokens or 0)
        tout += int(reply.output_tokens or 0)
        decision = parse_decision(reply.data) if reply.data else None
        if decision is None:
            decision = _consensus(ctx, verdicts, strategy="role")
        return StrategyResult(
            decision=decision,
            strategy="role",
            verdicts=verdicts,
            fallback=None if decision else "coordinator_failed",
            prompt_hash=prompt_hash,
            llm_calls=len(replies) + 1,
            input_tokens=tin,
            output_tokens=tout,
            meta={"specialists": primary + helper},
        )


class DebateStrategy:
    def __init__(self, backend: LLMBackend, cfg: CrewConfig, *, base: RoleStrategy | None = None):
        self._backend = backend
        self._cfg = cfg
        self._base = base or RoleStrategy(backend, cfg)

    async def decide(self, ctx: ContextPack, *, deadline: float) -> StrategyResult:
        base = await self._base.decide(ctx, deadline=deadline)
        if base.decision is None:
            return base
        proposal = base.decision.to_dict()
        critique = await self._backend.complete_json(
            critic_messages(ctx, proposal),
            schema=AGENT_VERDICT_SCHEMA,
            model=self._cfg.coordinator_model,
            timeout_s=max(1.0, min(self._cfg.agent_timeout_s, deadline)),
            temperature=self._cfg.temperature,
            max_tokens=self._cfg.max_tokens,
        )
        tin = base.input_tokens + int(critique.input_tokens or 0)
        tout = base.output_tokens + int(critique.output_tokens or 0)
        verdicts = list(base.verdicts)
        if critique.data:
            parsed = parse_agent_verdict(critique.data)
            if parsed is not None:
                verdicts.append(parsed)
        turns = 0
        current = base.decision
        while turns < max(1, self._cfg.debate_max_turns - 1):
            turns += 1
            safety = current.guardrails.to_dict()
            revision = await self._backend.complete_json(
                revision_messages(ctx, current.to_dict(), critique.data or {}, safety),
                schema=CREW_DECISION_SCHEMA,
                model=self._cfg.coordinator_model,
                timeout_s=max(1.0, min(self._cfg.agent_timeout_s, deadline)),
                temperature=self._cfg.temperature,
                max_tokens=self._cfg.max_tokens,
            )
            tin += int(revision.input_tokens or 0)
            tout += int(revision.output_tokens or 0)
            revised = parse_decision(revision.data) if revision.data else None
            if revised is None:
                break
            current = revised
        return StrategyResult(
            decision=current,
            strategy="debate",
            verdicts=verdicts,
            prompt_hash=base.prompt_hash,
            llm_calls=base.llm_calls + 1 + turns,
            input_tokens=tin,
            output_tokens=tout,
            meta={**base.meta, "debate_turns": turns},
        )


class FastPathStrategy:
    async def decide(self, ctx: ContextPack, suggestions: list[dict[str, Any]] | None) -> CrewDecision | None:
        return fast_path_decision(ctx, suggestions)


def carry_over_result(ctx: ContextPack, reason: str = "deadline") -> StrategyResult:
    decision = hold_decision(ctx, f"carry_over: {reason}; se mantiene la configuración anterior")
    decision.confidence = 0.4
    return StrategyResult(decision=decision, strategy="carry_over", fallback=reason)


__all__ = [
    "DebateStrategy",
    "FastPathStrategy",
    "RoleStrategy",
    "StrategyResult",
    "VotingStrategy",
    "carry_over_result",
    "fast_path_decision",
    "hold_decision",
    "select_specialists",
]
