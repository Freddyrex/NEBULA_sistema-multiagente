"""Loader shim for the SMAC cognitive agent.

The implementation lives in the ``cognitive`` subpackage; this module only
re-exports it so the ``SAReasoner`` dynamic loader (which resolves
``groq_reasoning_agent`` to ``groqreasoningagent/groqreasoningagent.py``)
can find the component class.
"""

from nebula.core.situationalawareness.awareness.cognitive.groqreasoningagent import (
    GroqReasoning,
    GroqReasoningAgent,
)

__all__ = ["GroqReasoning", "GroqReasoningAgent"]
