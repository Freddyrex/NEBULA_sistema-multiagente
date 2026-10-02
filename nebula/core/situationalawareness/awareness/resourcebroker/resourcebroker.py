from __future__ import annotations

import logging
from typing import Any

from nebula.core.nebulaevents import RoundEndEvent
from nebula.core.situationalawareness.awareness.cognitive.cognitiveagent import CognitiveAgent
from nebula.core.situationalawareness.awareness.sautils.sacommand import (
    SACommandAction,
    SACommandPRIO,
    factory_sa_command,
)
from nebula.core.situationalawareness.awareness.sautils.samoduleagent import SAModuleAgent
from nebula.core.situationalawareness.awareness.sautils.sasystemmonitor import SystemMonitor
from nebula.core.situationalawareness.awareness.suggestionbuffer import SuggestionBuffer


def decide_resource_action(
    cpu_usage: float | None,
    memory_usage: float | None,
    gpu_mem_usage: float | None,
    cpu_high: float = 85.0,
    mem_high: float = 90.0,
    gpu_mem_high: float = 90.0,
) -> tuple[SACommandAction | None, SACommandPRIO, str]:
    """Decide the resource-broker action from a telemetry snapshot.

    Args:
        cpu_usage (float | None): CPU usage percent.
        memory_usage (float | None): RAM usage percent.
        gpu_mem_usage (float | None): GPU memory usage percent (None if no GPU).
        cpu_high (float): CPU threshold for load shedding.
        mem_high (float): RAM threshold for load shedding.
        gpu_mem_high (float): GPU memory threshold for load shedding.

    Returns:
        tuple: (action or None, priority, explanation). Critical pressure on
        any resource suggests pausing local training; sustained high (but not
        critical) CPU suggests halving the local batch size.
    """
    critical_cpu = cpu_usage is not None and cpu_usage >= 95.0
    critical_mem = memory_usage is not None and memory_usage >= 95.0
    critical_gpu = gpu_mem_usage is not None and gpu_mem_usage >= 95.0
    if critical_cpu or critical_mem or critical_gpu:
        return (
            SACommandAction.PAUSE_LOCAL_TRAINING,
            SACommandPRIO.HIGH,
            f"Critical resource pressure (cpu={cpu_usage}%, mem={memory_usage}%, gpu_mem={gpu_mem_usage}%)",
        )
    high_cpu = cpu_usage is not None and cpu_usage >= cpu_high
    high_mem = memory_usage is not None and memory_usage >= mem_high
    high_gpu = gpu_mem_usage is not None and gpu_mem_usage >= gpu_mem_high
    if high_cpu or high_mem or high_gpu:
        return (
            SACommandAction.REDUCE_BATCH_SIZE,
            SACommandPRIO.MEDIUM,
            f"High resource usage (cpu={cpu_usage}%, mem={memory_usage}%, gpu_mem={gpu_mem_usage}%)",
        )
    return None, SACommandPRIO.LOW, ""


class ResourceBroker(CognitiveAgent):
    """Reactive agent that sheds local training load under pressure (SMAC, Fase 2).

    Polls the :class:`SystemMonitor` on every round end (fast probes only —
    the multi-second network usage probe is deliberately skipped so the
    agent never delays arbitration) and suggests ``PAUSE_LOCAL_TRAINING``
    or ``REDUCE_BATCH_SIZE`` advisory commands. A cooldown in rounds avoids
    flapping between contradictory suggestions.
    """

    AGENT_FAMILY = "ResourceBroker"

    def __init__(self, config: dict):
        super().__init__(config)
        self._cpu_high = float(config.get("cpu_high", 85.0))
        self._mem_high = float(config.get("mem_high", 90.0))
        self._gpu_mem_high = float(config.get("gpu_mem_high", 90.0))
        self._cooldown_rounds = int(config.get("cooldown_rounds", 3))
        self._monitor = SystemMonitor()
        self._agent = ResourceBrokerAgent(self)

    async def init(self):
        await self._agent.register_sa_agent()
        await super().init()

    async def perceive(self) -> dict[str, Any]:
        """Fast telemetry snapshot (no network-usage probe: too slow)."""
        snapshot: dict[str, Any] = {}
        try:
            import psutil

            snapshot["cpu_usage"] = psutil.cpu_percent(interval=None)
            snapshot["memory_usage"] = psutil.virtual_memory().percent
        except Exception as e:
            logging.warning(f"ResourceBroker | cannot read host telemetry: {e}")
        try:
            gpu_stats = await self._monitor.get_gpu_usage()
            if gpu_stats:
                used = sum(g.get("memory_used", 0) for g in gpu_stats)
                total = sum(g.get("memory_total", 0) for g in gpu_stats) or 1
                snapshot["gpu_mem_usage"] = used / total * 100.0
            else:
                snapshot["gpu_mem_usage"] = None
        except Exception:
            snapshot["gpu_mem_usage"] = None
        return snapshot

    async def sa_component_actions(self):
        snapshot = await self.perceive()
        if snapshot:
            await self.merge_observations(snapshot)

        action, priority, explanation = decide_resource_action(
            snapshot.get("cpu_usage"),
            snapshot.get("memory_usage"),
            snapshot.get("gpu_mem_usage"),
            self._cpu_high,
            self._mem_high,
            self._gpu_mem_high,
        )
        beliefs = await self.get_beliefs()
        current_round = beliefs.get("round", 0) + 1
        await self.update_belief("round", current_round)

        if action is None:
            if self._verbose:
                logging.info(f"ResourceBroker | resources nominal {snapshot}")
            await self._agent.notify_all_suggestions_done(RoundEndEvent)
            return

        last_action_round = beliefs.get("last_action_round", -(10**9))
        if current_round - last_action_round < self._cooldown_rounds:
            if self._verbose:
                logging.info("ResourceBroker | cooldown active, skipping suggestion")
            await self._agent.notify_all_suggestions_done(RoundEndEvent)
            return

        await self.update_belief("last_action_round", current_round)
        await self.update_belief("last_action", action.value)
        target: dict[str, Any] = {"readings": snapshot}
        if action == SACommandAction.REDUCE_BATCH_SIZE:
            target["batch_factor"] = 0.5
        sac = factory_sa_command(
            "cognitive",
            action,
            self._agent,
            target,
            priority,
            True,
            None,
            0.7,
            explanation,
            self._cooldown_rounds,
        )
        await self._agent.suggest_action(sac)
        await self._agent.notify_all_suggestions_done(RoundEndEvent)

    async def stop(self):
        logging.info("🛑  Stopping ResourceBroker...")


class ResourceBrokerAgent(SAModuleAgent):
    def __init__(self, broker: ResourceBroker):
        self._broker = broker

    async def get_agent(self) -> str:
        return "ResourceBroker_MainAgent"

    async def register_sa_agent(self):
        await SuggestionBuffer.get_instance().register_event_agents(RoundEndEvent, self)

    async def suggest_action(self, sac):
        await SuggestionBuffer.get_instance().register_suggestion(RoundEndEvent, self, sac)

    async def notify_all_suggestions_done(self, event_type):
        await SuggestionBuffer.get_instance().notify_all_suggestions_done_for_agent(self, event_type)
