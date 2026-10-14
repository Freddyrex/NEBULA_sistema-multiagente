"""Pedir un escenario nuevo por chat lleva a la página de creación."""
import asyncio
import os
import tempfile

from nebula.frontend.smac_api import SmacDeps, handle_ask
from nebula.frontend.smac_chat import NEW_SCENARIO_URL, wants_new_scenario


def _deps(tmp):
    return SmacDeps(config_dir=tmp, log_dir=tmp, databases_dir=None,
                    controller_host="h", controller_port=1, controller_name="c", api_key="")


def test_intent_es_en_and_negatives():
    assert wants_new_scenario("Quiero crear un escenario nuevo. ¿Qué necesito?")
    assert wants_new_scenario("crea un escenario para MNIST")
    assert wants_new_scenario("llévame a un nuevo escenario")
    assert wants_new_scenario("hola, creemos un escenario")
    assert wants_new_scenario("vamos a crear un escenario de prueba")
    assert wants_new_scenario("quiero una nueva simulación")
    assert wants_new_scenario("crea una simulación con 3 nodos")
    assert wants_new_scenario("I want to create a new scenario")
    assert wants_new_scenario("take me to the new scenario page")
    assert wants_new_scenario("let's create a scenario")
    assert wants_new_scenario("start a new simulation")
    assert not wants_new_scenario("¿cómo va el escenario?")
    assert not wants_new_scenario("pausa el nodo 1")
    assert not wants_new_scenario("el escenario nuevo va lento")
    assert not wants_new_scenario("")
    assert NEW_SCENARIO_URL == "/platform/dashboard/deployment/"


def test_llm_comprehension_wins_evil_url_dropped_no_dup():
    from nebula.frontend import smac_api, smac_chat

    smac_chat._pending_actions.clear()
    smac_chat._RATE_BUCKETS.clear()
    real = smac_api.ask_groq

    async def fake_ask(*a, **k):
        return {"reply": "Vamos allá.",
                "actions": [{"kind": "navigate",
                             "params": {"url": NEW_SCENARIO_URL},
                             "description": "Abrir creación"},
                            {"kind": "navigate",
                             "params": {"url": "https://evil.example/"},
                             "description": "phishing"}],
                "sources": []}

    smac_api.ask_groq = fake_ask
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "demo"))
            st, out = asyncio.run(handle_ask(
                _deps(tmp), {"user": "op", "role": "admin"},
                "hola, creemos un escenario", "demo"))
    finally:
        smac_api.ask_groq = real
    assert st == 200
    navs = [a for a in out["actions"] if a.get("kind") == "navigate"]
    # una sola: la del LLM; la fallback no duplica y la maligna se descarta
    assert len(navs) == 1 and navs[0]["params"]["url"] == NEW_SCENARIO_URL
    assert "id" not in navs[0] and smac_chat._pending_actions == {}
    smac_chat._pending_actions.clear()


def test_no_scenario_open_still_navigates_for_creation():
    import json as _json
    import os as _os

    from nebula.frontend import smac_chat

    smac_chat._pending_actions.clear()
    smac_chat._RATE_BUCKETS.clear()
    with tempfile.TemporaryDirectory() as tmp:
        st, out = asyncio.run(handle_ask(
            _deps(tmp), {"user": "op", "role": "admin"},
            "hola, creemos un escenario", ""))
        assert st == 200
        navs = [a for a in out["actions"] if a.get("kind") == "navigate"]
        assert len(navs) == 1 and navs[0]["params"]["url"] == NEW_SCENARIO_URL
        assert NEW_SCENARIO_URL in out["reply"]
        st2, out2 = asyncio.run(handle_ask(
            _deps(tmp), {"user": "op", "role": "admin"}, "¿cómo va todo?", ""))
        assert st2 == 400
    smac_chat._pending_actions.clear()


def test_degraded_path_appends_navigate_without_pending():
    from nebula.frontend import smac_chat

    smac_chat._pending_actions.clear()
    smac_chat._RATE_BUCKETS.clear()
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "demo"))
        st, out = asyncio.run(handle_ask(
            _deps(tmp), {"user": "op", "role": "admin"},
            "Quiero crear un escenario nuevo. ¿Qué necesito?", "demo"))
        assert st == 200 and out.get("degraded") is True
        navs = [a for a in out["actions"] if a.get("kind") == "navigate"]
        assert len(navs) == 1 and navs[0]["params"]["url"] == NEW_SCENARIO_URL
        assert "id" not in navs[0]  # sin confirmación
        assert NEW_SCENARIO_URL in out["reply"]
        assert smac_chat._pending_actions == {}  # nada registrado
    smac_chat._pending_actions.clear()


def test_groq_path_appends_navigate_unregistered():
    from nebula.frontend import smac_api, smac_chat

    smac_chat._pending_actions.clear()
    smac_chat._RATE_BUCKETS.clear()
    real = smac_api.ask_groq

    async def fake_ask(*a, **k):
        return {"reply": "Claro.", "actions": [], "sources": []}

    smac_api.ask_groq = fake_ask
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "demo"))
            st, out = asyncio.run(handle_ask(
                _deps(tmp), {"user": "op", "role": "admin"},
                "create a new scenario please", "demo"))
    finally:
        smac_api.ask_groq = real
    assert st == 200 and out.get("degraded", False) is False
    navs = [a for a in out["actions"] if a.get("kind") == "navigate"]
    assert len(navs) == 1 and "id" not in navs[0]
    assert NEW_SCENARIO_URL in out["reply"]
    assert smac_chat._pending_actions == {}
    smac_chat._pending_actions.clear()


def test_no_intent_no_navigate_and_viewer_allowed():
    from nebula.frontend import smac_chat

    smac_chat._pending_actions.clear()
    smac_chat._RATE_BUCKETS.clear()
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "demo"))
        st, out = asyncio.run(handle_ask(
            _deps(tmp), {"user": "v", "role": "guest"}, "¿cómo va?", "demo"))
        assert st == 200
        assert all(a.get("kind") != "navigate" for a in out["actions"])
        assert NEW_SCENARIO_URL not in out.get("reply", "")
        smac_chat._RATE_BUCKETS.clear()
        st2, out2 = asyncio.run(handle_ask(
            _deps(tmp), {"user": "v", "role": "guest"}, "quiero un nuevo escenario", "demo"))
        assert st2 == 200  # navegar no es mutación: viewer también
        assert any(a.get("kind") == "navigate" for a in out2["actions"])
    smac_chat._pending_actions.clear()
