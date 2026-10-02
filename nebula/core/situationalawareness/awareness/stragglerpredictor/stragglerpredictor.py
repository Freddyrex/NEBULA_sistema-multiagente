from __future__ import annotations

import logging
import statistics
import time
from collections import defaultdict, deque
from typing import Any

from nebula.core.eventmanager import EventManager
from nebula.core.nebulaevents import RoundEndEvent, RoundStartEvent, UpdateReceivedEvent
from nebula.core.situationalawareness.awareness.cognitive.cognitiveagent import CognitiveAgent
from nebula.core.situationalawareness.awareness.sautils.sacommand import (
    SACommandAction,
    SACommandPRIO,
    SACommandState,
    factory_sa_command,
)
from nebula.core.situationalawareness.awareness.sautils.samoduleagent import SAModuleAgent
from nebula.core.situationalawareness.awareness.suggestionbuffer import SuggestionBuffer


def predict_stragglers(
    latency_history: dict[str, list[float]],
    round_deadline: float | None,
    min_samples: int = 3,
    std_factor: float = 2.0,
) -> dict[str, float]:
    """Predict straggler neighbors from per-neighbor update-latency history.

    A neighbor is flagged when its predicted latency
    (``mean + std_factor * stdev``) exceeds the estimated round deadline.

    Args:
        latency_history (dict): Neighbor -> list of past update latencies (s).
        round_deadline (float | None): Estimated round deadline in seconds.
            None disables prediction (no baseline yet).
        min_samples (int): Minimum samples per neighbor to predict.
        std_factor (float): Strictness of the prediction band.

    Returns:
        dict: Neighbor -> confidence in [0.5, 0.95] for predicted stragglers.
    """
    predicted: dict[str, float] = {}
    if round_deadline is None or round_deadline <= 0:
        return predicted
    for neighbor, samples in latency_history.items():
        if len(samples) < min_samples:
            continue
        mean = statistics.mean(samples)
        stdev = statistics.stdev(samples) if len(samples) >= 2 else 0.0
        forecast = mean + std_factor * stdev
        if forecast > round_deadline:
            confidence = min(0.95, 0.5 + (forecast - round_deadline) / round_deadline)
            predicted[neighbor] = confidence
    return predicted


class StragglerPredictor(CognitiveAgent):
    """Predictive agent that anticipates straggler neighbors (SMAC, Fase 2).

    Unlike the reactive policies, this agent keeps a persistent belief state
    with per-neighbor update-latency history and round-duration statistics,
    and flags neighbors *before* they miss the round deadline.

    Perception sources: ``RoundStartEvent`` (round start time),
    ``UpdateReceivedEvent`` (per-neighbor arrival latency) and
    ``RoundEndEvent`` (round duration baseline). Decisions are emitted as
    ``EXCLUDE_NODE`` training commands on ``RoundEndEvent`` arbitration.
    """

    AGENT_FAMILY = "StragglerPredictor"

    def __init__(self, config: dict):
        super().__init__(config)
        self._history_window = int(config.get("history_window", 20))
        self._min_samples = int(config.get("min_samples", 3))
        self._std_factor = float(config.get("std_factor", 2.0))
        self._latency_history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=self._history_window))
        self._round_durations: deque[float] = deque(maxlen=self._history_window)
        self._round_start_time: float | None = None
        self._current_round: int | None = None
        self._agent = StragglerPredictorAgent(self)

    async def init(self):
        await EventManager.get_instance().subscribe_node_event(RoundStartEvent, self._observe_round_start)
        await EventManager.get_instance().subscribe_node_event(UpdateReceivedEvent, self._observe_update)
        await self._agent.register_sa_agent()
        await super().init()

    async def _observe_round_start(self, rse: RoundStartEvent):
        try:
            round_number, start_time, _ = await rse.get_event_data()
            self._current_round = round_number
            self._round_start_time = start_time or time.time()
        except Exception as e:
            logging.warning(f"StragglerPredictor | cannot observe round start: {e}")

    async def _observe_update(self, ure: UpdateReceivedEvent):
        try:
            _, _, source, _, _ = await ure.get_event_data()
            if self._round_start_time is None or source == self._addr:
                return
            latency = time.time() - self._round_start_time
            if latency >= 0:
                self._latency_history[source].append(latency)
        except Exception as e:
            logging.warning(f"StragglerPredictor | cannot observe update: {e}")

    def estimate_round_deadline(self) -> float | None:
        """Estimate the round deadline from recent round durations."""
        if len(self._round_durations) < 2:
            return None
        baseline = statistics.median(self._round_durations)
        return max(baseline * 2.0, 10.0)

    async def perceive(self) -> dict[str, Any]:
        return {}

    async def sa_component_actions(self):
        """Close the current round observations and suggest exclusions."""
        if self._round_start_time is not None:
            duration = time.time() - self._round_start_time
            if duration > 0:
                self._round_durations.append(duration)
            self._round_start_time = None

        history = {node: list(samples) for node, samples in self._latency_history.items()}
        deadline = self.estimate_round_deadline()
        predicted = predict_stragglers(history, deadline, self._min_samples, self._std_factor)

        await self.update_belief("round_deadline", deadline)
        await self.update_belief("predicted_stragglers", sorted(predicted.keys()))
        await self.update_belief("last_round", self._current_round)

        if self._verbose:
            logging.info(f"StragglerPredictor | deadline={deadline}, predicted={sorted(predicted.keys())}")

        for neighbor, confidence in predicted.items():
            await self._agent.create_and_suggest_exclusion(
                neighbor,
                confidence,
                f"Predicted straggler: forecast latency exceeds round deadline ({deadline:.1f}s)",
            )
        await self._agent.notify_all_suggestions_done(RoundEndEvent)

    async def stop(self):
        logging.info("🛑  Stopping StragglerPredictor...")


class StragglerPredictorAgent(SAModuleAgent):
    def __init__(self, predictor: StragglerPredictor):
        self._predictor = predictor

    async def get_agent(self) -> str:
        return "StragglerPredictor_MainAgent"

    async def register_sa_agent(self):
        await SuggestionBuffer.get_instance().register_event_agents(RoundEndEvent, self)

    async def suggest_action(self, sac):
        await SuggestionBuffer.get_instance().register_suggestion(RoundEndEvent, self, sac)

    async def notify_all_suggestions_done(self, event_type):
        await SuggestionBuffer.get_instance().notify_all_suggestions_done_for_agent(self, event_type)

    async def create_and_suggest_exclusion(self, neighbor: str, confidence: float, explanation: str):
        sac = factory_sa_command(
            "training",
            SACommandAction.EXCLUDE_NODE,
            self,
            {"node": neighbor},
            SACommandPRIO.HIGH,
            True,
            None,
            confidence,
            explanation,
            2,
        )
        await self.suggest_action(sac)
        sa_command_state = await sac.get_state_future()
        if sa_command_state == SACommandState.EXECUTED and self._predictor._verbose:
            logging.info(f"StragglerPredictor | exclusion suggested for {neighbor}")
