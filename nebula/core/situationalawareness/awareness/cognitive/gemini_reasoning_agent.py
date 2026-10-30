from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

from nebula.core.nebulaevents import RoundEndEvent
from nebula.core.situationalawareness.awareness.cognitive.cognitiveagent import CognitiveAgent
from nebula.core.situationalawareness.awareness.sautils.sacommand import SACommand
from nebula.core.situationalawareness.awareness.sautils.samoduleagent import SAModuleAgent
from nebula.core.situationalawareness.awareness.suggestionbuffer import SuggestionBuffer

DEFAULT_MODEL = "gemini-3.5-flash-lite"

#: Env vars probed (in order) when the scenario config names no environment
#: variable. Keeps scenarios generated before the Gemini migration working.
_FALLBACK_KEY_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

DECISION_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "smac_decision",
        # NOTE: keys rejected by Gemini's structured-output subset (e.g.
        # maxLength, additionalProperties) are stripped inside
        # nebula.core.smac_gemini before sending; structure is enforced by
        # _validate_decision() instead.
        "schema": {
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": [
                        "no_action",
                        "adjust_training_policy",
                        "request_topology_change",
                        "select_aggregator",
                        "flag_for_human_review",
                    ],
                },
                "target_agent": {"type": "string"},
                "parameters": {"type": "object"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "explanation": {"type": "string", "maxLength": 500},
            },
            "required": ["decision", "confidence", "explanation"],
        },
    },
}

SYSTEM_PROMPT = (
    "You are the cognitive advisor of a decentralized federated learning node. "
    "You receive anonymized, aggregated metadata (round statistics, neighbor latency forecasts, "
    "resource pressure, topology summary and the suggestions already emitted by the deterministic "
    "agents of the node). You NEVER see training data, gradients or model weights. "
    "Reply with a single JSON object following the provided schema. "
    "Prefer 'no_action' when the deterministic agents already cover the situation. "
    "Your suggestions are advisory: they compete in a priority arbitration with lower priority "
    "than telemetry-driven agents. Keep 'explanation' under 500 characters. "
    "When breaking a tie, set parameters.preferred_action to the action value of the command "
    "you endorse (or omit it to abstain)."
)


def anonymize_addr(addr: str) -> str:
    """One-way short hash of a node address (privacy: no raw IPs to the LLM)."""
    return hashlib.sha256(str(addr).encode()).hexdigest()[:8]


def resolve_agent_api_key(config: dict[str, Any]) -> str:
    """Resolve the Gemini key for the agent from the environment (never logged).

    Inline keys are rejected: the agent configuration is persisted in
    ``scenario.json`` and served through the frontend, so the key may only
    travel through the environment variable named by ``api_key_env_var``.

    Args:
        config (dict[str, Any]): The agent configuration.

    Returns:
        str: The key read from the environment, or an empty string when unset.

    Raises:
        ValueError: If the configuration carries a non-empty inline ``api_key``.
    """
    explicit = config.get("api_key")
    if explicit and str(explicit).strip():
        message = (
            "Inline 'api_key' is not supported: the Gemini key must be provided through the "
            "environment variable named by 'api_key_env_var'."
        )
        raise ValueError(message)

    env_var = str(config.get("api_key_env_var") or "").strip()
    candidates = [env_var] if env_var else list(_FALLBACK_KEY_ENV_VARS)
    for name in candidates:
        value = os.environ.get(name, "")
        if value:
            return value
    return ""


