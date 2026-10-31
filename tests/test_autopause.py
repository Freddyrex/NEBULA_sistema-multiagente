"""Pausa automática mientras hay sugerencias pendientes (Sprint 7+).

La simulación se pausa al sugerir y se reanuda al decidir (aceptar/rechazar),
con override manual. Las pausas manuales nunca se tocan.
"""
import asyncio
import json
import os
import tempfile

from nebula.frontend.smac_api import (
    SmacDeps,
    clear_auto_pause,
    handle_confirm,
    handle_propose,
    handle_resume,
    maybe_auto_pause,
)


def _deps(tmp):
    return SmacDeps(config_dir=tmp, log_dir=tmp, databases_dir=None,
                    controller_host="h", controller_port=1, controller_name="c", api_key="")


def _admin():
    return {"user": "op", "role": "admin"}


def _state_with_suggestion():
    return {"scenario": "demo", "status": "running",
            "nodes": [{"idx": "0", "state": "running"}, {"idx": "1", "state": "running"}],
            "suggestions": [{"id": "agg-Krum-x", "kind": "select_aggregator",
                             "title": "Cambiar FedAvg → Krum"}],
            "architecture": {"suggestions": []}}


def _commands(tmp, scenario="demo"):
    folder = os.path.join(tmp, scenario, "operator_commands")
    out = []
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            if name.endswith(".json") and ".result." not in name and not name.startswith("."):
                with open(os.path.join(folder, name)) as f:
                    out.append(json.load(f))
    return out


def _audit(tmp, scenario="demo"):
    path = os.path.join(tmp, scenario, "smac_audit.jsonl")
    if not os.path.isfile(path):
        return []
    return [json.loads(l) for l in open(path)]


def test_pause_fires_once_per_suggestion_set():
    with tempfile.TemporaryDirectory() as tmp:
        deps = _deps(tmp)
        state = _state_with_suggestion()
        assert maybe_auto_pause(deps, "demo", state) is True
        cmds = _commands(tmp)
        assert len(cmds) == 1 and cmds[0]["action"] == "pause"
        assert cmds[0]["target"] == "all" and cmds[0]["params"] == {"paused": True}
        events = [l["event"] for l in _audit(tmp)]
        assert events == ["auto_paused"]
        assert state["auto_paused"]["active"] is True
        # segundo poll: sin duplicados
        assert maybe_auto_pause(deps, "demo", state) is False
        assert len(_commands(tmp)) == 1
        assert [l["event"] for l in _audit(tmp)] == ["auto_paused"]


def test_no_suggestions_no_pause_and_cleared_resumes():
    with tempfile.TemporaryDirectory() as tmp:
        deps = _deps(tmp)
        state = {"scenario": "demo", "status": "running",
                 "nodes": [{"idx": "0", "state": "running"}],
                 "suggestions": [], "architecture": {"suggestions": []}}
        assert maybe_auto_pause(deps, "demo", state) is False
        assert _commands(tmp) == [] and _audit(tmp) == []
        # marcador activo + sugerencias liquidadas → reanuda solo
        from nebula.frontend.smac_chat import audit_event

        audit_event(tmp, "demo", "smac-auto-pause", "auto_paused", {"suggestions": ["x"]})
        assert maybe_auto_pause(deps, "demo", state) is True
        cmds = _commands(tmp)
        assert len(cmds) == 1 and cmds[0]["params"] == {"paused": False}
        assert [l["event"] for l in _audit(tmp)][-1] == "auto_resumed"


def test_manually_paused_nodes_are_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        deps = _deps(tmp)
        state = {"scenario": "demo", "status": "running",
                 "nodes": [{"idx": "0", "state": "paused"}],
                 "suggestions": [{"title": "Cambiar FedAvg → Krum"}],
                 "architecture": {"suggestions": []}}
        assert maybe_auto_pause(deps, "demo", state) is False
        assert _commands(tmp) == [] and _audit(tmp) == []
        assert clear_auto_pause(deps, "demo", "manual") is False


def test_decision_resumes_after_accept_and_reject():
    from nebula.frontend import smac_chat
    from nebula.frontend import smac_api

    smac_chat._pending_actions.clear()
    with tempfile.TemporaryDirectory() as tmp:
        deps = _deps(tmp)
        maybe_auto_pause(deps, "demo", _state_with_suggestion())

        async def fake_exec(record, *a, **kw):
            return {"ok": True, "detail": "fake"}

        real = smac_api.execute_action
        smac_api.execute_action = fake_exec
        try:
            _, rec = handle_propose(deps, _admin(), "demo", "select_aggregator",
                                    {"node": "all", "algorithm": "Krum"}, "x")
            st, out = asyncio.run(handle_confirm(deps, _admin(), rec["id"], True))
            assert st == 200 and out["ok"] is True
            # la sugerencia sigue ahí: próximo poll vuelve a pausar; rechazar reanuda
            maybe_auto_pause(deps, "demo", _state_with_suggestion())
            _, rec2 = handle_propose(deps, _admin(), "demo", "select_aggregator",
                                     {"node": "all", "algorithm": "Median"}, "y")
            st2, out2 = asyncio.run(handle_confirm(deps, _admin(), rec2["id"], False))
            assert st2 == 200 and out2["ok"] is False
        finally:
            smac_api.execute_action = real
        params = [c["params"] for c in _commands(tmp)]
        assert params.count({"paused": True}) == 2
        # aceptar consume el marcador y reanuda; al rechazar ya no hay marcador
        assert params.count({"paused": False}) == 2
        events = [l["event"] for l in _audit(tmp)]
        assert events.count("auto_resumed") == 2
    smac_chat._pending_actions.clear()


def test_manual_resume_override_and_rbac():
    from nebula.frontend.smac_chat import audit_event

    with tempfile.TemporaryDirectory() as tmp:
        deps = _deps(tmp)
        st, out = asyncio.run(handle_resume(deps, _admin(), "demo"))
        assert st == 200 and out == {"ok": True, "resumed": False}
        audit_event(tmp, "demo", "smac-auto-pause", "auto_paused", {"suggestions": ["x"]})
        st2, out2 = asyncio.run(handle_resume(deps, _admin(), "demo"))
        assert st2 == 200 and out2["resumed"] is True
        assert _commands(tmp)[-1]["params"] == {"paused": False}
        st3, _ = asyncio.run(handle_resume(deps, {"user": "v", "role": "guest"}, "demo"))
        assert st3 == 403
        st4, _ = asyncio.run(handle_resume(deps, _admin(), ""))
        assert st4 == 400


def test_prompt_and_fallback_mention_autopause():
    from nebula.frontend.smac_chat import CHAT_SYSTEM_PROMPT, deterministic_reply

    assert "auto-pauses" in CHAT_SYSTEM_PROMPT or "auto_paused" in CHAT_SYSTEM_PROMPT
    state = {"scenario": "d", "status": "running", "round": 6, "nodes": [],
             "config": {}, "metrics_summary": {"metrics": {}, "available": [], "warnings": []},
             "auto_paused": {"active": True, "reason": "sugerencias pendientes"}}
    assert "Pausa automática" in deterministic_reply(state, "dime qué hay")["reply"]
