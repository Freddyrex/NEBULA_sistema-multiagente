"""SMAC API orchestration (spec §12): ask/analyze/propose/confirm/execute/events/metrics.

Thin, dependency-explicit handlers; the FastAPI routes in ``app.py`` are small
wrappers around them. Every handler takes plain values (no ``Request`` objects)
so the whole flow is unit-testable.

Security model (spec §8):

- All callers must be authenticated (routes enforce the NEBULA session).
- RBAC: ``admin`` may do everything, ``operator`` (role ``user``) everything
  except ``stop_scenario``, ``viewer`` (unknown roles) read-only.
- Write actions never execute directly: the LLM proposes, the backend validates
  against the whitelist, the human confirms with a single-use expiring token, and
  the backend executes + audits. ``stop_scenario`` additionally requires typing
  the scenario name (reinforced confirmation).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass

from nebula.frontend.smac_chat import (
    ACTION_KINDS,
    ANALYZE_SYSTEM_PROMPT,
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ask_groq,
    audit_event,
    build_panel_state,
    can_execute_kind,
    check_rate_limit,
    deterministic_reply,
    execute_action,
    peek_pending_action,
    pop_pending_action,
    record_metric,
    register_pending_actions,
    require_role,
)

SNAPSHOT_CACHE_TTL_S = 45.0

_scenario_locks: dict[str, asyncio.Lock] = {}
_snapshot_cache: dict[str, tuple[float, dict]] = {}


@dataclass
class SmacDeps:
    """Explicit dependencies for the handlers (paths, controller, LLM settings)."""

    config_dir: str | None
    log_dir: str | None
    databases_dir: str | None
    controller_host: str
    controller_port: int
    controller_name: str
    api_key: str
    chat_model: str = "openai/gpt-oss-120b"
    chat_effort: str = "medium"
    timeout_s: float = 30.0
    max_tokens: int = 4096


def _lock_for(scenario: str) -> asyncio.Lock:
    lock = _scenario_locks.get(scenario)
    if lock is None:
        lock = asyncio.Lock()
        _scenario_locks[scenario] = lock
    return lock


def get_panel_state(deps: SmacDeps, scenario: str, username: str, use_cache: bool = True) -> dict:
    """Scenario state with a short TTL cache (spec §6.2)."""
    now = time.time()
    if use_cache:
        cached = _snapshot_cache.get(scenario)
        if cached and now - cached[0] < SNAPSHOT_CACHE_TTL_S:
            return cached[1]
    state = build_panel_state(
        deps.config_dir, deps.log_dir, deps.databases_dir, scenario, username, deps.controller_name
    )
    _snapshot_cache[scenario] = (now, state)
    return state


def _new_request_id(provided: str | None) -> str:
    return provided or uuid.uuid4().hex[:12]


async def handle_ask(
    deps: SmacDeps,
    session: dict,
    message: str,
    scenario: str,
    request_id: str | None = None,
    node: str | None = None,
) -> tuple[int, dict]:
    """Ask handler: rate limit → snapshot → Groq (or deterministic fallback).

    Returns (http_status, payload). One Groq call at a time per scenario
    (serialization avoids mismatched replies).
    """
    username = (session or {}).get("user", "?")
    rid = _new_request_id(request_id)
    if not message or not scenario:
        return 400, {"request_id": rid, "error": "Fields 'message' and 'scenario' are required"}
    if not check_rate_limit(username):
        return 429, {"request_id": rid, "error": "Rate limit exceeded (20 messages/min). Slow down."}
    record_metric("ask_total")
    audit_event(deps.log_dir, scenario, username, "ask", {"request_id": rid, "message": message[:500]})
    t0 = time.time()
    async with _lock_for(scenario):
        state = get_panel_state(deps, scenario, username)
        if node:
            message = f"[selected node: {node}] {message}"
        answer = await ask_groq(
            message,
            state,
            deps.api_key,
            model=deps.chat_model,
            reasoning_effort=deps.chat_effort,
            timeout_s=deps.timeout_s,
            max_completion_tokens=deps.max_tokens,
        )
    latency_ms = int((time.time() - t0) * 1000)
    record_metric("latency_ms", latency_ms)
    if answer is None:
        record_metric("ask_degraded")
        fallback = deterministic_reply(state, message)
        audit_event(deps.log_dir, scenario, username, "ask_degraded", {"request_id": rid})
        return 200, {
            "request_id": rid,
            **fallback,
            "model": deps.chat_model,
            "latency_ms": latency_ms,
            "degraded": True,
        }
    record_metric("ask_groq_ok")
    actions = register_pending_actions(answer.get("actions", []), scenario, username)
    record_metric("actions_proposed", len(actions))
    audit_event(
        deps.log_dir, scenario, username, "ask_answered", {"request_id": rid, "actions": [a["id"] for a in actions]}
    )
    return 200, {
        "request_id": rid,
        "reply": answer.get("reply", ""),
        "actions": actions,
        "sources": answer.get("sources", []),
        "model": deps.chat_model,
        "latency_ms": latency_ms,
    }


async def handle_analyze(deps: SmacDeps, session: dict, scenario: str) -> tuple[int, dict]:
    """One-shot structured analysis (State / Risks / Next actions)."""
    username = (session or {}).get("user", "?")
    if not scenario:
        return 400, {"error": "Field 'scenario' is required"}
    if not check_rate_limit(username):
        return 429, {"error": "Rate limit exceeded (20 messages/min). Slow down."}
    state = get_panel_state(deps, scenario, username)
    answer = await ask_groq(
        "Analyze the current federation state.",
        state,
        deps.api_key,
        model=deps.chat_model,
        reasoning_effort=deps.chat_effort,
        timeout_s=deps.timeout_s,
        max_completion_tokens=deps.max_tokens,
        system_prompt=ANALYZE_SYSTEM_PROMPT,
    )
    if answer is None:
        fallback = deterministic_reply(state, "")
        return 200, {"analysis": fallback["reply"], "degraded": True}
    audit_event(deps.log_dir, scenario, username, "analyze", {})
    return 200, {"analysis": answer.get("reply", "")}


def handle_propose(
    deps: SmacDeps, session: dict, scenario: str, kind: str, params: dict, description: str
) -> tuple[int, dict]:
    """Propose a control action (operator/admin only). Returns the confirmation record."""
    username = (session or {}).get("user", "?")
    try:
        role = require_role(session, ROLE_ADMIN, ROLE_OPERATOR)
    except PermissionError as e:
        return 403, {"error": str(e)}
    if kind not in ACTION_KINDS or not scenario:
        return 400, {"error": "A valid 'kind' and 'scenario' are required"}
    if kind == "stop_scenario" and role != ROLE_ADMIN:
        return 403, {"error": "stop_scenario requires the admin role"}
    registered = register_pending_actions(
        [{"kind": kind, "params": params or {}, "description": description or kind}], scenario, username
    )
    if not registered:
        return 400, {"error": "Invalid action"}
    record_metric("actions_proposed", len(registered))
    audit_event(
        deps.log_dir, scenario, username, "action_proposed", {"id": registered[0]["id"], "kind": kind, "params": params}
    )
    return 200, registered[0]


async def _execute_record(deps: SmacDeps, record: dict) -> dict:
    """Execute a validated pending record and audit the outcome."""
    result = await execute_action(
        record, deps.config_dir, deps.controller_host, deps.controller_port, deps.controller_name
    )
    if result.get("ok"):
        record_metric("actions_executed_ok")
    else:
        record_metric("actions_executed_fail")
    audit_event(
        deps.log_dir,
        record["scenario"],
        record["username"],
        "action_executed",
        {
            "id": record["id"],
            "kind": record["kind"],
            "ok": result.get("ok"),
            "detail": str(result.get("detail", ""))[:500],
        },
    )
    return result


async def handle_confirm(
    deps: SmacDeps, session: dict, action_id: str, confirm: bool, confirm_name: str | None = None
) -> tuple[int, dict]:
    """Confirm (or discard) a proposed action by its single-use token."""
    username = (session or {}).get("user", "?")
    record = peek_pending_action(action_id)
    if record is None:
        return 404, {"ok": False, "detail": "Action expired or unknown. Ask again to re-propose it."}
    if not confirm:
        pop_pending_action(action_id)
        record_metric("actions_rejected")
        audit_event(deps.log_dir, record["scenario"], username, "action_rejected", {"id": action_id})
        return 200, {"ok": False, "detail": f"Action discarded: {record['description']}"}
    try:
        role = require_role(session, ROLE_ADMIN, ROLE_OPERATOR)
    except PermissionError as e:
        pop_pending_action(action_id)
        return 403, {"ok": False, "detail": str(e)}
    if not can_execute_kind(role, record["kind"]):
        pop_pending_action(action_id)
        return 403, {"ok": False, "detail": f"Role '{role}' cannot execute {record['kind']}"}
    if record["kind"] == "stop_scenario" and (confirm_name or "").strip() != record["scenario"]:
        # Reinforced confirmation for the high-risk action: type the scenario name.
        return 200, {
            "ok": False,
            "detail": "Type the scenario name to confirm stopping it.",
            "needs_confirm_name": True,
        }
    pop_pending_action(action_id)
    record_metric("actions_confirmed")
    audit_event(deps.log_dir, record["scenario"], username, "action_confirmed", {"id": action_id})
    return 200, await _execute_record(deps, record)


async def handle_execute_token(
    deps: SmacDeps, session: dict, action: dict, confirmation_token: str
) -> tuple[int, dict]:
    """Spec §4.3 unified execution: {action: {type, params}, confirmation_token}.

    The token binds the exact proposal (kind + params); mismatches are rejected
    without consuming the token.
    """
    action = action or {}
    record = peek_pending_action(confirmation_token)
    if record is None:
        return 404, {"status": "error", "detail": "Invalid or expired confirmation token."}
    if action.get("type") != record["kind"] or (action.get("params") or {}) != record["params"]:
        return 400, {"status": "error", "detail": "Action does not match the confirmed proposal."}
    status, result = await handle_confirm(
        deps, session, confirmation_token, True, (action.get("params") or {}).get("confirm_name")
    )
    if isinstance(result, dict) and "needs_confirm_name" in result:
        return 200, {"status": "needs_confirm_name", "detail": result["detail"]}
    if status != 200:
        return status, {"status": "error", "detail": result.get("detail", "error")}
    return 200, {"status": "ok" if result.get("ok") else "error", "detail": result.get("detail", "")}


def _sse_event(name: str, payload: dict) -> str:
    import json as _json

    return f"event: {name}\ndata: {_json.dumps(payload)}\n\n"


def _node_idx_from_target(target: str) -> str | None:
    """Extract a node idx from a decision target string."""
    import re as _re

    m = _re.search(r"'node':\s*'(\d+)'|\"node\":\s*\"(\d+)\"|node\D*(\d+)", str(target or ""))
    if not m:
        return None
    return next(g for g in m.groups() if g is not None)


def _diff_node_events(scenario: str, nodes: list, prev_states: dict) -> list[str]:
    """SSE chunks for node state transitions; updates prev_states in place."""
    chunks = []
    for node in nodes:
        idx, st = node["idx"], node["state"]
        prev = prev_states.get(idx)
        if prev is not None and prev != st:
            if st in ("offline", "stopped"):
                chunks.append(_sse_event("node_offline", {"scenario": scenario, "node": idx, "state": st}))
            elif prev in ("offline", "stopped", "paused") and st == "running":
                chunks.append(_sse_event("node_back", {"scenario": scenario, "node": idx, "state": st}))
        prev_states[idx] = st
    return chunks


def _diff_decision_events(scenario: str, decisions: list) -> list[str]:
    """SSE chunks for high-signal new agent decisions."""
    chunks = []
    for dec in decisions:
        if dec.get("action") == "exclude_node":
            nid = _node_idx_from_target((dec.get("extra") or {}).get("target"))
            chunks.append(
                _sse_event(
                    "predicted_straggler",
                    {"scenario": scenario, "node": nid, "explanation": dec.get("explanation", "")},
                )
            )
        elif dec.get("action") == "select_aggregator":
            chunks.append(
                _sse_event("aggregator_switched", {"scenario": scenario, "explanation": dec.get("explanation", "")})
            )
    return chunks


async def watch_scenario_events(
    deps: SmacDeps,
    scenario: str,
    username: str,
    max_wait_s: float = 90.0,
    poll_s: float = 4.0,
    max_events: int | None = None,
):
    """Yield SSE events for proactive alerts (spec §4.5, P2).

    Watches the SMAC decision log (predicted stragglers, aggregator switches)
    and scenario status (node offline/back, scenario finished) by diffing
    successive snapshots.
    """
    import asyncio as _asyncio

    deadline = time.time() + max_wait_s
    prev_states: dict[str, str] = {}
    prev_status: str | None = None
    seen_decisions = -1
    emitted = 0
    while time.time() < deadline:
        try:
            state = get_panel_state(deps, scenario, username, use_cache=False)
        except Exception:
            await _asyncio.sleep(poll_s)
            continue
        status = state.get("status")
        if prev_status is not None and status != prev_status:
            yield _sse_event("scenario_status", {"scenario": scenario, "from": prev_status, "to": status})
            emitted += 1
            if status in ("completed", "finished"):
                yield _sse_event("scenario_finished", {"scenario": scenario, "status": status})
                return
        prev_status = status
        for chunk in _diff_node_events(scenario, state.get("nodes", []), prev_states):
            yield chunk
            emitted += 1
        decisions = state.get("recent_decisions", [])
        if seen_decisions == -1:
            seen_decisions = len(decisions)
        elif len(decisions) > seen_decisions:
            for chunk in _diff_decision_events(scenario, decisions[seen_decisions:]):
                yield chunk
                emitted += 1
            seen_decisions = len(decisions)
        if max_events is not None and emitted >= max_events:
            return
        await _asyncio.sleep(poll_s)