class GeminiReasoning(CognitiveAgent):
    """LLM-backed advisory agent served through the Gemini API (SMAC, Fase 4).

    Operates strictly outside the critical path of the FL round:

    - periodic invocation every ``invoke_every_n_rounds`` round ends;
    - on-demand invocation as last-resort tie-breaker for ambiguous
      arbitration ties (see ``SAReasoner._resolve_tie_with_cognition``);
    - bounded timeout (default 8 s: Gemini latency is far above Groq's, the
      aggressive 300 ms budget of the Groq era would never complete) with
      silent safe degradation: any transport, parsing or validation error
      yields no suggestion and the node keeps operating with the
      deterministic agents only.

    The API key is never stored in scenario JSON and never logged: it is read
    from the environment variable named in ``api_key_env_var`` (default
    ``GEMINI_API_KEY``, with ``GOOGLE_API_KEY`` fallback). A configuration
    carrying an inline ``api_key`` is rejected. Without a key the agent
    self-disables with a warning.
    """

    AGENT_FAMILY = "GeminiReasoning"

    def __init__(self, config: dict):
        super().__init__(config)
        self._model = config.get("model", DEFAULT_MODEL)
        self._reasoning_effort = config.get("reasoning_effort", "low")
        self._temperature = float(config.get("temperature", 1))
        self._top_p = float(config.get("top_p", 1))
        self._max_completion_tokens = int(config.get("max_completion_tokens", 2048))
        self._timeout_s = float(config.get("timeout_s", 8.0))
        self._invoke_every_n_rounds = int(config.get("invoke_every_n_rounds", 5))
        self._invoke_on_tiebreak = bool(config.get("invoke_on_tiebreak", True))
        self._api_key = resolve_agent_api_key(config)
        self._rounds_seen = 0
        self._agent = GeminiReasoningAgent(self)
        if not self._api_key and self._verbose:
            logging.warning("GeminiReasoning | no API key found, agent disabled (safe degradation)")

    @property
    def invoke_on_tiebreak(self) -> bool:
        return self._invoke_on_tiebreak and bool(self._api_key)

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    async def init(self):
        await self._agent.register_sa_agent()
        await super().init()

    async def _build_snapshot(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """Aggregate anonymized context from all sibling agents of the node."""
        agents_summary: dict[str, Any] = {}
        sar = self._sar
        if sar is not None:
            for name in ("sanetwork", "satraining", "stragglerpredictor", "aggregationselector", "resourcebroker"):
                comp = sar.get_component(name) if hasattr(sar, "get_component") else None
                if comp is None:
                    continue
                try:
                    if hasattr(comp, "snapshot"):
                        summary = await comp.snapshot()
                    else:
                        summary = {"agent": comp.__class__.__name__}
                    agents_summary[name] = self._anonymize(summary)
                except Exception as e:
                    logging.debug(f"GeminiReasoning | cannot snapshot {name}: {e}")
        snapshot = {
            "round": getattr(sar, "current_round", None),
            "node": anonymize_addr(self._addr),
            "agents": agents_summary,
        }
        if extra:
            snapshot.update(extra)
        return snapshot

    def _anonymize(self, obj: Any) -> Any:
        """Recursively hash strings that look like node addresses (ip:port)."""
        if isinstance(obj, dict):
            return {k: self._anonymize(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self._anonymize(v) for v in obj]
        if isinstance(obj, str) and ":" in obj:
            head, _, tail = obj.partition(":")
            if head and tail and all(c.isdigit() or c == "." for c in head):
                return anonymize_addr(obj)
        return obj

    async def _query_llm(self, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        """Query Gemini with structured outputs; None on any failure (degradation)."""
        from nebula.core.smac_gemini import gemini_json_completion

        decision = await gemini_json_completion(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(snapshot)},
            ],
            model=self._model,
            api_key=self._api_key,
            response_format=DECISION_SCHEMA,
            reasoning_effort=self._reasoning_effort,
            temperature=self._temperature,
            top_p=self._top_p,
            max_completion_tokens=self._max_completion_tokens,
            timeout_s=self._timeout_s,
        )
        return self._validate_decision(decision) if decision is not None else None

    def _validate_decision(self, decision: Any) -> dict[str, Any] | None:
        """Validate the structured decision; None when invalid."""
        if not isinstance(decision, dict):
            return None
        allowed = {
            "no_action",
            "adjust_training_policy",
            "request_topology_change",
            "select_aggregator",
            "flag_for_human_review",
        }
        if decision.get("decision") not in allowed:
            return None
        try:
            confidence = float(decision.get("confidence", -1))
        except (TypeError, ValueError):
            return None
        if not 0.0 <= confidence <= 1.0:
            return None
        if not isinstance(decision.get("explanation"), str):
            return None
        decision["confidence"] = confidence
        decision["explanation"] = decision["explanation"][:500]
        return decision

    async def sa_component_actions(self):
        """Periodic advisory invocation (every N rounds, never blocking)."""
        self._rounds_seen += 1
        if not self.enabled or (self._rounds_seen % self._invoke_every_n_rounds != 0):
            await self._agent.notify_all_suggestions_done(RoundEndEvent)
            return
        snapshot = await self._build_snapshot()
        decision = await self._query_llm(snapshot)
        if decision is None or decision.get("decision") == "no_action":
            await self._agent.notify_all_suggestions_done(RoundEndEvent)
            return
        try:
            cmd = SACommand.from_llm_decision(decision, self._agent)
        except ValueError as e:
            logging.warning(f"GeminiReasoning | invalid decision mapping: {e}")
            await self._agent.notify_all_suggestions_done(RoundEndEvent)
            return
        if cmd is not None:
            if self._verbose:
                logging.info(f"GeminiReasoning | suggesting {cmd.get_action().value}: {cmd.get_explanation()}")
            await self._agent.suggest_action(cmd)
        await self._agent.notify_all_suggestions_done(RoundEndEvent)

    async def resolve_tie(self, cmd1: SACommand, cmd2: SACommand) -> SACommand | None:
        """Resolve an ambiguous arbitration tie via the LLM (bounded time).

        Returns the winning command, or None to fall back to the static rule.
        """
        if not self.enabled:
            return None
        snapshot = await self._build_snapshot(
            extra={
                "tie_break": {
                    "candidates": [cmd1.to_dict(), cmd2.to_dict()],
                    "instruction": "Choose one candidate by setting parameters.preferred_action "
                    "to its action value, or abstain by omitting it.",
                }
            }
        )
        decision = await self._query_llm(snapshot)
        if not decision:
            return None
        preferred = (decision.get("parameters", {}) or {}).get("preferred_action")
        if preferred == cmd1.get_action().value:
            return cmd1
        if preferred == cmd2.get_action().value:
            return cmd2
        return None

    async def stop(self):
        logging.info("🛑  Stopping GeminiReasoning...")


class GeminiReasoningAgent(SAModuleAgent):
    def __init__(self, reasoning: GeminiReasoning):
        self._reasoning = reasoning

    async def get_agent(self) -> str:
        return "GeminiReasoning_MainAgent"

    async def register_sa_agent(self):
        await SuggestionBuffer.get_instance().register_event_agents(RoundEndEvent, self)

    async def suggest_action(self, sac):
        await SuggestionBuffer.get_instance().register_suggestion(RoundEndEvent, self, sac)

    async def notify_all_suggestions_done(self, event_type):
        await SuggestionBuffer.get_instance().notify_all_suggestions_done_for_agent(self, event_type)
