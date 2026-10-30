from __future__ import annotations

import logging
import statistics

from nebula.core.eventmanager import EventManager
from nebula.core.nebulaevents import AggregationEvent
from nebula.core.situationalawareness.awareness.cognitive.cognitiveagent import CognitiveAgent
from nebula.core.situationalawareness.awareness.sautils.sacommand import (
    SACommandAction,
    SACommandPRIO,
    factory_sa_command,
)
from nebula.core.situationalawareness.awareness.sautils.samoduleagent import SAModuleAgent
from nebula.core.situationalawareness.awareness.suggestionbuffer import SuggestionBuffer

SUPPORTED_ALGORITHMS = ("FedAvg", "Krum", "Median", "TrimmedMean")


def choose_aggregation(
    n_updates: int,
    n_missing: int,
    n_blacklisted: int,
    weight_cv: float,
    missing_ratio_threshold: float = 0.3,
    variance_threshold: float = 1.0,
) -> tuple[str, str]:
    """Choose the aggregation algorithm from the current round context.

    Deterministic rule set (safe default: ``FedAvg``):

    - Byzantine suspicion (blacklisted nodes) with enough updates -> ``Krum``,
      ``Median`` as fallback when there are too few updates for Krum.
    - Many missing updates or highly dispersed update weights -> ``TrimmedMean``.
    - Moderate weight dispersion -> ``Median``.
    - Otherwise -> ``FedAvg``.

    Args:
        n_updates (int): Number of received updates.
        n_missing (int): Number of missing updates.
        n_blacklisted (int): Number of currently blacklisted nodes.
        weight_cv (float): Coefficient of variation of the update weights.
        missing_ratio_threshold (float): Missing ratio triggering robustness.
        variance_threshold (float): Weight CV triggering robustness.

    Returns:
        tuple[str, str]: (algorithm name, human-readable reason).
    """
    total = n_updates + n_missing
    missing_ratio = (n_missing / total) if total > 0 else 0.0
    if n_blacklisted > 0:
        if n_updates >= 4:
            return "Krum", f"{n_blacklisted} blacklisted node(s), {n_updates} updates available"
        return "Median", f"{n_blacklisted} blacklisted node(s) but only {n_updates} updates (Krum needs >= 4)"
    if missing_ratio > missing_ratio_threshold or weight_cv > variance_threshold:
        return "TrimmedMean", f"missing_ratio={missing_ratio:.2f}, weight_cv={weight_cv:.2f}"
    if weight_cv > variance_threshold / 2:
        return "Median", f"moderate weight dispersion (cv={weight_cv:.2f})"
    return "FedAvg", "stable round context"


def _weights_cv(updates: dict) -> float:
    """Coefficient of variation of the update weights (best-effort)."""
    try:
        weights = [float(entry[1]) for entry in updates.values()]
        if len(weights) < 2:
            return 0.0
        mean = statistics.mean(weights)
        if mean == 0:
            return 0.0
        return statistics.stdev(weights) / abs(mean)
    except Exception:
        return 0.0


