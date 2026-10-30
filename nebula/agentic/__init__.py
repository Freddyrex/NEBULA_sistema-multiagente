"""NEBULA agentic decision layer (SMAC multi-agent / optional CrewAI).

Public surface:

- :mod:`schemas`: typed contracts and Gemini JSON schemas.
- :mod:`config`: feature flags and budget.
- :mod:`context`: deterministic ``ContextPack`` builders.
- :mod:`patterns`: available architectural patterns and levers.
- :mod:`guardrails`: deterministic safety checks.
- :mod:`strategies`: fast-path, voting, role and debate.
- :mod:`orchestrator`: cache/timeout/fallback pipeline.
- :mod:`agents`: optional CrewAI adapter.
"""

from nebula.agentic.config import CrewConfig, crew_enabled, load_crew_config
from nebula.agentic.context import build_context_pack, build_node_context_pack
from nebula.agentic.guardrails import GuardrailEngine
from nebula.agentic.orchestrator import CrewOrchestrator, CrewResult, build_orchestrator
from nebula.agentic.patterns import PATTERNS, PatternSpec, available_patterns, get_pattern
from nebula.agentic.schemas import (
    AgentVerdict,
    ContextPack,
    CrewDecision,
    CrewTrace,
    Evidence,
    GuardrailReport,
    SafetyVerdict,
)

__all__ = [
    "PATTERNS",
    "AgentVerdict",
    "ContextPack",
    "CrewConfig",
    "CrewDecision",
    "CrewOrchestrator",
    "CrewResult",
    "CrewTrace",
    "Evidence",
    "GuardrailEngine",
    "GuardrailReport",
    "PatternSpec",
    "SafetyVerdict",
    "available_patterns",
    "build_context_pack",
    "build_node_context_pack",
    "build_orchestrator",
    "crew_enabled",
    "get_pattern",
    "load_crew_config",
]
