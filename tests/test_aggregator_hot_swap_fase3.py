"""Fase 3 (Copilot): cambio de agregador en caliente, un clic, parcial sin reanudar.

Criterios:
- Aceptar un `select_aggregator` completo reanuda (clear_auto_pause).
- Un broadcast PARCIAL NO reanuda en silencio: se audita auto_resume_skipped,
  la respuesta trae partial=True y auto_resumed=False.
- Rechazar reanuda (ya existente, no se rompe).
"""
import asyncio
import json
import os
import tempfile

from nebula.frontend import smac_api
from nebula.frontend.smac_api import SmacDeps, handle_confirm, handle_propose, maybe_auto_pause


def _deps(tmp):
    return SmacDeps(config_dir=tmp, log_dir=tmp, databases_dir=None,
                    controller_host="h", controller_port=1, controller_name="c", api_key="")


def _admin():
    return {"user": "op", "role": "admin"}


def _state_with_suggestion():
    return {"scenario": "demo", "status": "running",
            "nodes": [{"idx": "0", "state": "running"}],
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


def test_partial_change_does_not_resume_silently():
    from nebula.frontend import smac_chat

    smac_chat._pending_actions.clear()
    with tempfile.TemporaryDirectory() as tmp:
        deps = _deps(tmp)
        maybe_auto_pause(deps, "demo", _state_with_suggestion())

        async def fake_partial(record, *a, **kw):
            return {"ok": False, "partial": True, "detail": "command c → all: 1 ok; ALERT partial",
                    "node_results": {"0": {"status": "ok"}}, "nodes_ok": ["0"],
                    "nodes_missing": ["1"], "nodes_failed": []}

        real = smac_api.execute_action
        smac_api.execute_action = fake_partial
        try:
            _, rec = handle_propose(deps, _admin(), "demo", "select_aggregator",
                                    {"node": "all", "algorithm": "Krum"}, "x")
            st, out = asyncio.run(handle_confirm(deps, _admin(), rec["id"], True))
            assert st == 200
            assert out["partial"] is True and out["auto_resumed"] is False
            assert "PARCIAL" in out["detail"]
        finally:
            smac_api.execute_action = real
        # No se emitió pause(all, False): la simulación sigue pausada.
        params = [c["params"] for c in _commands(tmp)]
        assert {"paused": False} not in params
        assert {"paused": True} in params  # de la auto-pausa
        events = [l["event"] for l in _audit(tmp)]
        assert "auto_resume_skipped" in events
        assert "auto_resumed" not in events
    smac_chat._pending_actions.clear()


def test_full_change_resumes():
    from nebula.frontend import smac_chat

    smac_chat._pending_actions.clear()
    with tempfile.TemporaryDirectory() as tmp:
        deps = _deps(tmp)
        maybe_auto_pause(deps, "demo", _state_with_suggestion())

        async def fake_full(record, *a, **kw):
            return {"ok": True, "partial": False, "detail": "command c → all: 1 ok",
                    "node_results": {"0": {"status": "ok"}}, "nodes_ok": ["0"],
                    "nodes_missing": [], "nodes_failed": []}

        real = smac_api.execute_action
        smac_api.execute_action = fake_full
        try:
            _, rec = handle_propose(deps, _admin(), "demo", "select_aggregator",
                                    {"node": "all", "algorithm": "Median"}, "y")
            st, out = asyncio.run(handle_confirm(deps, _admin(), rec["id"], True))
            assert st == 200 and out["ok"] is True
            assert not out.get("partial")
        finally:
            smac_api.execute_action = real
        params = [c["params"] for c in _commands(tmp)]
        assert {"paused": False} in params
        assert "auto_resumed" in [l["event"] for l in _audit(tmp)]
    smac_chat._pending_actions.clear()


def test_reject_still_resumes():
    from nebula.frontend import smac_chat

    smac_chat._pending_actions.clear()
    with tempfile.TemporaryDirectory() as tmp:
        deps = _deps(tmp)
        maybe_auto_pause(deps, "demo", _state_with_suggestion())
        _, rec = handle_propose(deps, _admin(), "demo", "select_aggregator",
                                {"node": "all", "algorithm": "Krum"}, "x")
        st, out = asyncio.run(handle_confirm(deps, _admin(), rec["id"], False))
        assert st == 200 and out["ok"] is False
        params = [c["params"] for c in _commands(tmp)]
        assert {"paused": False} in params
    smac_chat._pending_actions.clear()


def test_viewer_cannot_propose_but_sees_card():
    # El backend rechaza al viewer (403); el frontend oculta botones por rol.
    with tempfile.TemporaryDirectory() as tmp:
        deps = _deps(tmp)
        st, out = handle_propose(deps, {"user": "v", "role": "guest"}, "demo",
                                 "select_aggregator", {"node": "all", "algorithm": "Krum"}, "x")
        assert st == 403
