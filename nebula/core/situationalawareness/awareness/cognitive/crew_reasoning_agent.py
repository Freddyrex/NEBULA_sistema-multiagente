"""CrewAI-backed advisory agent for the aggregator node (SMAC, Fase 4).

The controller and the node share the same decision contract
(:mod:`nebula.agentic`), but this agent only proposes commands that already
exist in the node's arbitration repertoire. It is advisory and off by default:

- periodic invocation every ``invoke_every_n_rounds`` round ends;
- optional last-resort tie-break (``invoke_on_tiebreak``);
- bounded total budget (default 8 s), silent degradation to
  :class:`GeminiReasoning`-style ``no_action`` on any failure;
- every crew decision is appended to ``crew_trace.jsonl`` when a log dir is
  configured, never containing raw addresses.

Enable with ``sar_components.crew_reasoning_agent = true`` plus an explicit
``crew_reasoning_agent.enabled = true``; the API key is read from the
environment (``GEMINI_API_KEY`` / ``GOOGLE_API_KEY``) and never stored in the
scenario JSON.
"""

from __future__ import annotations

import logging
from typing import Any

from nebula.agentic.config import CrewConfig
from nebula.agentic.context import build_node_context_pack
from nebula.agentic.orchestrator import CrewOrchestrator, CrewResult
from nebula.agentic.trace import TraceStore
from nebula.core.nebulaevents import RoundEndEvent
from nebula.core.situationalawareness.awareness.cognitive.cognitiveagent import CognitiveAgent
from nebula.core.situationalawareness.awareness.cognitive.gemini_reasoning_agent import (
    DEFAULT_MODEL,
    anonymize_addr,
    resolve_agent_api_key,
)
from nebula.core.situationalawareness.awareness.sautils.sacommand import SACommand
from nebula.core.situationalawareness.awareness.sautils.samoduleagent import SAModuleAgent
from nebula.core.situationalawareness.awareness.suggestionbuffer import SuggestionBuffer


def _as_decision(result: CrewResult) -> dict[str, Any] | None:  # noqa: C901 - explicit lever map
    """Translate a crew proposal into the node's structured decision schema."""
    proposal = result.proposal if result.actionable else None
    if not proposal:
        return None
    kind = str(proposal.get("kind") or "")
    params = proposal.get("params") if isinstance(proposal.get("params"), dict) else {}
    decision: dict[str, Any] | None = None
    if kind == "select_aggregator":
        algorithm = str(params.get("algorithm") or "").strip()
        if algorithm:
            decision = {
                "decision": "select_aggregator",
                "parameters": {"algorithm": algorithm, "node": params.get("node") or "all"},
            }
    elif kind == "pause_node":
        node = str(params.get("node") or "").strip()
        if node:
            decision = {
                "decision": "adjust_training_policy",
                "parameters": {"pause_node": node, "policy": "pause"},
            }
    elif kind == "propose_architecture_change":
        change = params.get("change") if isinstance(params.get("change"), dict) else {}
        change_kind = str(change.get("kind") or "").strip().lower()
        value = change.get("value")
        if change_kind == "topology":
            decision = {
                "decision": "request_topology_change",
                "parameters": {"change": change},
            }
        elif change_kind in ("rounds", "epochs", "model", "batch", "batch_size"):
            decision = {
                "decision": "adjust_training_policy",
                "parameters": {change_kind: value, "change": change},
            }
        else:
            decision = {"decision": "flag_for_human_review", "parameters": {"change": change}}
    elif kind == "propose_new_simulation":
        decision = {
            "decision": "flag_for_human_review",
            "parameters": {
                "reason": "structural change requires a new simulation",
                "changes": params.get("changes"),
            },
        }
    if decision is None:
        return None
    decision["confidence"] = max(0.0, min(1.0, float(result.decision.confidence)))
    decision["explanation"] = (result.decision.global_rationale or "crew proposal")[:500]
    decision["priority"] = "LOW"
    decision["target_agent"] = "crew_reasoning_agent"
    return decision


