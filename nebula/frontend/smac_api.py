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
import contextlib
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from nebula.config.defaults import (
    ACTION_ID_HEX,
    AUDIT_TRUNC_CHARS,
    RATE_LIMIT_PER_MIN,
    S_TO_MS,
    SCENARIO_DONE_STATES,
    SMAC_CHAT_MODEL,
    SMAC_MAX_TOKENS,
    SMAC_REASONING_EFFORT,
    SMAC_SNAPSHOT_TTL_S,
    SMAC_TIMEOUT_S,
    SSE_MAX_WAIT_S,
    SSE_POLL_S,
)
from nebula.frontend.conversation import append_exchange, build_conversation_context
from nebula.frontend.smac_chat import (
    ACTION_KINDS,
    ANALYZE_SYSTEM_PROMPT,
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ask_gemini,
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
    role_of,
)

SNAPSHOT_CACHE_TTL_S = SMAC_SNAPSHOT_TTL_S

_scenario_locks: dict[str, asyncio.Lock] = {}
# (timestamp, state, username): el estado del panel depende del usuario (hilo
# de conversación y contenedores), así que la cache no se comparte entre
# usuarios del mismo escenario.
_snapshot_cache: dict[str, tuple[float, dict, str]] = {}


def _rounds_completed_now(log_dir: str | None, databases_dir: str | None, scenario: str) -> int | None:
    """Best-effort rounds_completed for audit records (Sprint 3).

    Uses the cached MetricsService digest when available; None on any failure
    (audit must never break the propose/confirm flow).
    """
    try:
        from nebula.frontend.metrics import MetricsService

        payload, _ = MetricsService(log_dir=log_dir, databases_dir=databases_dir).summary(scenario)
        rc = (payload.get("scenario") or {}).get("rounds_completed")
        return None if rc is None else int(rc)
    except Exception:
        return None


def _cost_guard_context(deps: SmacDeps, scenario: str) -> tuple[dict | None, dict]:
    """Best-effort (measured, topology) for the Sprint 4 backend guard.

    ``measured`` carries digest finals + rounds_completed (None when no logs);
    ``topology`` carries avg_neighbors/n_nodes from scenario.json (missing →
    network cost stays ``unavailable``, never invented). Never raises.
    """
    measured: dict | None = None
    topology: dict = {}
    try:
        from nebula.frontend.metrics import MetricsService

        payload, _ = MetricsService(log_dir=deps.log_dir, databases_dir=deps.databases_dir).summary(scenario)
        m = payload.get("metrics", {}) or {}
        measured = {"rounds_completed": (payload.get("scenario") or {}).get("rounds_completed")}
        for name in ("round_time_s", "bytes_sent", "bytes_received", "cpu_percent", "ram_percent",
                     "cpu_temp", "cpu_temp_est", "accuracy", "f1_macro", "recall_macro", "precision_macro"):
            entry = m.get(name)
            if isinstance(entry, dict) and entry.get("final") is not None:
                measured[name] = {"final": entry["final"]}
    except Exception:
        measured = None
    try:
        import json as _json
        import os as _os

        with open(_os.path.join(deps.config_dir or "", scenario, "scenario.json")) as f:
            data = _json.load(f) or {}
        raw_nodes = data.get("nodes") or {}
        if isinstance(raw_nodes, dict) and raw_nodes:
            degrees = []
            for raw in raw_nodes.values():
                nb = (raw or {}).get("neighbors") or []
                degrees.append(len(nb) if isinstance(nb, list) else 0)
            if degrees:
                topology["avg_neighbors"] = sum(degrees) / len(degrees)
                topology["n_nodes"] = len(degrees)
        elif data.get("n_nodes"):
            topology["n_nodes"] = int(data["n_nodes"])
    except Exception:
        pass
    return measured, topology


def _guard_architecture_change(deps: SmacDeps, scenario: str, params: dict) -> tuple[bool, str]:
    """Sprint 4 backend guard: cost required + resource limits enforced.

    Recomputes from measured inputs when available and rejects over-limit
    proposals even if the LLM-claimed cost passes. Returns (ok, reason).
    Never raises.
    """
    try:
        from nebula.frontend.cost_model import validate_for_backend
        from nebula.frontend.exec_interpreter import load_thresholds

        try:
            thresholds = load_thresholds()
        except Exception:
            thresholds = None
        measured, topology = _cost_guard_context(deps, scenario)
        ok, reason, _cost = validate_for_backend(params, measured, topology, thresholds)
        return ok, reason
    except Exception as e:
        return False, f"error del guard: {e}"


