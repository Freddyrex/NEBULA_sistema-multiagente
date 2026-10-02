"""Cognitive layer for the Situational Awareness multi-agent system (SMAC).

This package contains the formalization of NEBULA's situational awareness
components as a true multi-agent system:

- :mod:`cognitiveagent`: base class for cognitive agents with persistent
  belief state (perception -> belief -> decision cycle).
- :mod:`decisionlog`: auditable ring-buffer + JSONL persistence of the
  commands selected by arbitration.
- :mod:`groqreasoningagent`: LLM-backed advisory agent served through Groq
  (optional, safe degradation when unavailable).
"""

from nebula.core.situationalawareness.awareness.cognitive.cognitiveagent import CognitiveAgent
from nebula.core.situationalawareness.awareness.cognitive.decisionlog import CognitiveDecisionLog

__all__ = ["CognitiveAgent", "CognitiveDecisionLog"]
