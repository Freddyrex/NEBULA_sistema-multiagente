"""Sprint 8: la conversación del operador continúa entre simulaciones.

Criterios:
- Cada intercambio (pregunta + respuesta) queda persistido por usuario.
- Al abrir una simulación nueva, el snapshot del LLM lleva el hilo anterior
  (`conversation.messages` + `conversation.carried`).
- El arrastre usa el linaje (ancestros) cuando existe y, si no, la simulación
  más reciente del usuario (escenario creado a mano).
- El hilo de un usuario no se mezcla con el de otro.
- El snapshot encogido (anti-413) mantiene el hilo recortado.
"""
import copy
import os
import tempfile

from nebula.frontend.conversation import (
    append_exchange,
    build_conversation_context,
    load_conversation,
)
from nebula.frontend.smac_chat import shrink_snapshot_for_llm


def _ask(log_dir, user, scenario, question, answer, actions=None):
    assert append_exchange(log_dir, user, scenario, question, answer, actions)


def test_exchange_is_persisted_per_user():
    with tempfile.TemporaryDirectory() as tmp:
        _ask(tmp, "admin", "alpha", "¿cómo va?", "Todo bien")
        entries = load_conversation(tmp, "admin")
        assert [e["role"] for e in entries] == ["user", "agent"]
        assert entries[0]["text"] == "¿cómo va?"
        assert entries[1]["text"] == "Todo bien"
        assert entries[0]["scenario"] == "alpha"
        # Otro usuario no ve el hilo ajeno.
        assert load_conversation(tmp, "otro") == []


def test_persisted_file_lives_in_conversations_dir():
    with tempfile.TemporaryDirectory() as tmp:
        _ask(tmp, "Admin", "alpha", "hola", "hola")
        assert os.path.isfile(os.path.join(tmp, "_conversations", "admin.json"))


def test_new_scenario_carries_previous_thread():
    with tempfile.TemporaryDirectory() as tmp:
        _ask(tmp, "admin", "alpha", "propón 20 rondas", "uso 20 rondas")
        _ask(tmp, "admin", "alpha", "¿y el agregador?", "Krum")
        # Simulación nueva (creada a mano: no hay linaje).
        ctx = build_conversation_context(tmp, "admin", "beta")
        assert ctx["messages"] == []          # aún no se habló en beta
        assert ctx["carried_from"] == "alpha"
        assert [m["text"] for m in ctx["carried"]] == [
            "propón 20 rondas", "uso 20 rondas", "¿y el agregador?", "Krum",
        ]
        # Al hablar en beta, el hilo nuevo se acumula sobre el arrastrado.
        _ask(tmp, "admin", "beta", "aplicado, ¿y ahora?", "sí")
        ctx2 = build_conversation_context(tmp, "admin", "beta")
        assert [m["text"] for m in ctx2["messages"]] == [
            "aplicado, ¿y ahora?", "sí",
        ]
        # El hilo ordenado es la conversación completa (lo anterior primero).
        assert [m["text"] for m in ctx2["thread"]] == [
            "propón 20 rondas", "uso 20 rondas", "¿y el agregador?", "Krum",
            "aplicado, ¿y ahora?", "sí",
        ]
        assert ctx2["carried_from"] == "alpha"


def test_lineage_ancestors_drive_the_carry_over():
    with tempfile.TemporaryDirectory() as tmp:
        _ask(tmp, "admin", "alpha", "resumen de alpha", "ok alpha")
        _ask(tmp, "admin", "beta", "resumen de beta", "ok beta")
        chain = [{"scenario": "beta", "parent": "alpha", "checkpoint_round": 4}]
        ctx = build_conversation_context(tmp, "admin", "beta", chain=chain)
        # El linaje manda: lo arrastrado es el ancestro, no el más reciente.
        assert ctx["carried_from"] == "alpha"
        assert [m["text"] for m in ctx["carried"]] == ["resumen de alpha", "ok alpha"]


def test_general_thread_is_used_when_there_is_no_scenario():
    with tempfile.TemporaryDirectory() as tmp:
        _ask(tmp, "admin", "", "crea un escenario nuevo", "listo")
        ctx = build_conversation_context(tmp, "admin", "gamma")
        assert ctx["carried_from"] == ""     # hilo general (sin escenario)
        assert [m["text"] for m in ctx["carried"]] == ["crea un escenario nuevo", "listo"]


def test_conversation_is_capped_and_shrunk():
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(40):
            _ask(tmp, "admin", "alpha", f"pregunta {i}", f"respuesta {i}")
        ctx = build_conversation_context(tmp, "admin", "alpha")
        assert len(ctx["messages"]) <= 20
        slim = shrink_snapshot_for_llm({"conversation": ctx})
        assert len(slim["conversation"]["messages"]) <= 8
        assert len(slim["conversation"]["carried"]) <= 6
        assert len(slim["conversation"]["thread"]) <= 12


def test_missing_log_dir_is_safe():
    ctx = build_conversation_context(None, "admin", "alpha")
    assert ctx == {
        "messages": [],
        "carried": [],
        "thread": [],
        "carried_from": None,
        "scenarios": [],
    }


def test_handle_ask_persists_and_carries_the_thread():
    """El intercambio se guarda y el siguiente escenario lo recibe."""
    import asyncio

    from nebula.frontend import smac_api
    from nebula.frontend.smac_api import SmacDeps, handle_ask

    seen: list[dict] = []

    async def fake_ask(message, context, api_key, **kwargs):
        # Copia: el snapshot cacheado se refresca in-place tras persistir el
        # intercambio, así que hay que fijarlo en el momento de la llamada.
        seen.append(copy.deepcopy(context.get("conversation") or {}))
        return {"reply": "Propongo 20 rondas.", "actions": [], "sources": []}

    real = smac_api.ask_gemini
    smac_api.ask_gemini = fake_ask
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "alpha"))
            deps = SmacDeps(
                config_dir=tmp, log_dir=tmp, databases_dir=None,
                controller_host="h", controller_port=1, controller_name="c", api_key="",
            )
            session = {"user": "admin", "role": "admin"}
            status, out = asyncio.run(handle_ask(deps, session, "¿cuántas rondas?", "alpha"))
            assert status == 200 and out["reply"]
            # El hilo quedó persistido…
            entries = load_conversation(tmp, "admin")
            assert [e["text"] for e in entries] == ["¿cuántas rondas?", "Propongo 20 rondas."]

            # …y la simulación siguiente lo recibe como contexto arrastrado.
            os.makedirs(os.path.join(tmp, "beta"))
            status2, _ = asyncio.run(handle_ask(deps, session, "aplicado", "beta"))
            assert status2 == 200
            conv = seen[-1]
            assert conv["carried_from"] == "alpha"
            assert [m["text"] for m in conv["carried"]] == [
                "¿cuántas rondas?", "Propongo 20 rondas.",
            ]
            # El mensaje en curso va aparte (no forma parte del contexto).
            assert conv["messages"] == []

            # Segunda pregunta en beta: ya se ve el hilo de beta.
            status3, _ = asyncio.run(handle_ask(deps, session, "¿y ahora?", "beta"))
            assert status3 == 200
            assert [m["text"] for m in seen[-1]["messages"]] == [
                "aplicado", "Propongo 20 rondas.",
            ]
    finally:
        smac_api.ask_gemini = real