def _guard_new_simulation(deps: SmacDeps, scenario: str, params: dict) -> tuple[bool, str]:
    """Fase 4 backend guard: structural draft + 5-item cost + measured recompute.

    Reuses ``validate_for_backend`` (whitelist + per-kind structural values +
    model compatibility + resource limits recomputed from measured data) over the
    first structural change, then validates the full draft via
    ``_validate_new_simulation``. Returns (ok, reason). Never raises.
    """
    try:
        from nebula.frontend.cost_model import validate_for_backend
        from nebula.frontend.exec_interpreter import load_thresholds
        from nebula.frontend.smac_chat import _iter_draft_changes

        try:
            thresholds = load_thresholds()
        except Exception:
            thresholds = None
        changes = (params or {}).get("changes") or {}
        # Normalize to the single-change shape validate_for_backend expects.
        first = None
        if isinstance(changes, dict):
            for _kind, change in _iter_draft_changes(changes):
                first = change
                break
        if first is None:
            return False, "rechazado por el backend: changes inválido"
        guard_params = {"change": first, "cost": (params or {}).get("cost")}
        measured, topology = _cost_guard_context(deps, scenario)
        ok, reason, _cost = validate_for_backend(guard_params, measured, topology, thresholds)
        if not ok:
            return False, reason
        # Draft-level validation (parent exists, whitelist, lineage buildable).
        from nebula.frontend.smac_chat import _validate_new_simulation

        ok2, reason2, _draft = _validate_new_simulation(deps.config_dir, params or {})
        if not ok2:
            return False, reason2
        return True, reason
    except Exception as e:
        return False, f"error del guard: {e}"


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
    #: URL base absoluto del controller (p. ej. https://host) cuando se despliega
    #: fuera de la red Docker; vacío = ``http://{controller_host}:{controller_port}``.
    controller_base_url: str = ""
    #: Token interno compartido con el controller para llamadas service-to-service.
    controller_token: str = ""
    chat_model: str = SMAC_CHAT_MODEL
    chat_effort: str = SMAC_REASONING_EFFORT
    timeout_s: float = SMAC_TIMEOUT_S
    max_tokens: int = SMAC_MAX_TOKENS
    #: Catálogo de páginas de la app (id → ruta) que el chat puede ofrecer.
    pages: list[dict] = field(default_factory=list)


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
        if cached and now - cached[0] < SNAPSHOT_CACHE_TTL_S and cached[2] == username:
            return cached[1]
    state = build_panel_state(
        deps.config_dir, deps.log_dir, deps.databases_dir, scenario, username, deps.controller_name
    )
    try:
        maybe_auto_pause(deps, scenario, state)
    except Exception:
        pass
    _snapshot_cache[scenario] = (now, state, username)
    return state


def _refresh_cached_conversation(deps: SmacDeps, scenario: str, username: str) -> None:
    """Pone al día el hilo en el snapshot cacheado tras persistir el intercambio.

    Sin esto, la cache (45 s) serviría un snapshot con el hilo anterior y el
    agente "olvidaría" lo recién dicho.
    """
    cached = _snapshot_cache.get(scenario)
    if not cached or cached[2] != username:
        return
    with contextlib.suppress(Exception):
        state = cached[1]
        state["conversation"] = build_conversation_context(
            deps.log_dir,
            username,
            scenario,
            chain=(state.get("lineage") or {}).get("chain"),
        )


def _suggestion_titles(state: dict) -> list[str]:
    """Titles of pending adaptive suggestions (aggregator + architecture)."""
    out = []
    try:
        for s in state.get("suggestions", []) or []:
            if isinstance(s, dict) and s.get("title"):
                out.append(str(s["title"]))
        for s in (state.get("architecture") or {}).get("suggestions", []) or []:
            if isinstance(s, dict) and s.get("title"):
                out.append(str(s["title"]))
    except Exception:
        pass
    return out


def _suggestion_ids(state: dict) -> tuple[list[str], list[str]]:
    """Stable (suggestion_ids, finding_ids) of pending suggestions.

    Fase 2 (Copilot): the auto_paused audit records which suggestions and
    findings triggered the pause, so the panel can correlate them.
    """
    sids: list[str] = []
    fids: list[str] = []
    try:
        pools = list(state.get("suggestions", []) or [])
        pools += list((state.get("architecture") or {}).get("suggestions", []) or [])
        for s in pools:
            if not isinstance(s, dict):
                continue
            sid = s.get("id") or (s.get("params") or {}).get("id")
            if sid:
                sids.append(str(sid))
            ids = s.get("finding_ids")
            if isinstance(ids, list):
                fids += [str(x) for x in ids]
    except Exception:
        pass
    # Dedupe preserving order.
    return list(dict.fromkeys(sids)), list(dict.fromkeys(fids))


