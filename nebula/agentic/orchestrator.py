"""Crew orchestrator: cache → fast-path → strategy → guardrails → proposal/trace.

The orchestrator only *proposes*. Turning the proposal into a pending action,
asking for human confirmation and executing it remains in the existing SMAC
``propose → confirm → execute`` flow.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nebula.agentic.config import CrewConfig, load_crew_config
from nebula.agentic.context import build_context_pack
from nebula.agentic.guardrails import GuardrailEngine
from nebula.agentic.llm import GeminiBackend, LLMBackend
from nebula.agentic.patterns import canonical_algorithm, get_pattern
from nebula.agentic.schemas import (
    ContextPack,
    CrewDecision,
    CrewTrace,
    GuardrailReport,
    utc_now,
)
from nebula.agentic.strategies import (
    DebateStrategy,
    RoleStrategy,
    StrategyResult,
    VotingStrategy,
    carry_over_result,
    fast_path_decision,
    hold_decision,
    select_specialists,
)
from nebula.agentic.trace import TraceStore

_RPM_WINDOW_S = 60.0
_RPM_LOCK = asyncio.Lock()
_RPM_CALLS: deque[float] = deque()


async def _reserve_llm_calls(calls: int, max_rpm: int, deadline: float) -> bool:
    """Global sliding-window limiter shared by all orchestrator instances."""
    if max_rpm <= 0 or calls <= 0:
        return True
    calls = min(calls, max_rpm)
    async with _RPM_LOCK:
        now = time.monotonic()
        while _RPM_CALLS and now - _RPM_CALLS[0] > _RPM_WINDOW_S:
            _RPM_CALLS.popleft()
        if len(_RPM_CALLS) + calls > max_rpm:
            wait = _RPM_WINDOW_S - (now - _RPM_CALLS[0]) + 0.01 if _RPM_CALLS else 0.0
            if now + wait >= deadline:
                return False
            await asyncio.sleep(wait)
            now = time.monotonic()
            while _RPM_CALLS and now - _RPM_CALLS[0] > _RPM_WINDOW_S:
                _RPM_CALLS.popleft()
            if len(_RPM_CALLS) + calls > max_rpm:
                return False
        _RPM_CALLS.extend(now for _ in range(calls))
        return True


CREW_METRICS: dict[str, Any] = {
    "decisions": 0,
    "llm_calls": 0,
    "cache_hits": 0,
    "fallbacks": 0,
    "blocked": 0,
    "latencies_ms": [],
}


@dataclass
class CrewResult:
    decision: CrewDecision
    strategy: str
    proposal: dict[str, Any] | None = None
    trace: CrewTrace | None = None
    fallback: str | None = None
    blocked: bool = False
    cache_hit: bool = False
    latency_ms: int = 0
    guardrails: GuardrailReport | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def actionable(self) -> bool:
        return bool(self.proposal) and not self.blocked and self.decision.guardrails.passed


class CrewOrchestrator:
    def __init__(
        self,
        cfg: CrewConfig,
        *,
        api_key: str = "",
        backend: LLMBackend | None = None,
        guardrails: GuardrailEngine | None = None,
        trace_store: TraceStore | None = None,
        metric_callback: Callable[[str, Any], None] | None = None,
    ):
        self.cfg = cfg
        self._api_key = api_key
        self._backend = backend or GeminiBackend(api_key)
        self._guardrails = guardrails or GuardrailEngine(
            min_clients=cfg.min_clients, max_exclusion_ratio=cfg.max_exclusion_ratio
        )
        self._trace_store = trace_store
        self._metric_callback = metric_callback
        self._cache: dict[tuple[str, str], tuple[float, CrewResult]] = {}

    def _metric(self, name: str, value: Any = 1) -> None:
        try:
            if name == "latency_ms":
                values = CREW_METRICS["latencies_ms"]
                values.append(int(value))
                del values[:-200]
            else:
                CREW_METRICS[name] = CREW_METRICS.get(name, 0) + value
        except Exception as e:
            logging.debug(f"SMAC crew | metric update failed: {e}")
        if self._metric_callback is not None:
            try:
                callback_name = name if str(name).startswith("crew_") else f"crew_{name}"
                self._metric_callback(callback_name, value)
            except Exception as e:
                logging.debug(f"SMAC crew | metric callback failed: {e}")

    def _cached(self, scenario: str, state_hash: str) -> CrewResult | None:
        if self.cfg.cache_ttl_s <= 0:
            return None
        entry = self._cache.get((scenario, state_hash))
        if entry is None:
            return None
        timestamp, result = entry
        if time.monotonic() - timestamp > self.cfg.cache_ttl_s:
            self._cache.pop((scenario, state_hash), None)
            return None
        result.cache_hit = True
        self._metric("cache_hits")
        return result

    def _store(self, scenario: str, state_hash: str, result: CrewResult) -> None:
        if self.cfg.cache_ttl_s <= 0:
            return
        self._cache[(scenario, state_hash)] = (time.monotonic(), result)
        if len(self._cache) > 128:
            oldest = sorted(self._cache.items(), key=lambda item: item[1][0])[:32]
            for key, _value in oldest:
                self._cache.pop(key, None)

    def _select_auto(self, ctx: ContextPack, suggestions: list[dict[str, Any]]) -> str:
        if fast_path_decision(ctx, suggestions) is not None:
            return "fast_path"
        if not ctx.findings and not suggestions:
            return "fast_path"
        domains = len(select_specialists(ctx, self.cfg.max_agents))
        critical = any(str(f.get("severity") or "").lower() == "critical" for f in ctx.findings)
        if self.cfg.debate_enabled and critical:
            return "debate"
        if critical or domains > 2:
            return "role"
        return "voting"

    async def decide(  # noqa: C901 - pipeline readability over a state machine
        self,
        scenario: str,
        *,
        state: dict[str, Any] | None = None,
        context: ContextPack | None = None,
        strategy: str | None = None,
        trigger: str = "manual",
        user: str = "",
        role: str = "user",
        suggestions: list[dict[str, Any]] | None = None,
        config_dir: str | None = None,
    ) -> CrewResult:
        started = time.monotonic()
        if not self.cfg.enabled:
            decision = hold_decision(
                context or build_context_pack(state or {}, scenario=scenario, user=user),
                "multi_agent desactivado; se mantiene el flujo single-agent",
            )
            return CrewResult(decision=decision, strategy="disabled", fallback="disabled")

        ctx = context or build_context_pack(
            state or {},
            scenario=scenario,
            user=user,
            min_clients=self.cfg.min_clients,
            max_exclusion_ratio=self.cfg.max_exclusion_ratio,
        )
        candidates = list(suggestions or [])
        if not candidates:
            candidates = list((ctx.costs or {}).get("candidates") or [])
        cached = self._cached(scenario, ctx.state_hash)
        if cached is not None:
            cached.latency_ms = int((time.monotonic() - started) * 1000)
            return cached

        requested = str(strategy or self.cfg.strategy or "auto").strip().lower()
        chosen = self._select_auto(ctx, candidates) if requested == "auto" else requested
        deadline = started + max(1.0, self.cfg.total_timeout_s)

        result: StrategyResult
        if chosen == "carry_over":
            result = carry_over_result(ctx, "requested")
        elif chosen == "fast_path":
            decision = fast_path_decision(ctx, candidates)
            if decision is None and not ctx.findings and not candidates:
                decision = hold_decision(ctx, "sin hallazgos ni sugerencias; mantener configuración")
            if decision is not None:
                result = StrategyResult(decision=decision, strategy="fast_path")
            else:
                result = await self._run_llm_strategy("voting", ctx, deadline)
        elif chosen == "voting":
            result = await self._run_llm_strategy("voting", ctx, deadline)
            if result.decision is None and (deadline - time.monotonic()) > 2.0:
                fallback_role = await self._run_llm_strategy("role", ctx, deadline)
                if fallback_role.decision is not None:
                    result = fallback_role
        elif chosen == "role":
            result = await self._run_llm_strategy("role", ctx, deadline)
        elif chosen == "debate":
            result = await self._run_llm_strategy("debate", ctx, deadline)
        else:
            result = carry_over_result(ctx, f"estrategia desconocida: {chosen}")

        if result.decision is None:
            result = carry_over_result(ctx, result.fallback or "no_decision")
            self._metric("fallbacks")

        decision = result.decision
        report = self._guardrails.validate(decision, ctx, role=role)
        decision.guardrails = report
        blocked = not report.passed
        if blocked:
            self._metric("blocked")
            vetoed = hold_decision(ctx, "vetado por guardrails: " + "; ".join(report.vetoes[:3]))
            vetoed.guardrails = report
            vetoed.evidence = decision.evidence or vetoed.evidence
            decision = vetoed

        proposal = None if blocked else self._to_proposal(decision, ctx)
        latency_ms = int((time.monotonic() - started) * 1000)
        trace = CrewTrace(
            trace_id=f"crew-{ctx.state_hash}-{int(time.time())}",
            scenario=scenario,
            round=ctx.round,
            strategy=result.strategy,
            state_hash=ctx.state_hash,
            started_at=utc_now(),
            finished_at=utc_now(),
            latency_ms=latency_ms,
            fallback=result.fallback,
            agents=[{"agent": v.agent, "pattern": v.pattern, "decision": v.decision} for v in result.verdicts],
            verdicts=[v.to_dict() for v in result.verdicts],
            final_decision=decision.to_dict(),
            guardrails=report.to_dict(),
            prompt_hash=result.prompt_hash,
            llm_calls=result.llm_calls,
            tokens_in=result.input_tokens,
            tokens_out=result.output_tokens,
            error=None if result.decision is not None else result.fallback,
        )
        decision.trace_id = trace.trace_id
        if self._trace_store is not None:
            self._trace_store.record(trace)
        self._metric("decisions")
        self._metric("llm_calls", result.llm_calls)
        self._metric("tokens_in", result.input_tokens)
        self._metric("tokens_out", result.output_tokens)
        self._metric("latency_ms", latency_ms)
        result.meta.setdefault("tokens_in", result.input_tokens)
        result.meta.setdefault("tokens_out", result.output_tokens)

        outcome = CrewResult(
            decision=decision,
            strategy=result.strategy,
            proposal=proposal,
            trace=trace,
            fallback=result.fallback,
            blocked=blocked,
            latency_ms=latency_ms,
            guardrails=report,
            meta=result.meta,
        )
        self._store(scenario, ctx.state_hash, outcome)
        return outcome

    async def _run_llm_strategy(self, name: str, ctx: ContextPack, deadline: float) -> StrategyResult:
        remaining = deadline - time.monotonic()
        if remaining <= 0.5 or not getattr(self._backend, "enabled", True):
            return carry_over_result(ctx, "deadline" if remaining <= 0.5 else "llm_disabled")
        expected_calls = {
            "voting": max(1, self.cfg.max_agents),
            "role": min(3, max(1, self.cfg.max_agents)),
            "debate": max(2, 2 * self.cfg.debate_max_turns),
        }.get(name, 1)
        if not await _reserve_llm_calls(expected_calls, self.cfg.max_rpm, deadline):
            self._metric("rate_limited")
            return carry_over_result(ctx, "rate_limit")
        if name == "voting":
            strategy = VotingStrategy(self._backend, self.cfg)
        elif name == "role":
            strategy = RoleStrategy(self._backend, self.cfg)
        else:
            strategy = DebateStrategy(self._backend, self.cfg)
        try:
            return await asyncio.wait_for(strategy.decide(ctx, deadline=deadline), timeout=max(0.5, remaining))
        except TimeoutError:
            self._metric("fallbacks")
            return carry_over_result(ctx, "deadline")
        except Exception as e:
            logging.warning(f"SMAC crew | strategy {name} failed: {e}")
            self._metric("fallbacks")
            return carry_over_result(ctx, f"error:{type(e).__name__}")

    @staticmethod
    def _change_from_params(params: dict[str, Any]) -> dict[str, Any]:
        """Accept nested ``change`` or the flat shape used in prompts/UI."""
        nested = params.get("change")
        if isinstance(nested, dict) and nested:
            return dict(nested)
        kind = str(params.get("kind") or "").strip().lower()
        if kind == "rounds" and params.get("total_rounds") is not None:
            return {"kind": "rounds", "total_rounds": params["total_rounds"]}
        if kind == "epochs" and params.get("local_epochs", params.get("epochs")) is not None:
            return {"kind": "epochs", "epochs": params.get("local_epochs", params.get("epochs"))}
        if kind in ("batch", "batch_size") and params.get("batch_size") is not None:
            return {"kind": "batch", "batch_size": params["batch_size"]}
        if kind == "model" and params.get("model") is not None:
            return {"kind": "model", "to_model": params["model"]}
        if kind in ("topology", "federation") and params.get(kind) is not None:
            return {"kind": kind, kind: params[kind]}
        if kind == "aggregator" and params.get("algorithm") is not None:
            return {"kind": "aggregator", "algorithm": params["algorithm"]}
        return {}

    def _to_proposal(  # noqa: C901 - one branch per supported lever
        self, decision: CrewDecision, ctx: ContextPack
    ) -> dict[str, Any] | None:
        actionable = [
            (pattern_id, pattern)
            for pattern_id, pattern in decision.patterns.items()
            if pattern.value != "hold" and get_pattern(pattern_id) is not None
        ]
        if not actionable:
            return None
        pattern_id, pattern = max(actionable, key=lambda item: item[1].confidence)
        spec = get_pattern(pattern_id)
        if spec is None:
            return None
        params = dict(pattern.params or {})
        description = (pattern.rationale or decision.global_rationale or "")[:300]
        if spec.action_kind == "select_aggregator":
            algorithm = canonical_algorithm(params.get("algorithm") or params.get("value"))
            if not algorithm:
                return None
            return {
                "kind": "select_aggregator",
                "params": {"node": params.get("node") or "all", "algorithm": algorithm},
                "description": description,
            }
        if spec.action_kind == "pause_node":
            node = str(params.get("node") or "").strip()
            if not node:
                return None
            return {"kind": "pause_node", "params": {"node": node}, "description": description}
        if spec.action_kind == "propose_architecture_change":
            change = self._change_from_params(params)
            if not change:
                return None
            return {
                "kind": "propose_architecture_change",
                "params": {"change": change, "cost": params.get("cost"), "trigger": "crew"},
                "description": description,
            }
        if spec.action_kind == "propose_new_simulation":
            change = self._change_from_params(params)
            if not change:
                return None
            changes = params.get("changes")
            if not isinstance(changes, dict) or not changes:
                changes = {str(change.get("kind") or "change"): change}
            return {
                "kind": "propose_new_simulation",
                "params": {
                    "parent": params.get("parent") or ctx.scenario.get("name"),
                    "changes": changes,
                    "cost": params.get("cost"),
                    "explanation": description,
                },
                "description": description,
            }
        return None


def build_orchestrator(
    config_dir: str | None,
    scenario: str,
    *,
    api_key: str,
    state: dict[str, Any] | None = None,
    use_crewai: bool | None = None,
    log_dir: str | None = None,
    metric_callback: Callable[[str, Any], None] | None = None,
) -> CrewOrchestrator:
    cfg = load_crew_config(config_dir, scenario)
    backend: LLMBackend | None = None
    if use_crewai if use_crewai is not None else cfg.use_crewai:
        from nebula.agentic.agents import CrewAIBackend, crewai_available

        if crewai_available():
            backend = CrewAIBackend(api_key, temperature=cfg.temperature, max_tokens=cfg.max_tokens)
    if backend is None:
        backend = GeminiBackend(api_key)
    return CrewOrchestrator(
        cfg,
        api_key=api_key,
        backend=backend,
        trace_store=TraceStore(log_dir=log_dir, scenario=scenario),
        metric_callback=metric_callback,
    )


__all__ = ["CREW_METRICS", "CrewOrchestrator", "CrewResult", "build_orchestrator"]