class CrewReasoning(CognitiveAgent):
    """CrewAI advisory agent for the aggregator node (off by default)."""

    AGENT_FAMILY = "CrewReasoning"

    def __init__(self, config: dict):
        super().__init__(config)
        self._enabled = bool(config.get("enabled", False))
        self._invoke_every_n_rounds = max(1, int(config.get("invoke_every_n_rounds", 5)))
        self._invoke_on_tiebreak = bool(config.get("invoke_on_tiebreak", False))
        self._scenario_name = str(config.get("scenario_name") or "")
        self._log_dir = config.get("log_dir")
        self._api_key = resolve_agent_api_key(config)
        self._cfg = CrewConfig(
            enabled=self._enabled,
            strategy=str(config.get("strategy") or "auto"),
            specialist_model=str(config.get("specialist_model") or config.get("model") or DEFAULT_MODEL),
            coordinator_model=str(config.get("coordinator_model") or DEFAULT_MODEL),
            agent_timeout_s=max(1.0, float(config.get("agent_timeout_s", 6.0))),
            total_timeout_s=max(1.0, float(config.get("total_timeout_s", 8.0))),
            cache_ttl_s=max(0.0, float(config.get("cache_ttl_s", 45.0))),
            max_agents=max(1, min(5, int(config.get("max_agents", 4)))),
            debate_enabled=bool(config.get("debate_enabled", False)),
            debate_max_turns=max(1, min(3, int(config.get("debate_max_turns", 2)))),
            max_rpm=max(1, int(config.get("max_rpm", 30))),
            # The node only knows its own view; global fairness/min-client checks
            # remain the controller's responsibility.
            min_clients=max(1, int(config.get("min_clients", 1))),
            max_exclusion_ratio=max(0.0, min(1.0, float(config.get("max_exclusion_ratio", 0.4)))),
            use_crewai=bool(config.get("use_crewai", False)),
            trigger=str(config.get("trigger") or "round_end"),
            temperature=max(0.0, min(1.0, float(config.get("temperature", 0.2)))),
            max_tokens=max(256, int(config.get("max_completion_tokens", 1200))),
            require_human_review=bool(config.get("require_human_review", True)),
        )
        self._orchestrator: CrewOrchestrator | None = None
        self._rounds_seen = 0
        self._agent = CrewReasoningAgent(self)
        if not self._api_key:
            logging.warning("CrewReasoning | no API key found, agent disabled (safe degradation)")

    @property
    def enabled(self) -> bool:
        return bool(self._enabled and self._api_key)

    @property
    def invoke_on_tiebreak(self) -> bool:
        return bool(self.enabled and self._invoke_on_tiebreak)

    def _get_orchestrator(self) -> CrewOrchestrator:
        if self._orchestrator is None:
            self._orchestrator = CrewOrchestrator(
                self._cfg,
                api_key=self._api_key,
                trace_store=(
                    TraceStore(log_dir=self._log_dir, scenario=self._scenario_name or "node")
                    if self._log_dir
                    else None
                ),
            )
        return self._orchestrator

    async def init(self):
        await self._agent.register_sa_agent()
        await super().init()

    async def _build_snapshot(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Aggregate anonymized context from the node's sibling agents."""
        agents_summary: dict[str, Any] = {}
        sar = self._sar
        if sar is not None:
            for name in (
                "sanetwork",
                "satraining",
                "stragglerpredictor",
                "aggregationselector",
                "resourcebroker",
            ):
                comp = sar.get_component(name) if hasattr(sar, "get_component") else None
                if comp is None:
                    continue
                try:
                    summary = await comp.snapshot() if hasattr(comp, "snapshot") else {"agent": comp.__class__.__name__}
                    agents_summary[name] = self._anonymize(summary)
                except Exception as e:
                    logging.debug(f"CrewReasoning | cannot snapshot {name}: {e}")
        snapshot = {
            "round": getattr(sar, "current_round", None),
            "node": anonymize_addr(self._addr),
            "status": "running",
            "agents": agents_summary,
        }
        if extra:
            snapshot.update(extra)
        return snapshot

    def _anonymize(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: self._anonymize(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self._anonymize(v) for v in obj]
        if isinstance(obj, str) and ":" in obj:
            head, _, tail = obj.partition(":")
            if head and tail and all(c.isdigit() or c == "." for c in head):
                return anonymize_addr(obj)
        return obj

    async def _decide(self, snapshot: dict[str, Any]) -> CrewResult | None:
        ctx = build_node_context_pack(
            snapshot,
            scenario=self._scenario_name or "node",
            node=str(snapshot.get("node") or ""),
            min_clients=self._cfg.min_clients,
            max_exclusion_ratio=self._cfg.max_exclusion_ratio,
        )
        try:
            return await self._get_orchestrator().decide(
                self._scenario_name or "node",
                context=ctx,
                strategy=self._cfg.strategy,
                trigger=self._cfg.trigger,
                role="user",
            )
        except Exception as e:
            logging.warning(f"CrewReasoning | crew failed: {e}")
            return None

    async def sa_component_actions(self):
        """Periodic advisory invocation (never blocks the FL round)."""
        self._rounds_seen += 1
        if not self.enabled or (self._rounds_seen % self._invoke_every_n_rounds != 0):
            await self._agent.notify_all_suggestions_done(RoundEndEvent)
            return
        result = await self._decide(await self._build_snapshot())
        command = self._command_from_result(result)
        if command is not None:
            if self._verbose:
                logging.info(f"CrewReasoning | suggesting {command.get_action().value}")
            await self._agent.suggest_action(command)
        await self._agent.notify_all_suggestions_done(RoundEndEvent)

    def _command_from_result(self, result: CrewResult | None) -> SACommand | None:
        if result is None:
            return None
        decision = _as_decision(result)
        if decision is None:
            return None
        try:
            return SACommand.from_llm_decision(decision, self._agent)
        except ValueError as e:
            logging.warning(f"CrewReasoning | invalid decision mapping: {e}")
            return None

    async def resolve_tie(self, cmd1: SACommand, cmd2: SACommand) -> SACommand | None:
        """Resolve an arbitration tie with the crew, bounded and fail-open."""
        if not self.invoke_on_tiebreak:
            return None
        result = await self._decide(
            await self._build_snapshot(
                extra={
                    "tie_break": {
                        "candidates": [cmd1.to_dict(), cmd2.to_dict()],
                        "instruction": "Endorse one candidate action or abstain.",
                    }
                }
            )
        )
        command = self._command_from_result(result)
        if command is None:
            return None
        action = command.get_action()
        if action is cmd1.get_action():
            return cmd1
        if action is cmd2.get_action():
            return cmd2
        return None

    async def stop(self):
        logging.info("🛑  Stopping CrewReasoning...")


class CrewReasoningAgent(SAModuleAgent):
    def __init__(self, reasoning: CrewReasoning):
        self._reasoning = reasoning

    async def get_agent(self) -> str:
        return "CrewReasoning_MainAgent"

    async def register_sa_agent(self):
        await SuggestionBuffer.get_instance().register_event_agents(RoundEndEvent, self)

    async def suggest_action(self, sac):
        await SuggestionBuffer.get_instance().register_suggestion(RoundEndEvent, self, sac)

    async def notify_all_suggestions_done(self, event_type):
        await SuggestionBuffer.get_instance().notify_all_suggestions_done_for_agent(self, event_type)


__all__ = ["CrewReasoning", "CrewReasoningAgent"]