def maybe_auto_pause(deps: SmacDeps, scenario: str, state: dict) -> bool:
    """Pause the simulation while adaptive suggestions are pending (Sprint 7+).

    Fires once per suggestion set: issues operator pause(all) + audits
    auto_paused. If the marker is active but suggestions cleared, resumes
    instead. Manual pauses are never touched (no marker, no resume).
    Fase 1: honours ``adaptive_agent.pause_on_suggestion`` — an old scenario
    without the key (or with it off) never auto-pauses.
    Returns True when the state flag changed. Never raises.
    """
    try:
        from nebula.core.operatorbus import issue_operator_command
        from nebula.frontend.exec_interpreter import adaptive_flags
        from nebula.frontend.smac_chat import audit_event, auto_pause_status

        titles = _suggestion_titles(state)
        status = auto_pause_status(deps.log_dir, scenario)
        if not adaptive_flags(deps.config_dir, scenario).get("pause_on_suggestion", False):
            # Flag off: never pause on our behalf, but still clear a marker
            # left over from a previous run so we don't strand a resume.
            if status.get("active"):
                return clear_auto_pause(deps, scenario, "pause_on_suggestion disabled")
            state["auto_paused"] = {"active": False}
            return False
        nodes = state.get("nodes", []) or []
        any_running = any((n or {}).get("state") == "running" for n in nodes)
        running = str(state.get("status", "")).lower() == "running"
        if titles and running and any_running and not status.get("active"):
            sids, fids = _suggestion_ids(state)
            rnd = state.get("round")
            issue_operator_command(deps.config_dir, scenario, "all", "pause", {"paused": True})
            audit_event(deps.log_dir, scenario, "smac-auto-pause", "auto_paused",
                        {"suggestions": titles[:5],
                         "suggestion_ids": sids[:20], "finding_ids": fids[:20],
                         "round": rnd,
                         "reason": f"{len(titles)} sugerencia(s) pendiente(s): {'; '.join(titles[:2])}"})
            state["auto_paused"] = {"active": True, "reason": "sugerencias pendientes",
                                    "suggestions": titles[:5], "suggestion_ids": sids[:20],
                                    "finding_ids": fids[:20], "round": rnd}
            return True
        if status.get("active") and not titles:
            return clear_auto_pause(deps, scenario, "suggestions cleared")
        state["auto_paused"] = status
        return False
    except Exception:
        return False


def clear_auto_pause(deps: SmacDeps, scenario: str, why: str) -> bool:
    """Resume after an auto-pause (Sprint 7+). No-op without our marker.

    Never resumes manual pauses and never raises.
    """
    try:
        from nebula.core.operatorbus import issue_operator_command
        from nebula.frontend.smac_chat import audit_event, auto_pause_status

        if not auto_pause_status(deps.log_dir, scenario).get("active"):
            return False
        issue_operator_command(deps.config_dir, scenario, "all", "pause", {"paused": False})
        audit_event(deps.log_dir, scenario, "smac-auto-pause", "auto_resumed", {"why": why})
        return True
    except Exception:
        return False


async def handle_resume(deps: SmacDeps, session: dict, scenario: str) -> tuple[int, dict]:
    """Manual resume override while auto-paused (operator/admin only)."""
    username = (session or {}).get("user", "?")
    try:
        role = require_role(session, ROLE_ADMIN, ROLE_OPERATOR)
    except PermissionError as e:
        return 403, {"error": str(e)}
    if not scenario:
        return 400, {"error": "Field 'scenario' is required"}
    _ = role
    resumed = clear_auto_pause(deps, scenario, "manual resume")
    audit_event(deps.log_dir, scenario, username, "auto_resumed_manual", {"resumed": resumed})
    return 200, {"ok": True, "resumed": resumed}


def _new_request_id(provided: str | None) -> str:
    return provided or uuid.uuid4().hex[:ACTION_ID_HEX]


def _crew_is_enabled(deps: SmacDeps, scenario: str) -> bool:
    try:
        from nebula.agentic.config import load_crew_config

        cfg = load_crew_config(deps.config_dir, scenario)
        return bool(cfg.enabled and deps.api_key)
    except Exception:
        return False


def _looks_spanish(message: str) -> bool:
    text = str(message or "").lower()
    return bool(
        any(ch in text for ch in "áéíóúñ¿¡")
        or any(word in text for word in ("qué", "que ", "cómo", "como ", "analiza", "recomienda",
                                         "siguiente", "cambia", "ajusta", "mejora", "nodo",
                                         "ronda", "agregador", "simulación", "escenario"))
    )


async def _run_crew_for(
    deps: SmacDeps,
    session: dict,
    scenario: str,
    state: dict,
    *,
    trigger: str,
    strategy: str | None = None,
):
    """Run the agentic crew for one snapshot; returns ``CrewResult`` or None."""
    if not _crew_is_enabled(deps, scenario):
        return None
    try:
        from nebula.frontend.agentic_context import run_crew

        return await run_crew(
            deps,
            scenario,
            str((session or {}).get("user", "?")),
            state=state,
            role=role_of(session),
            strategy=strategy,
            trigger=trigger,
        )
    except Exception as e:
        logging.warning(f"SMAC crew | disabled after error ({scenario}): {e}")
        return None


