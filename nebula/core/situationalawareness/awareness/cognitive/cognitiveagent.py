from __future__ import annotations

import asyncio
import logging
from typing import Any

from nebula.core.situationalawareness.awareness.sareasoner import SAMComponent


class CognitiveAgent(SAMComponent):
    """Base class for cognitive Situational Awareness agents (SMAC).

    Formalizes the already existing ``SAMComponent`` contract as a true agent
    with an explicit perception -> belief -> decision cycle:

    - *perception*: :meth:`perceive` gathers local observations (telemetry,
      events, neighbor state) without side effects.
    - *belief*: observations are merged into a persistent belief state
      (:meth:`update_belief` / :meth:`get_beliefs`) so the agent can learn
      across rounds instead of reacting to a single snapshot.
    - *decision*: :meth:`sa_component_actions` turns beliefs into
      :class:`SACommand` suggestions registered in the ``SuggestionBuffer``
      through the agent's ``SAModuleAgent`` wrapper.

    Subclasses must implement :meth:`sa_component_actions` and should
    override :meth:`perceive`. The class never performs network I/O by
    itself; coordination between nodes happens through the commands it
    emits (same contract as the reactive agents).
    """

    #: Human readable agent family name, used by arbitration policies and logs.
    AGENT_FAMILY = "Cognitive"

    def __init__(self, config: dict):
        self._config = config
        self._sar = config.get("sar")
        self._addr = config.get("addr", "unknown")
        self._verbose = bool(config.get("verbose", False))
        self._beliefs: dict[str, Any] = {}
        self._beliefs_lock = asyncio.Lock()

    @property
    def sar(self):
        """SA Reasoner this agent is attached to (may be None in unit tests)."""
        return self._sar

    async def perceive(self) -> dict[str, Any]:
        """Gather local observations without side effects.

        Returns:
            dict: Observation snapshot. The default implementation returns
            an empty dict; subclasses override it.
        """
        return {}

    async def update_belief(self, key: str, value: Any):
        """Persistently store (or overwrite) a single belief.

        Args:
            key (str): Belief name.
            value (Any): Belief value (must be JSON-serializable when the
                belief is exposed to the cognitive layer).
        """
        async with self._beliefs_lock:
            self._beliefs[key] = value

    async def get_belief(self, key: str, default: Any = None) -> Any:
        """Retrieve a single belief.

        Args:
            key (str): Belief name.
            default (Any): Value returned when the belief is not set.
        """
        async with self._beliefs_lock:
            return self._beliefs.get(key, default)

    async def get_beliefs(self) -> dict[str, Any]:
        """Return a shallow copy of the current belief state."""
        async with self._beliefs_lock:
            return dict(self._beliefs)

    async def merge_observations(self, observations: dict[str, Any]):
        """Merge a perception snapshot into the belief state.

        The default strategy overwrites keys; agents with temporal state
        (e.g. sliding windows) override this method.
        """
        async with self._beliefs_lock:
            self._beliefs.update(observations)

    async def snapshot(self) -> dict[str, Any]:
        """Build an anonymized summary of this agent for the cognitive layer.

        Only aggregated metadata is exposed, never training data, gradients
        or model weights.
        """
        beliefs = await self.get_beliefs()
        return {
            "agent": self.agent_name(),
            "beliefs": beliefs,
        }

    def agent_name(self) -> str:
        """Unique agent name used in buffers, arbitration and logs."""
        return f"{self.AGENT_FAMILY}_{self.__class__.__name__}"

    async def init(self):
        """Initialize the agent (default: perceive once to seed beliefs)."""
        try:
            observations = await self.perceive()
            if observations:
                await self.merge_observations(observations)
        except Exception as e:
            logging.warning(f"{self.agent_name()} | initial perception failed: {e}")

    async def sa_component_actions(self):
        """Perception -> belief -> decision cycle entry point."""
        raise NotImplementedError
