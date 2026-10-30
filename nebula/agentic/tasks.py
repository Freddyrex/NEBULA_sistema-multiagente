"""CrewAI task builders (optional dependency).

Tasks ask for the same JSON schemas used by the direct backend. They are kept
separate from the strategies so a CrewAI version bump only touches this file.
"""

from __future__ import annotations

import json
from typing import Any

from nebula.agentic.prompts import (
    coordinator_messages,
    critic_messages,
    safety_messages,
    specialist_messages,
)
from nebula.agentic.schemas import AGENT_VERDICT_SCHEMA, CREW_DECISION_SCHEMA, SAFETY_VERDICT_SCHEMA


def _task_classes():
    try:
        from crewai import Task
    except Exception:
        return None
    else:
        return Task


def _description(messages: list[dict[str, str]], schema: dict[str, Any]) -> str:
    user = "\n\n".join(m.get("content", "") for m in messages if m.get("role") != "system")
    return (
        f"{user}\n\nDevuelve SOLO JSON válido conforme a este esquema:\n"
        f"{json.dumps(schema, ensure_ascii=False)}"
    )


def build_specialist_tasks(context_payload: Any, agents: dict[str, Any], *, patterns: dict[str, Any]):
    Task = _task_classes()
    if Task is None:
        return []
    tasks = []
    for key, agent in agents.items():
        if key in ("safety", "coordinator") or agent is None:
            continue
        messages = specialist_messages(key, context_payload)
        tasks.append(
            Task(
                description=_description(messages, AGENT_VERDICT_SCHEMA),
                expected_output="AgentVerdict JSON.",
                agent=agent,
                async_execution=True,
            )
        )
    return tasks


def build_safety_task(context_payload: Any, proposals: list[dict[str, Any]], agent: Any, *, dependencies: list[Any]):
    Task = _task_classes()
    if Task is None or agent is None:
        return None
    messages = safety_messages(context_payload, proposals)
    return Task(
        description=_description(messages, SAFETY_VERDICT_SCHEMA),
        expected_output="SafetyVerdict JSON.",
        agent=agent,
        context=dependencies or None,
    )


def build_synthesis_task(
    context_payload: Any,
    verdicts: list[dict[str, Any]],
    safety: dict[str, Any],
    agent: Any,
    *,
    dependencies: list[Any],
):
    Task = _task_classes()
    if Task is None or agent is None:
        return None
    messages = coordinator_messages(context_payload, verdicts)
    return Task(
        description=_description(messages, CREW_DECISION_SCHEMA),
        expected_output="CrewDecision JSON.",
        agent=agent,
        context=dependencies or None,
    )


def build_critic_task(context_payload: Any, proposal: dict[str, Any], agent: Any, *, dependencies: list[Any]):
    Task = _task_classes()
    if Task is None or agent is None:
        return None
    messages = critic_messages(context_payload, proposal)
    return Task(
        description=_description(messages, AGENT_VERDICT_SCHEMA),
        expected_output="AgentVerdict JSON.",
        agent=agent,
        context=dependencies or None,
    )


__all__ = [
    "build_critic_task",
    "build_safety_task",
    "build_specialist_tasks",
    "build_synthesis_task",
]