def _register_crew_proposal(
    result, deps: SmacDeps, scenario: str, username: str, *, spanish: bool = True
) -> tuple[list[dict], str]:
    """Register an actionable crew proposal as a pending (confirmable) action."""
    from nebula.frontend.agentic_context import crew_result_payload, decision_reply

    actions: list[dict] = []
    try:
        if result.actionable and result.proposal:
            registered = register_pending_actions([result.proposal], scenario, username)
            actions = registered or []
            if registered:
                audit_event(
                    deps.log_dir,
                    scenario,
                    username,
                    "crew_proposed",
                    {
                        "id": registered[0].get("id"),
                        "kind": result.proposal.get("kind"),
                        "params": result.proposal.get("params"),
                        "strategy": result.strategy,
                        "latency_ms": result.latency_ms,
                        "trace": crew_result_payload(result).get("trace"),
                    },
                )
    except Exception as e:
        logging.warning(f"SMAC crew | cannot register proposal ({scenario}): {e}")
    return actions, decision_reply(result, "es" if spanish else "en")


def _llm_navigate_actions(
    answer: dict | None, pages: list[dict], scenario: str = ""
) -> list[dict]:
    """Pass through LLM-proposed 'navigate' actions (Sprint 7+).

    The model proposes a page id ('params.target') from the 'pages' catalog of
    the app; the backend resolves it to an internal URL, so the LLM never
    writes a URL. Legacy 'params.url' proposals are accepted only when they
    match a catalog page for the open scenario. Unknown targets and
    non-catalog URLs are dropped with a warning (never an open redirect).
    Never raises.
    """
    out: list[dict] = []
    seen: set[str] = set()
    try:
        from nebula.frontend.pages import page_url, resolve_nav_target

        for action in (answer or {}).get("actions", []) or []:
            if not isinstance(action, dict) or action.get("kind") != "navigate":
                continue
            params = action.get("params") or {}
            resolved = None
            target = params.get("target")
            if target:
                resolved = resolve_nav_target(target, pages, scenario)
                if not resolved:
                    logging.warning(f"SMAC-Chat | dropping navigate to unknown page: {target!r}")
            if not resolved:
                url = str(params.get("url") or "")
                if url and any(page_url(p, scenario) == url for p in (pages or [])):
                    resolved = {
                        "kind": "navigate",
                        "params": {"url": url},
                        "description": action.get("description") or "",
                    }
                else:
                    logging.warning(f"SMAC-Chat | dropping navigate to non-catalog url: {url!r}")
            if not resolved:
                continue
            url = str((resolved.get("params") or {}).get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            # La etiqueta del botón la escribe el modelo (en su idioma); la
            # del catálogo es solo el respaldo.
            resolved["description"] = action.get("description") or resolved.get("description") or ""
            out.append(resolved)
    except Exception:
        pass
    return out


def _nav_fallback_actions(
    message: str, pages: list[dict], scenario: str = "", lang: str = "es"
) -> tuple[list[dict], str]:
    """Deterministic navigation button for the creation intent (LLM down).

    In the normal path the model detects the intent and proposes the page; this
    keeps the previous behaviour of always offering the creation page when the
    operator asks for a new scenario. Never raises.
    """
    actions: list[dict] = []
    link = ""
    try:
        from nebula.frontend.smac_chat import NEW_SCENARIO_URL, wants_new_scenario

        if wants_new_scenario(message):
            url = NEW_SCENARIO_URL
            target = ""
            purpose = ""
            for page in pages or []:
                if page.get("path") == url:
                    target = page.get("id") or ""
                    purpose = page.get("purpose") or ""
                    break
            actions = [{
                "kind": "navigate",
                "params": {"url": url, **({"target": target} if target else {})},
                "description": purpose or ("Abrir la página de nuevo escenario"
                                           if lang != "en" else "Open the new scenario page"),
            }]
            link = (
                f"Puedes crearlo aquí: {url}"
                if lang != "en"
                else f"You can create it here: {url}"
            )
    except Exception:
        actions, link = [], ""
    return actions, link


def _with_nav_fallback(
    actions: list[dict],
    reply: str,
    message: str,
    pages: list[dict],
    scenario: str,
    lang: str,
) -> tuple[list[dict], str]:
    """Add the deterministic navigation button when the model proposed none."""
    if any(a.get("kind") == "navigate" for a in actions):
        return actions, reply
    navs, link = _nav_fallback_actions(message, pages, scenario, lang)
    if not navs:
        return actions, reply
    return [*actions, *navs], f"{reply}\n{link}" if reply else link


async def handle_ask(
    deps: SmacDeps,
    session: dict,
    message: str,
    scenario: str,
    request_id: str | None = None,
    node: str | None = None,
) -> tuple[int, dict]:
    """Ask handler: rate limit → snapshot → Gemini (or deterministic fallback).

    Returns (http_status, payload). One Gemini call at a time per scenario
    (serialization avoids mismatched replies).
    """
    username = (session or {}).get("user", "?")
    rid = _new_request_id(request_id)
    if not message:
        return 400, {"request_id": rid, "error": "Field 'message' is required"}
    if not scenario:
        # Sin escenario abierto solo se atiende la navegación (no hay snapshot
        # que leer); el resto sigue exigiendo escenario.
        lang = "es" if _looks_spanish(message) else "en"
        navs, link = _nav_fallback_actions(message, deps.pages, "", lang)
        if navs:
            if not check_rate_limit(username):
                return 429, {
                    "request_id": rid,
                    "error": f"Rate limit exceeded ({RATE_LIMIT_PER_MIN} messages/min). Slow down.",
                }
            record_metric("ask_total")
            audit_event(deps.log_dir, "", username, "ask_navigate",
                        {"request_id": rid, "url": navs[0]["params"]["url"]})
            append_exchange(deps.log_dir, username, "", message, link)
            return 200, {"request_id": rid, "reply": link, "actions": navs,
                         "sources": [], "model": deps.chat_model,
                         "latency_ms": 0, "degraded": False}
        return 400, {"request_id": rid, "error": "Field 'scenario' is required"}
    if not check_rate_limit(username):
        return 429, {
            "request_id": rid,
            "error": f"Rate limit exceeded ({RATE_LIMIT_PER_MIN} messages/min). Slow down.",
        }
    record_metric("ask_total")
    audit_event(deps.log_dir, scenario, username, "ask", {"request_id": rid, "message": message[:AUDIT_TRUNC_CHARS]})
    t0 = time.time()
    # Sprint 8: el hilo se persiste con el texto original (sin el prefijo del
    # nodo seleccionado) para que la conversación continúe entre escenarios.
    operator_message = message
    crew_result = None
    async with _lock_for(scenario):
        state = get_panel_state(deps, scenario, username)
        if node:
            message = f"[selected node: {node}] {message}"
        try:
            from nebula.agentic.prompts import is_decision_intent

            wants_crew = bool(is_decision_intent(operator_message))
        except Exception:
            wants_crew = False
        if wants_crew:
            crew_result = await _run_crew_for(
                deps, session, scenario, state, trigger="chat"
            )
            if crew_result is not None and crew_result.strategy in ("disabled", "carry_over"):
                # LLM unavailable/deadline: keep the battle-tested single-agent path.
                crew_result = None
        answer = None
        if crew_result is None:
            answer = await ask_gemini(
                message,
                # El catálogo de páginas viaja en el contexto: el modelo elige
                # el destino (id) y el backend construye la URL.
                {**state, "pages": deps.pages},
                deps.api_key,
                model=deps.chat_model,
                reasoning_effort=deps.chat_effort,
                timeout_s=deps.timeout_s,
                max_completion_tokens=deps.max_tokens,
            )
    latency_ms = int((time.time() - t0) * S_TO_MS)
    record_metric("latency_ms", latency_ms)
    lang = "es" if _looks_spanish(operator_message) else "en"
    if crew_result is not None:
        actions, reply = _register_crew_proposal(
            crew_result, deps, scenario, username, spanish=_looks_spanish(operator_message)
        )
        record_metric("crew_answered")
        if actions:
            record_metric("actions_proposed", len(actions))
        else:
            record_metric("crew_hold")
        actions, reply = _with_nav_fallback(
            actions, reply, operator_message, deps.pages, scenario, lang
        )
        audit_event(
            deps.log_dir, scenario, username, "ask_answered_crew",
            {"request_id": rid, "strategy": crew_result.strategy,
             "actions": [a.get("id") for a in actions if "id" in a]},
        )
        append_exchange(
            deps.log_dir, username, scenario, operator_message, reply,
            [a.get("kind", "") for a in actions if a.get("kind")],
        )
        _refresh_cached_conversation(deps, scenario, username)
        from nebula.frontend.agentic_context import crew_result_payload

        return 200, {
            "request_id": rid,
            "reply": reply,
            "actions": actions,
            "sources": [],
            "model": deps.chat_model,
            "latency_ms": latency_ms,
            "crew": crew_result_payload(crew_result),
        }
    if answer is None:
        record_metric("ask_degraded")
        fallback = deterministic_reply(state, message)
        fb_actions, fb_reply = _with_nav_fallback(
            fallback.get("actions", []) or [], str(fallback.get("reply", "")),
            operator_message, deps.pages, scenario, lang,
        )
        if fb_actions != (fallback.get("actions", []) or []):
            fallback = {**fallback, "reply": fb_reply, "actions": fb_actions}
        audit_event(deps.log_dir, scenario, username, "ask_degraded", {"request_id": rid})
        append_exchange(
            deps.log_dir,
            username,
            scenario,
            operator_message,
            str(fallback.get("reply", "")),
        )
        _refresh_cached_conversation(deps, scenario, username)
        return 200, {
            "request_id": rid,
            **fallback,
            "model": deps.chat_model,
            "latency_ms": latency_ms,
            "degraded": True,
        }
    record_metric("ask_gemini_ok")
    actions = register_pending_actions(answer.get("actions", []), scenario, username)
    record_metric("actions_proposed", len(actions))
    # LLM-comprehended navigation: the model picks a destination KEY and the
    # backend resolves it to an internal URL (the LLM never writes URLs).
    # Validated against the registry, never registered (no confirmation).
    nav_actions = _llm_navigate_actions(answer, deps.pages, scenario)
    actions = [*actions, *nav_actions]
    reply = answer.get("reply", "")
    if not nav_actions:
        # Deterministic fallback (also covers the degraded path above).
        actions, reply = _with_nav_fallback(
            actions, reply, operator_message, deps.pages, scenario, lang
        )
    audit_event(
        deps.log_dir, scenario, username, "ask_answered", {"request_id": rid, "actions": [a.get("id") for a in actions if "id" in a]}
    )
    append_exchange(
        deps.log_dir,
        username,
        scenario,
        operator_message,
        reply,
        [a.get("kind", "") for a in actions if a.get("kind")],
    )
    _refresh_cached_conversation(deps, scenario, username)
    return 200, {
        "request_id": rid,
        "reply": reply,
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
        return 429, {"error": f"Rate limit exceeded ({RATE_LIMIT_PER_MIN} messages/min). Slow down."}
    state = get_panel_state(deps, scenario, username)
    crew_result = await _run_crew_for(deps, session, scenario, state, trigger="analyze")
    if crew_result is not None and crew_result.strategy not in ("disabled", "carry_over"):
        actions, reply = _register_crew_proposal(crew_result, deps, scenario, username)
        record_metric("crew_analyze")
        if actions:
            record_metric("actions_proposed", len(actions))
        audit_event(
            deps.log_dir, scenario, username, "analyze_crew",
            {"strategy": crew_result.strategy, "actions": [a.get("id") for a in actions if "id" in a]},
        )
        from nebula.frontend.agentic_context import crew_result_payload

        return 200, {
            "analysis": reply,
            "actions": actions,
            "crew": crew_result_payload(crew_result),
        }
    answer = await ask_gemini(
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
    if kind == "rollback_action" and role != ROLE_ADMIN:
        return 403, {"error": "rollback_action requires the admin role"}
    if kind == "propose_architecture_change":
        # Sprint 4: sin costo no hay propuesta; el backend revalida límites
        # con datos medidos aunque lo declarado pase.
        ok, reason = _guard_architecture_change(deps, scenario, params or {})
        if not ok:
            audit_event(
                deps.log_dir, scenario, username, "action_rejected",
                {"id": None, "kind": kind, "params": params, "reason": reason, "guard": "cost-model"},
            )
            return 400, {"error": reason}
    if kind == "resume_from":
        # Sprint 6: el checkpoint debe existir y validar en TODOS los nodos
        # del padre; si no, 400 explícito sin registrar nada.
        from nebula.frontend.smac_chat import _validate_resume

        ok, reason, _child = _validate_resume(deps.config_dir, params or {})
        if not ok:
            audit_event(
                deps.log_dir, scenario, username, "action_rejected",
                {"id": None, "kind": kind, "params": params, "reason": reason, "guard": "checkpoint"},
            )
            return 400, {"error": reason}
    if kind == "propose_new_simulation":
        # Fase 4: guard estructural + costo de 5 rubros recalculado con datos
        # medidos; falla → 400 + auditoría guard: cost-model.
        ok, reason = _guard_new_simulation(deps, scenario, params or {})
        if not ok:
            audit_event(
                deps.log_dir, scenario, username, "action_rejected",
                {"id": None, "kind": kind, "params": params, "reason": reason, "guard": "cost-model"},
            )
            return 400, {"error": reason}
    registered = register_pending_actions(
        [{"kind": kind, "params": params or {}, "description": description or kind}], scenario, username
    )
    if not registered:
        return 400, {"error": "Invalid action"}
    record_metric("actions_proposed", len(registered))
    # Fase A: capturar digest base para medir el resultado tras la ejecución.
    try:
        from . import smac_chat

        smac_chat.set_pending_extra(registered[0]["id"], digest_before=_scenario_digest(deps, scenario))
    except Exception:
        pass
    # Sprint 3: la propuesta guarda kind+params (el trigger/previous de una
    # sugerencia viajan en params) y la ronda actual para la ventana de cooldown.
    audit_event(
        deps.log_dir,
        scenario,
        username,
        "action_proposed",
        {
            "id": registered[0]["id"],
            "kind": kind,
            "params": params,
            "round": _rounds_completed_now(deps.log_dir, deps.databases_dir, scenario),
        },
    )
    return 200, registered[0]


async def _execute_record(deps: SmacDeps, record: dict) -> dict:
    """Execute a validated pending record and audit the outcome."""
    # Fase B: capturar el snapshot pre-cambio (estado para revertir) ANTES de
    # ejecutar, para acciones reversibles (agregador/pausa).
    _capture_snapshot(deps, record)
    result = await execute_action(
        record, deps.config_dir, deps.controller_host, deps.controller_port, deps.controller_name,
        deps.log_dir,
        controller_base_url=deps.controller_base_url,
        controller_token=deps.controller_token,
    )
    if result.get("ok"):
        record_metric("actions_executed_ok")
    else:
        record_metric("actions_executed_fail")
    # Sprint 7: toda ejecución audita costo proyectado + baseline + resultado.
    projected, baseline = _projection_for(deps, record)
    payload = {
        "id": record["id"],
        "kind": record["kind"],
        # Sprint 3: params (con trigger/previous/new de la sugerencia) y
        # ronda de ejecución: traza métrica→prev/new→resultado.
        "params": record.get("params", {}),
        "round": _rounds_completed_now(deps.log_dir, deps.databases_dir, record["scenario"]),
        "projected_cost": projected,
        "baseline": baseline,
        "ok": result.get("ok"),
        "detail": str(result.get("detail", ""))[:AUDIT_TRUNC_CHARS],
    }
    # Fase 4: traza el linaje del borrador (id, padre, cambios).
    if record.get("kind") == "propose_new_simulation":
        payload["draft_id"] = result.get("draft_id")
        payload["parent"] = result.get("parent") or (record.get("params") or {}).get("parent")
        payload["changes"] = (record.get("params") or {}).get("changes")
    audit_event(deps.log_dir, record["scenario"], record["username"], "action_executed", payload)
    return result


def _capture_snapshot(deps: SmacDeps, record: dict) -> None:
    """Save a pre-action snapshot for reversible kinds (Fase B rollback)."""
    try:
        kind = record.get("kind")
        if kind not in ("select_aggregator", "pause_node", "resume_node", "propose_architecture_change"):
            return
        from . import smac_chat

        snapshot = {
            "action_id": record.get("id"),
            "kind": kind,
            "params": record.get("params", {}),
            "digest_before": _scenario_digest(deps, record.get("scenario", "")),
        }
        if kind == "select_aggregator":
            snapshot["previous_algorithm"] = _current_aggregator(deps, record.get("scenario", ""))
        smac_chat.save_snapshot(deps.log_dir, record.get("scenario", ""), snapshot)
    except Exception:
        pass


def _current_aggregator(deps: SmacDeps, scenario: str) -> str | None:
    try:
        from nebula.frontend.exec_interpreter import current_aggregator, read_audit_tail
        from nebula.frontend.smac_chat import _read_json_file

        scene = _read_json_file(
            os.path.join(deps.config_dir or "", scenario, "scenario.json")) or {}
        declared = (scene.get("scenario") or {}).get("aggregation") or scene.get("aggregation")
        audit_tail = read_audit_tail(deps.log_dir, scenario) if deps.log_dir else []
        alg, _source = current_aggregator(declared, audit_tail)
        return alg
    except Exception:
        return None


def _projection_for(deps: SmacDeps, record: dict) -> tuple[Any, Any]:
    """Best-effort (projected_cost, baseline) for the execution audit (Sprint 7).

    Architecture proposals carry their guarded cost in params; other kinds get
    a trivial projection (no training impact) or a recomputed one when cheap.
    Baselines come from the cached digest. Never raises; (None, None) on failure.
    """
    try:
        params = record.get("params", {}) or {}
        if isinstance(params.get("cost"), dict):
            claimed = params["cost"]
        else:
            claimed = {"note": "no training mutation expected",
                       "basis": "heuristic", "kind": record.get("kind")}
        baseline = None
        try:
            measured, _topo = _cost_guard_context(deps, record.get("scenario", ""))
            if measured:
                baseline = {k: (v or {}).get("final") for k, v in measured.items()
                            if isinstance(v, dict) and v.get("final") is not None}
                baseline["rounds_completed"] = measured.get("rounds_completed")
        except Exception:
            pass
        return claimed, baseline
    except Exception:
        return None, None


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
        # Sprint 3: el rechazo guarda kind+params para que la política de
        # cooldown pueda suprimir la re-sugerencia del mismo objetivo.
        audit_event(
            deps.log_dir,
            record["scenario"],
            username,
            "action_rejected",
            {"id": action_id, "kind": record.get("kind"), "params": record.get("params", {})},
        )
        try:
            from . import smac_chat

            smac_chat.save_decision_memory_entry(
                deps.log_dir,
                record["scenario"],
                {
                    "suggestion_id": action_id,
                    "kind": record.get("kind"),
                    "params": record.get("params", {}),
                    "decision": "rejected",
                },
            )
        except Exception:
            pass
        # Sprint 7+: la decisión (también rechazar) reanuda tras una auto-pausa.
        try:
            clear_auto_pause(deps, record["scenario"], f"confirm:{action_id}:rejected")
        except Exception:
            pass
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
    result = await _execute_record(deps, record)
    # Sprint 7+/Fase 3: el cambio se aplica en pausa y la decisión reanuda —
    # salvo que el broadcast quede PARCIAL: entonces NO se reanuda en silencio
    # (se alerta y se pregunta). El marcador de auto-pausa queda activo.
    partial = bool(isinstance(result, dict) and result.get("partial"))
    # Fase 4: aceptar una nueva simulación NO reanuda la original (queda pausada,
    # intacta, con su checkpoint pre-cambio): el operador decide cuándo detenerla.
    new_sim = record.get("kind") == "propose_new_simulation" and bool(result.get("ok"))
    if partial:
        try:
            audit_event(deps.log_dir, record["scenario"], username, "auto_resume_skipped",
                        {"id": action_id, "reason": "partial change",
                         "nodes_missing": (result or {}).get("nodes_missing", []),
                         "nodes_failed": (result or {}).get("nodes_failed", [])})
        except Exception:
            pass
    elif new_sim:
        # La original permanece pausada a propósito; solo se audita el hito.
        try:
            audit_event(deps.log_dir, record["scenario"], username, "auto_resume_skipped",
                        {"id": action_id, "reason": "new simulation draft accepted",
                         "draft_id": (result or {}).get("draft_id"),
                         "parent": (result or {}).get("parent")})
        except Exception:
            pass
    else:
        try:
            clear_auto_pause(deps, record["scenario"], f"confirm:{action_id}:executed")
        except Exception:
            pass
    out = dict(result) if isinstance(result, dict) else {"ok": bool(result)}
    if partial:
        out["partial"] = True
        out["auto_resumed"] = False
        out["detail"] = (out.get("detail") or "") + \
            " | cambio PARCIAL: la simulación sigue pausada; revisa y reanuda cuando quieras."
    elif new_sim:
        out["auto_resumed"] = False
    # Fase A: registrar en memoria de decisiones el resultado de lo aceptado y
    # medir el outcome (digest antes/después) para alimentar futuras sugerencias.
    try:
        from . import smac_chat

        smac_chat.save_decision_memory_entry(
            deps.log_dir,
            record["scenario"],
            {
                "suggestion_id": action_id,
                "kind": record.get("kind"),
                "params": record.get("params", {}),
                "decision": "accepted",
                "outcome": _record_action_outcome(deps, record, result),
            },
        )
    except Exception:
        pass
    return 200, out


def _record_action_outcome(deps: SmacDeps, record: dict, result: dict) -> dict:
    """Measure whether the accepted change improved the scenario (Fase A).

    Compares the aggregated digest before/after using the scenario's own
    metrics window; degrades gracefully to ``unknown`` when not measurable.
    """
    outcome = {"status": "unknown", "improved": None, "delta": {}}
    try:
        scenario = record.get("scenario") or ""
        after = _scenario_digest(deps, scenario)
        before = record.get("digest_before") or {}
        if not before:
            outcome["status"] = "baseline"
            outcome["after"] = after
            return outcome
        delta = {}
        for key in ("accuracy", "f1", "loss"):
            if key in before and key in after:
                try:
                    b = float(before[key])
                    a = float(after[key])
                except (TypeError, ValueError):
                    continue
                delta[key] = round(a - b, 6)
        outcome["delta"] = delta
        if delta:
            # accuracy/f1 subir es bueno; loss bajar es bueno.
            good = delta.get("accuracy", 0) + delta.get("f1", 0) - delta.get("loss", 0)
            outcome["improved"] = good > 0
            outcome["status"] = "measured"
    except Exception:
        pass
    return outcome


def _scenario_digest(deps: SmacDeps, scenario: str) -> dict:
    """Best-effort aggregated metric digest for a scenario (last round)."""
    digest: dict = {}
    try:
        from nebula.frontend.metrics import MetricsService

        payload, _ = MetricsService(log_dir=deps.log_dir, databases_dir=deps.databases_dir).summary(scenario)
        m = payload.get("metrics", {}) or {}
        for key in ("accuracy", "f1_macro", "loss"):
            entry = m.get(key)
            if isinstance(entry, dict):
                val = entry.get("final", entry.get("mean", entry.get("last")))
            else:
                val = entry
            if val is not None:
                digest[key] = val
    except Exception:
        pass
    return digest


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
    max_wait_s: float = SSE_MAX_WAIT_S,
    poll_s: float = SSE_POLL_S,
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
            if status in SCENARIO_DONE_STATES:
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
