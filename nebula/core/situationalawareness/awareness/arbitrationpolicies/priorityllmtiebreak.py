import logging

from nebula.core.situationalawareness.awareness.arbitrationpolicies.staticarbitrationpolicy import SAP
from nebula.core.situationalawareness.awareness.sautils.sacommand import SACommand


class PriorityLLMTiebreakPolicy(SAP):
    """Priority arbitration with cognitive (LLM) tie-breaking (SMAC).

    Behaves exactly like the static arbitration policy (:class:`SAP`) except
    when a conflict is *ambiguous*: same priority **and** same static agent
    weight. In that case the plain static rule would pick a winner
    arbitrarily, so the policy reports the tie as ambiguous through
    :meth:`is_ambiguous_tie` and the ``SAReasoner`` may ask the
    ``GroqReasoningAgent`` for a second opinion (bounded by a short timeout).
    When the cognitive layer is disabled, unavailable or times out, the
    static rule applies as a safe fallback.
    """

    async def is_ambiguous_tie(self, sac1: SACommand, sac2: SACommand) -> bool:
        """Check whether a conflict cannot be resolved by static weights.

        Args:
            sac1 (SACommand): First command in conflict.
            sac2 (SACommand): Second command in conflict.

        Returns:
            bool: True when both commands share priority and static weight.
        """
        if sac1.get_prio() != sac2.get_prio():
            return False

        async def get_weight(cmd):
            category = await self._get_agent_category(cmd)
            return self.agent_weights.get(category, 0)

        return await get_weight(sac1) == await get_weight(sac2)

    async def tie_break(self, sac1: SACommand, sac2: SACommand) -> bool:
        """Static fallback tie-break (same rule as :class:`SAP`)."""
        if self._verbose:
            logging.info(
                f"LLM tie-break fallback between ({await sac1.get_owner()}, {sac1.get_action().value}) & "
                f"({await sac2.get_owner()}, {sac2.get_action().value})"
            )
        return await super().tie_break(sac1, sac2)
