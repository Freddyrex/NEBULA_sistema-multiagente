"""Cognitive layer for the Situational Awareness multi-agent system (SMAC).

This package contains the formalization of NEBULA's situational awareness
components as a true multi-agent system:

- :mod:`cognitiveagent`: base class for cognitive agents with persistent
  belief state (perception -> belief -> decision cycle).
- :mod:`decisionlog`: auditable ring-buffer + JSONL persistence of the
  commands selected by arbitration.
- :mod:`gemini_reasoning_agent`: LLM-backed advisory agent served through the
  Gemini API (optional, safe degradation when unavailable).
- :mod:`crew_reasoning_agent`: optional CrewAI advisory agent for the
  aggregator node (off by default, proposals only).
"""

from nebula.core.situationalawareness.awareness.cognitive.cognitiveagent import CognitiveAgent
from nebula.core.situationalawareness.awareness.cognitive.decisionlog import CognitiveDecisionLog

__all__ = ["CognitiveAgent", "CognitiveDecisionLog"]
