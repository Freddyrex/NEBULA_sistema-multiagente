"""Loader shim for the SMAC cognitive agent.

The implementation lives in the ``cognitive`` subpackage; this module only
re-exports it so the ``SAReasoner`` dynamic loader (which resolves
``gemini_reasoning_agent`` to ``geminireasoningagent/geminireasoningagent.py``)
can find the component class.

The ``GroqReasoning`` aliases keep scenarios generated before the Gemini
migration loadable (their stored ``class_name`` was ``GroqReasoning``).
"""

from nebula.core.situationalawareness.awareness.cognitive.gemini_reasoning_agent import (
    GeminiReasoning,
    GeminiReasoningAgent,
)

#: Backward-compatible aliases (pre-Gemini scenario JSONs).
GroqReasoning = GeminiReasoning
GroqReasoningAgent = GeminiReasoningAgent

__all__ = [
    "GeminiReasoning",
    "GeminiReasoningAgent",
    "GroqReasoning",
    "GroqReasoningAgent",
]
