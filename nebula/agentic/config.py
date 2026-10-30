"""Configuration for the NEBULA agentic decision layer.

Priority: ``scenario.json :: adaptive_agent`` > environment > safe defaults.
The feature is **off by default**: a scenario without ``multi_agent=true`` never
invokes an LLM crew (roadmap rule: no auto-execution, no surprise cost).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from typing import Any

from nebula.config import defaults as _defaults

STRATEGIES = ("auto", "fast_path", "voting", "role", "debate", "carry_over")


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class CrewConfig:
    enabled: bool = False
    strategy: str = "auto"
    specialist_model: str = "gemini-3.5-flash-lite"
    coordinator_model: str = "gemini-3.5-flash"
    agent_timeout_s: float = 6.0
    total_timeout_s: float = 8.0
    cache_ttl_s: float = 45.0
    max_agents: int = 4
    debate_max_turns: int = 2
    debate_enabled: bool = False
    max_rpm: int = 30
    min_clients: int = 2
    max_exclusion_ratio: float = 0.4
    use_crewai: bool = False
    trigger: str = "manual"
    temperature: float = 0.2
    max_tokens: int = 1200
    require_human_review: bool = True

    def with_strategy(self, strategy: str | None) -> CrewConfig:
        if not strategy:
            return self
        normalized = str(strategy).strip().lower()
        if normalized not in STRATEGIES:
            return self
        return replace(self, strategy=normalized)


def _scenario_payload(config_dir: str | None, scenario: str) -> dict[str, Any]:
    if (
        not config_dir
        or not scenario
        or scenario in (".", "..")
        or os.path.basename(scenario) != scenario
        or "/" in scenario
        or "\\" in scenario
    ):
        return {}
    try:
        path = os.path.join(config_dir, scenario, "scenario.json")
        with open(path) as f:
            data = json.load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_crew_config(config_dir: str | None, scenario: str) -> CrewConfig:
    """Build the effective config for one scenario. Never raises."""
    raw = (_scenario_payload(config_dir, scenario).get("adaptive_agent") or {})
    if not isinstance(raw, dict):
        raw = {}

    env_enabled = _bool(os.environ.get("SMAC_MULTI_AGENT"), _defaults.SMAC_MULTI_AGENT)
    enabled = _bool(raw.get("multi_agent"), env_enabled)
    strategy = str(raw.get("crew_strategy") or os.environ.get("SMAC_CREW_STRATEGY") or _defaults.SMAC_CREW_STRATEGY).lower()
    if strategy not in STRATEGIES:
        strategy = "auto"

    def _pick(name: str, env: str, default: Any) -> Any:
        if name in raw and raw.get(name) is not None:
            return raw.get(name)
        return os.environ.get(env, default)

    return CrewConfig(
        enabled=enabled,
        strategy=strategy,
        specialist_model=str(_pick("crew_specialist_model", "SMAC_CREW_SPECIALIST_MODEL", _defaults.SMAC_CREW_SPECIALIST_MODEL)),
        coordinator_model=str(_pick("crew_coordinator_model", "SMAC_CREW_COORDINATOR_MODEL", _defaults.SMAC_CREW_COORDINATOR_MODEL)),
        agent_timeout_s=max(1.0, _float(_pick("crew_agent_timeout_s", "SMAC_CREW_AGENT_TIMEOUT_S", _defaults.SMAC_CREW_AGENT_TIMEOUT_S), _defaults.SMAC_CREW_AGENT_TIMEOUT_S)),
        total_timeout_s=max(1.0, _float(_pick("crew_timeout_s", "SMAC_CREW_TOTAL_TIMEOUT_S", _defaults.SMAC_CREW_TOTAL_TIMEOUT_S), _defaults.SMAC_CREW_TOTAL_TIMEOUT_S)),
        cache_ttl_s=max(0.0, _float(_pick("crew_cache_ttl_s", "SMAC_CREW_CACHE_TTL_S", _defaults.SMAC_CREW_CACHE_TTL_S), _defaults.SMAC_CREW_CACHE_TTL_S)),
        max_agents=max(1, min(5, _int(_pick("crew_max_agents", "SMAC_CREW_MAX_AGENTS", _defaults.SMAC_CREW_MAX_AGENTS), _defaults.SMAC_CREW_MAX_AGENTS))),
        debate_max_turns=max(1, min(3, _int(_pick("crew_debate_max_turns", "SMAC_CREW_DEBATE_MAX_TURNS", _defaults.SMAC_CREW_DEBATE_MAX_TURNS), _defaults.SMAC_CREW_DEBATE_MAX_TURNS))),
        debate_enabled=_bool(_pick("crew_debate_enabled", "SMAC_CREW_DEBATE_ENABLED", _defaults.SMAC_CREW_DEBATE_ENABLED), _defaults.SMAC_CREW_DEBATE_ENABLED),
        max_rpm=max(1, _int(_pick("crew_max_rpm", "SMAC_CREW_MAX_RPM", _defaults.SMAC_CREW_MAX_RPM), _defaults.SMAC_CREW_MAX_RPM)),
        min_clients=max(1, _int(_pick("crew_min_clients", "SMAC_CREW_MIN_CLIENTS", _defaults.SMAC_CREW_MIN_CLIENTS), _defaults.SMAC_CREW_MIN_CLIENTS)),
        max_exclusion_ratio=max(0.0, min(1.0, _float(
            _pick("crew_max_exclusion_ratio", "SMAC_CREW_MAX_EXCLUSION_RATIO", _defaults.SMAC_CREW_MAX_EXCLUSION_RATIO), _defaults.SMAC_CREW_MAX_EXCLUSION_RATIO))),
        use_crewai=_bool(_pick("crew_use_crewai", "SMAC_CREW_USE_CREWAI", _defaults.SMAC_CREW_USE_CREWAI), _defaults.SMAC_CREW_USE_CREWAI),
        trigger=str(_pick("crew_trigger", "SMAC_CREW_TRIGGER", "manual")),
        temperature=max(0.0, min(1.0, _float(os.environ.get("SMAC_TEMPERATURE"), 0.2))),
        max_tokens=max(256, _int(os.environ.get("SMAC_CHAT_MAX_TOKENS"), 1200)),
        require_human_review=_bool(raw.get("crew_require_human_review"), True),
    )


def crew_enabled(config_dir: str | None, scenario: str) -> bool:
    return load_crew_config(config_dir, scenario).enabled


__all__ = ["STRATEGIES", "CrewConfig", "crew_enabled", "load_crew_config"]
