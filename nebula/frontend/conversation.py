"""Memoria de conversación del operador (Sprint 8).

El chat con SMAC-Chat es un hilo único: cuando el operador pasa de una
simulación terminada a una nueva creada con las mejoras que el propio agente
sugirió, el escenario nuevo debe seguir sabiendo qué se discutió, qué se
propuso y qué se rechazó.

El transcript se persiste por usuario en
``<log_dir>/_conversations/<usuario>.json`` (mismo patrón de persistencia que
la memoria de decisiones por escenario) y se inyecta en el snapshot del LLM
como ``conversation`` (mensajes del escenario actual) y ``carried`` (mensajes
de las simulaciones anteriores).

Nunca lanza excepciones: un fallo de disco degrada el contexto, no el chat.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

# Tope de entradas por usuario (fichero completo).
MAX_ENTRIES = 300
# Mensajes del escenario actual enviados al LLM.
MAX_CONTEXT_MESSAGES = 20
# Mensajes arrastrados de simulaciones anteriores.
MAX_CARRIED_MESSAGES = 12
# Tope de caracteres por mensaje persistido.
MAX_TEXT_CHARS = 2000
# Rol del agente en el transcript.
ROLE_USER = "user"
ROLE_AGENT = "agent"

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(username: str) -> str:
    """Nombre de fichero seguro para el usuario (nada de rutas)."""
    name = _SAFE_RE.sub("_", (username or "").strip().lower())[:64]
    return name or "anonymous"


def conversation_path(log_dir: str | None, username: str) -> str | None:
    """Ruta del transcript del usuario, o None si no hay log_dir utilizable."""
    if not log_dir:
        return None
    try:
        base = os.path.join(log_dir, "_conversations")
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, f"{_safe_name(username)}.json")
    except Exception:
        return None


def load_conversation(log_dir: str | None, username: str) -> list[dict[str, Any]]:
    """Transcript completo del usuario (entradas en orden cronológico)."""
    path = conversation_path(log_dir, username)
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_conversation(log_dir: str | None, username: str, entries: list[dict[str, Any]]) -> bool:
    """Guarda el transcript (escritura atómica). True si se persistió."""
    path = conversation_path(log_dir, username)
    if not path:
        return False
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(entries[-MAX_ENTRIES:], f, indent=2)
        os.replace(tmp, path)
    except Exception:
        return False
    return True


def append_message(
    log_dir: str | None,
    username: str,
    scenario: str,
    role: str,
    text: str,
    actions: list[Any] | None = None,
) -> bool:
    """Añade un mensaje al transcript del usuario. True si se persistió."""
    clean = (text or "").strip()
    if not clean:
        return False
    entries = load_conversation(log_dir, username)
    entries.append(
        {
            "ts": time.time(),
            "scenario": scenario or "",
            "role": ROLE_AGENT if role == ROLE_AGENT else ROLE_USER,
            "text": clean[:MAX_TEXT_CHARS],
            "actions": [str(a) for a in (actions or [])][:10],
        }
    )
    return save_conversation(log_dir, username, entries)


def append_exchange(
    log_dir: str | None,
    username: str,
    scenario: str,
    user_text: str,
    agent_text: str,
    actions: list[Any] | None = None,
) -> bool:
    """Persiste el intercambio completo (pregunta + respuesta) de una vez."""
    entries = load_conversation(log_dir, username)
    now = time.time()
    for role, text in ((ROLE_USER, user_text), (ROLE_AGENT, agent_text)):
        clean = (text or "").strip()
        if not clean:
            continue
        entries.append(
            {
                "ts": now,
                "scenario": scenario or "",
                "role": role,
                "text": clean[:MAX_TEXT_CHARS],
                "actions": [str(a) for a in (actions or [])][:10] if role == ROLE_AGENT else [],
            }
        )
    return save_conversation(log_dir, username, entries)


def _message(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": entry.get("role", ROLE_USER),
        "scenario": entry.get("scenario", ""),
        "text": entry.get("text", ""),
    }


def _ancestor_scenarios(chain: Any) -> list[str]:
    """Escenarios antecesores según el linaje (parent → child, oldest first)."""
    out: list[str] = []
    if isinstance(chain, list):
        for link in chain:
            parent = (
                str(link.get("parent") or "").strip()
                if isinstance(link, dict)
                else str(link or "").strip()
            )
            if parent and parent not in out:
                out.append(parent)
    return out


def build_conversation_context(
    log_dir: str | None,
    username: str,
    scenario: str,
    chain: Any = None,
) -> dict[str, Any]:
    """Parte del snapshot con el hilo del operador.

    ``messages`` = mensajes del escenario actual; ``carried`` = mensajes de las
    simulaciones anteriores (los antecesores del linaje si se conocen; si no,
    la simulación más reciente del usuario) para que una simulación nueva
    creada con las sugerencias del agente continúe la misma conversación.

    Nunca lanza excepciones.
    """
    empty: dict[str, Any] = {
        "messages": [],
        "carried": [],
        "thread": [],
        "carried_from": None,
        "scenarios": [],
    }
    entries = load_conversation(log_dir, username)
    if not entries:
        return empty
    try:
        current = scenario or ""
        ancestors = [s for s in _ancestor_scenarios(chain) if s != current]
        current_msgs = [_message(e) for e in entries if e.get("scenario") == current]
        carried: list[dict[str, Any]] = []
        carried_from: str | None = None
        if ancestors:
            for anc in ancestors:
                carried.extend(_message(e) for e in entries if e.get("scenario") == anc)
            carried_from = ancestors[-1] if ancestors else None
        else:
            # Sin linaje conocido (escenario creado a mano): se arrastra la
            # simulación más reciente del usuario, que es de donde sale la
            # conversación que continúa.
            others = [e for e in entries if e.get("scenario") != current]
            last_scenario = str(others[-1].get("scenario") or "") if others else ""
            if not last_scenario:
                named = [str(e.get("scenario") or "") for e in others if e.get("scenario")]
                last_scenario = named[-1] if named else ""
            carried = [_message(e) for e in others if str(e.get("scenario") or "") == last_scenario]
            if carried:
                # "" = hilo general (sin escenario concreto).
                carried_from = last_scenario
        scenarios: list[str] = []
        for entry in entries:
            name = str(entry.get("scenario") or "")
            if name and name not in scenarios:
                scenarios.append(name)
        thread = [*carried, *current_msgs]
        return {
            "messages": current_msgs[-MAX_CONTEXT_MESSAGES:],
            "carried": carried[-MAX_CARRIED_MESSAGES:],
            "thread": thread[-(MAX_CONTEXT_MESSAGES + MAX_CARRIED_MESSAGES):],
            "carried_from": carried_from,
            "scenarios": scenarios[-10:],
        }
    except Exception:
        return empty
