"""CrewAI adapter for the SMAC decision layer.

CrewAI is an **optional** dependency (``uv sync --group crewai``). Importing this
module never requires it: the adapter is selected at runtime and degrades to the
direct Gemini backend when CrewAI is not installed.

The orchestrator talks to :class:`~nebula.agentic.llm.LLMBackend`, so CrewAI is
used for agent/task execution while the deterministic strategies, guardrails and
schemas stay untouched and testable without the dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from nebula.agentic.llm import LLMReply
from nebula.agentic.prompts import SPECIALISTS
from nebula.core.smac_gemini import extract_json_object

_IMPORT_ERROR: str | None = None


def crewai_available() -> bool:
    try:
        import crewai  # noqa: F401
    except Exception:
        return False
    else:
        return True


def _crewai():
    global _IMPORT_ERROR
    try:
        from crewai import LLM, Agent, Crew, Process, Task
    except Exception as e:  # pragma: no cover - depends on optional install
        _IMPORT_ERROR = str(e)
        return None
    else:
        return Agent, Crew, LLM, Process, Task


def build_llm(model: str, api_key: str, *, temperature: float = 0.2, max_tokens: int = 1200):
    """Build a CrewAI LLM for Gemini (``None`` when CrewAI is unavailable)."""
    parts = _crewai()
    if parts is None:
        return None
    _Agent, _Crew, LLM, _Process, _Task = parts
    kwargs: dict[str, Any] = {
        "model": f"gemini/{model}",
        "api_key": api_key,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        return LLM(**kwargs)
    except Exception:
        try:
            kwargs.pop("api_key")
            return LLM(**kwargs)
        except Exception as e:  # pragma: no cover
            logging.warning(f"SMAC crew | cannot build CrewAI LLM: {e}")
            return None


def build_specialist_agents(cfg, llm) -> dict[str, Any]:
    """Five specialist agents keyed by lens (4 analysts + safety)."""
    parts = _crewai()
    if parts is None or llm is None:
        return {}
    Agent, _Crew, _LLM, _Process, _Task = parts
    agents: dict[str, Any] = {}
    for key, spec in SPECIALISTS.items():
        agents[key] = Agent(
            role=spec["role"],
            goal=spec["focus"],
            backstory=(
                "Eres un especialista conservador de NEBULA. Solo propones cambios "
                "respaldados por evidencia del contexto; si falta evidencia, haces hold."
            ),
            llm=llm,
            verbose=False,
            allow_delegation=False,
            max_iter=1,
        )
    agents["safety"] = Agent(
        role="SafetyAuditor",
        goal="Vetar propuestas que violen guardrails, fairness o el whitelist.",
        backstory="Auditor determinista y adversarial; no ejecuta acciones.",
        llm=llm,
        verbose=False,
        allow_delegation=False,
        max_iter=1,
    )
    agents["coordinator"] = build_coordinator_agent(cfg, llm)
    return agents


def build_coordinator_agent(cfg, llm):
    parts = _crewai()
    if parts is None or llm is None:
        return None
    Agent, _Crew, _LLM, _Process, _Task = parts
    return Agent(
        role="Coordinator",
        goal="Sintetizar veredictos en una decisión conservadora y explicable.",
        backstory=(
            "Coordinador de NEBULA: prefiere hold ante conflicto sin evidencia "
            "dirimente y nunca ejecuta acciones."
        ),
        llm=llm,
        verbose=False,
        allow_delegation=False,
        max_iter=1,
    )


class CrewAIBackend:
    """``LLMBackend`` implementation that runs one CrewAI agent/task per call."""

    def __init__(self, api_key: str, *, temperature: float = 0.2, max_tokens: int = 1200):
        self._api_key = api_key or ""
        self._temperature = temperature
        self._max_tokens = max_tokens

    @property
    def enabled(self) -> bool:
        return bool(self._api_key) and crewai_available()

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any],
        model: str,
        timeout_s: float,
        temperature: float = 0.2,
        max_tokens: int = 1200,
        reasoning_effort: str = "low",
    ) -> LLMReply:
        started = time.monotonic()
        if not self._api_key:
            return LLMReply(None, 0, error="no_api_key")
        parts = _crewai()
        if parts is None:
            return LLMReply(None, 0, error=_IMPORT_ERROR or "crewai_unavailable")
        Agent, Crew, _LLM, Process, Task = parts
        llm = build_llm(model, self._api_key, temperature=temperature, max_tokens=max_tokens)
        if llm is None:
            return LLMReply(None, 0, error="crewai_llm_unavailable")
        system = "\n\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
        user = "\n\n".join(m.get("content", "") for m in messages if m.get("role") != "system")
        agent = Agent(
            role="NEBULA specialist",
            goal="Responder con el JSON exacto solicitado, sin inventar datos.",
            backstory=system or "Especialista del sistema multi-agente de NEBULA.",
            llm=llm,
            verbose=False,
            allow_delegation=False,
            max_iter=1,
        )
        task = Task(
            description=(
                f"{user}\n\nDevuelve SOLO un objeto JSON válido que cumpla este esquema:\n"
                f"{json.dumps(schema, ensure_ascii=False)}"
            ),
            expected_output="Un objeto JSON válido, sin markdown ni texto adicional.",
            agent=agent,
        )
        crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)
        try:
            result = await asyncio.wait_for(asyncio.to_thread(crew.kickoff), timeout=max(1.0, timeout_s))
        except Exception as e:
            latency = int((time.monotonic() - started) * 1000)
            logging.warning(f"SMAC crew | CrewAI kickoff failed: {e}")
            return LLMReply(None, latency, error=f"crewai:{type(e).__name__}")
        text = getattr(result, "raw", None) or str(result)
        parsed = extract_json_object(text)
        latency = int((time.monotonic() - started) * 1000)
        if parsed is None:
            return LLMReply(None, latency, error="crewai_invalid_json")
        return LLMReply(parsed, latency)


__all__ = [
    "CrewAIBackend",
    "build_coordinator_agent",
    "build_llm",
    "build_specialist_agents",
    "crewai_available",
]