class AggregationSelector(CognitiveAgent):
    """Agent that selects the aggregation algorithm dynamically (SMAC, Fase 2).

    On every ``AggregationEvent`` it evaluates the round context (received vs
    missing updates, blacklisted nodes, weight dispersion) with a
    deterministic rule set and emits a ``SELECT_AGGREGATOR`` command. The
    command applies the selection by hot-swapping the engine aggregator
    (``create_aggregator``) and passes the updates through untouched, so it
    is safe regardless of the position it ends up in during arbitration.
    When no engine is reachable (e.g. unit tests) the choice is recorded as
    an advisory belief instead.
    """

    AGENT_FAMILY = "AggregationSelector"

    def __init__(self, config: dict):
        super().__init__(config)
        self._missing_ratio_threshold = float(config.get("missing_ratio_threshold", 0.3))
        self._variance_threshold = float(config.get("variance_threshold", 1.0))
        self._agent = AggregationSelectorAgent(self)

    async def init(self):
        await EventManager.get_instance().subscribe_node_event(AggregationEvent, self._process_aggregation_event)
        await self._agent.register_sa_agent()
        await super().init()

    def current_algorithm(self) -> str:
        """Currently active aggregation algorithm (belief, defaults to FedAvg)."""
        return self._beliefs.get("current_algorithm", "FedAvg")

    async def _engine(self):
        """Reach the node engine through the discovery component (best-effort)."""
        try:
            sad = self._sar.sad if self._sar is not None else None
            engine = getattr(sad, "engine", None)
        except Exception:
            return None
        else:
            return engine

    async def _process_aggregation_event(self, age: AggregationEvent):
        try:
            updates, _expected_nodes, missing_nodes = await age.get_event_data()
        except Exception as e:
            logging.warning(f"AggregationSelector | cannot read aggregation event: {e}")
            await self._agent.notify_all_suggestions_done(AggregationEvent)
            return

        n_updates = len(updates)
        n_missing = len(missing_nodes or set())
        n_blacklisted = await self._count_blacklisted()
        weight_cv = _weights_cv(updates)

        choice, reason = choose_aggregation(
            n_updates,
            n_missing,
            n_blacklisted,
            weight_cv,
            self._missing_ratio_threshold,
            self._variance_threshold,
        )
        await self.update_belief("last_choice", choice)
        await self.update_belief("last_reason", reason)
        await self.update_belief(
            "last_context",
            {
                "n_updates": n_updates,
                "n_missing": n_missing,
                "n_blacklisted": n_blacklisted,
                "weight_cv": weight_cv,
            },
        )

        if choice != self.current_algorithm():
            if self._verbose:
                logging.info(f"AggregationSelector | switching {self.current_algorithm()} -> {choice} ({reason})")
            # Pass updates through: safe even if this command is executed first.
            sac = factory_sa_command(
                "cognitive",
                SACommandAction.SELECT_AGGREGATOR,
                self._agent,
                {"algorithm": choice, "reason": reason},
                SACommandPRIO.MEDIUM,
                False,
                self._apply_selection,
                0.8,
                f"Dynamic aggregation selection: {choice} ({reason})",
                1,
                choice,
                updates,
            )
            await self._agent.suggest_action(sac)
        elif self._verbose:
            logging.info(f"AggregationSelector | keeping {choice} ({reason})")
        await self._agent.notify_all_suggestions_done(AggregationEvent)

    async def _count_blacklisted(self) -> int:
        """Number of currently blacklisted nodes (best-effort, 0 when unknown)."""
        try:
            engine = await self._engine()
            if engine is None:
                return 0
            cm = getattr(engine, "cm", None)
            blacklist = getattr(cm, "blacklist", None)
            if blacklist is None:
                return 0
            nodes = getattr(blacklist, "blacklisted_nodes", None) or getattr(blacklist, "_blacklist", None) or []
            return len(nodes)
        except Exception:
            return 0

    async def _apply_selection(self, choice: str, updates: dict) -> dict:
        """Hot-swap the engine aggregator and pass the updates through."""
        await self.update_belief("current_algorithm", choice)
        engine = await self._engine()
        if engine is None:
            logging.info(f"AggregationSelector | advisory choice {choice} (no engine reachable)")
            return updates
        try:
            previous = engine.config.participant["aggregator_args"]["algorithm"]
            if previous == choice:
                return updates
            engine.config.participant["aggregator_args"]["algorithm"] = choice
            from nebula.core.aggregation.aggregator import create_aggregator

            new_aggregator = create_aggregator(config=engine.config, engine=engine)
            await new_aggregator.init()
            engine._aggregator = new_aggregator
            logging.info(f"AggregationSelector | aggregator hot-swapped {previous} -> {choice}")
        except Exception as e:
            logging.warning(f"AggregationSelector | hot-swap to {choice} failed, keeping current: {e}")
        return updates

    async def sa_component_actions(self):
        """No periodic action: this agent reacts to AggregationEvent only."""
        return

    async def stop(self):
        logging.info("🛑  Stopping AggregationSelector...")


class AggregationSelectorAgent(SAModuleAgent):
    def __init__(self, selector: AggregationSelector):
        self._selector = selector

    async def get_agent(self) -> str:
        return "AggregationSelector_MainAgent"

    async def register_sa_agent(self):
        await SuggestionBuffer.get_instance().register_event_agents(AggregationEvent, self)

    async def suggest_action(self, sac):
        await SuggestionBuffer.get_instance().register_suggestion(AggregationEvent, self, sac)

    async def notify_all_suggestions_done(self, event_type):
        await SuggestionBuffer.get_instance().notify_all_suggestions_done_for_agent(self, event_type)
