"""Loader shim for the node-side CrewAI advisory agent.

The implementation lives in the ``cognitive`` subpackage; this module only
re-exports it so ``SAReasoner``'s dynamic loader can resolve
``crew_reasoning_agent`` to ``crewreasoningagent/crewreasoningagent.py``.
"""

from nebula.core.situationalawareness.awareness.cognitive.crew_reasoning_agent import (
    CrewReasoning,
    CrewReasoningAgent,
)

__all__ = ["CrewReasoning", "CrewReasoningAgent"]
