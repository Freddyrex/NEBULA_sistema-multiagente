"""Sprint 5 (ROADMAP_AGENTE_ADAPTATIVO): arquitectura según las rondas.

Criterios:
- Plateau simulado → sugiere reducir rondas con ahorro estimado.
- Aceptar cambia total_rounds en todos los nodos (broadcast + parcial si uno
  no responde en el TTL, sin asumir éxito).
- Modelo incompatible rechazado con mensaje claro.
"""
import asyncio
import json
import os
import tempfile

from nebula.frontend.exec_interpreter import suggest_architecture


def _plat_finding(conv=3):
    return {"id": "accuracy_plateau--accuracy", "type": "accuracy_plateau",
            "severity": "info", "metric": "accuracy", "nodes": [],
            "evidence": [{"metric": "accuracy", "round": r, "node_id": None,
                          "value": 0.80, "detail": "global mean"} for r in (3, 4, 5)]
            + [{"metric": "accuracy", "round": conv, "node_id": None, "value": None,
                "detail": "converged_at (digest)"}],
            "summary": "plateau"}


def _measured():
    return {"round_time_s": {"final": 20.0}, "bytes_sent": {"final": 100.0},
            "bytes_received": {"final": 100.0}, "cpu_percent": {"final": 40.0},
            "ram_percent": {"final": 55.0}, "cpu_temp": {"final": 60.0},
            "cpu_temp_est": {"final": 62.0}, "accuracy": {"final": 0.80},
            "f1_macro": {"final": 0.78}, "rounds_completed": 6}


def _declared(total=10, epochs=2):
    return {"total_rounds": total, "epochs": epochs, "model": "MLP",
            "dataset": "MNIST", "n_nodes": 3, "avg_neighbors": 2.0}


def _flat_series(n=6):
    return {"accuracy": [(r, 0.80) for r in range(n)],
            "loss": [(r, 0.40) for r in range(n)]}


def test_plateau_suggests_early_stop_with_savings():
    sugg, rej = suggest_architecture([_plat_finding()], _flat_series(), _measured(), _declared())
    assert len(sugg) == 1
    s = sugg[0]
    assert s["kind"] == "propose_architecture_change"
    assert s["params"]["change"] == {"kind": "rounds", "total_rounds": 4, "current_rounds": 10}
    assert s["params"]["cost"]["compute"]["value"] == -120.0  # 6 rondas x 20s ahorradas
    assert s["requires_confirm"] is True and s["auto"] is False
    assert "Terminar en r4" in s["title"]


def test_strong_improvement_suggests_extension():
    series = {"accuracy": [(r, 0.50 + 0.05 * r) for r in range(8)],
              "loss": [(r, 0.60 - 0.05 * r) for r in range(8)]}
    measured = _measured()
    measured["rounds_completed"] = 8
    sugg, _ = suggest_architecture([], series, measured, _declared(total=10))
    assert len(sugg) == 1
    assert sugg[0]["params"]["change"]["total_rounds"] == 13
    assert sugg[0]["params"]["change"]["trend_slope"] > 0
    assert sugg[0]["params"]["cost"]["benefit"]["basis"] == "measured"


def test_divergence_suggests_fewer_epochs():
    series = {"accuracy": [(r, 0.70) for r in range(6)],
              "loss": [(r, 0.40 + 0.02 * r) for r in range(6)]}
    sugg, _ = suggest_architecture([], series, _measured(), _declared())
    kinds = [s["params"]["change"]["kind"] for s in sugg]
    assert "epochs" in kinds
    ep = [s for s in sugg if s["params"]["change"]["kind"] == "epochs"][0]
    assert ep["params"]["change"]["epochs"] == 1


def test_model_candidate_without_hypothesis_is_rejected_explicitly():
    sugg, rej = suggest_architecture(
        [], _flat_series(), _measured(), _declared(),
        model_options=[{"to_model": "CNN"}])
    assert sugg == []
    assert len(rej) == 1 and "no conviene" in rej[0]["verdict"]


def test_model_candidate_with_hypothesis_can_pass():
    sugg, _ = suggest_architecture(
        [], _flat_series(), _measured(), _declared(),
        model_options=[{"to_model": "CNN",
                        "expected_gain": {"low": 0.02, "mid": 0.05, "high": 0.08}}],
        thresholds={"cost_model": {"ram_limit": 100.0, "cpu_limit": 100.0, "temp_limit": 100.0,
                                   "temp_est_limit": 99.0, "min_gain": 0.01,
                                   "max_lost_rounds": 10, "min_rounds_for_trend": 4}})
    assert len(sugg) == 1
    assert sugg[0]["params"]["change"]["to_model"] == "CNN"


def test_no_data_no_suggestions_no_crash():
    assert suggest_architecture([], {}, {}, {}) == ([], [])
    assert suggest_architecture(None) == ([], [])


def test_finished_run_defers_to_resume():
    sugg, rej = suggest_architecture([_plat_finding()], _flat_series(), _measured(), _declared(),
                                     scenario_completed=True)
    assert sugg == []
    assert len(rej) == 1 and "resume_from" in rej[0]["verdict"]


def _fake_engine_bus():
    from nebula.core import operatorbus as ob

    class FakeEngine:
        total_rounds = 10

        class config:
            participant = {"scenario_args": {"rounds": 10},
                           "training_args": {"epochs": 2},
                           "data_args": {"dataset": "MNIST"},
                           "model_args": {"model": "MLP"}}

    bus = ob.OperatorBus.__new__(ob.OperatorBus)
    bus._engine = FakeEngine()
    return ob, bus


def test_adjust_rounds_updates_engine_and_config():
    ob, bus = _fake_engine_bus()

    class SyncLock:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    bus._engine.get_round_lock = lambda: SyncLock()
    status, detail = asyncio.run(bus._do_adjust_rounds({"total_rounds": 6}))
    assert status == "ok" and "10 -> 6" in detail and "boundary" in detail
    assert bus._engine.total_rounds == 6
    assert bus._engine.config.participant["scenario_args"]["rounds"] == 6
    for bad in (0, -3, "x", None, 100001):
        s, d = asyncio.run(bus._do_adjust_rounds({"total_rounds": bad}))
        assert s == "error"


def test_adjust_epochs_updates_trainer():
    ob, bus = _fake_engine_bus()

    class FakeTrainer:
        epochs = 2

        def set_epochs(self, e): self.epochs = e

    bus._engine.trainer = FakeTrainer()
    status, detail = asyncio.run(bus._do_adjust_epochs({"epochs": 1}))
    assert status == "ok" and "2 -> 1" in detail
    assert bus._engine.trainer.epochs == 1
    s, _ = asyncio.run(bus._do_adjust_epochs({"epochs": 0}))
    assert s == "error"


def test_swap_model_compatible_swaps_and_incompatible_rejects():
    import nebula.core.models.registry as reg

    ob, bus = _fake_engine_bus()
    calls = {}

    class FakeTrainer:
        model = "OLD"

        def cleanup(self): calls["cleanup"] = True
        def set_model(self, m):
            calls["model"] = m
            self.model = m
        def set_current_round(self, r): calls["round"] = r
        def create_trainer(self): calls["create"] = True

    class FakeLock:
        async def locked_async(self): return False

    bus._engine.trainer = FakeTrainer()
    bus._engine.round = 6
    bus._engine._round_in_process_lock = FakeLock()
    # no-op con engine fresco (mismo modelo) se rechaza sin instanciar
    s0, d0 = asyncio.run(bus._do_swap_model({"to_model": "MLP"}))
    assert s0 == "error" and "no-op" in d0
    real = reg.instantiate
    reg.instantiate = lambda ds, mn: f"MODEL-{mn}"
    try:
        status, detail = asyncio.run(bus._do_swap_model({"to_model": "CNN"}))
    finally:
        reg.instantiate = real
    assert status == "ok" and "MLP" in detail and "CNN" in detail and "fresh weights" in detail
    assert calls == {"cleanup": True, "model": "MODEL-CNN", "round": 6, "create": True}
    assert bus._engine.config.participant["model_args"]["model"] == "CNN"

    s2, d2 = asyncio.run(bus._do_swap_model({"to_model": "ResNet9"}))
    assert s2 == "error" and "compatible" in d2 and "CNN" in d2


def test_swap_model_refused_mid_round():
    ob, bus = _fake_engine_bus()

    class BusyLock:
        async def locked_async(self): return True

    bus._engine._round_in_process_lock = BusyLock()
    s, d = asyncio.run(bus._do_swap_model({"to_model": "CNN"}))
    assert s == "error" and "boundary" in d


def test_collect_results_and_broadcast_partial():
    from nebula.core.operatorbus import collect_command_results, issue_operator_command
    from nebula.frontend import smac_chat

    with tempfile.TemporaryDirectory() as tmp:
        scdir = os.path.join(tmp, "demo")
        os.makedirs(os.path.join(scdir))
        with open(os.path.join(scdir, "scenario.json"), "w") as f:
            json.dump({"n_nodes": 3, "nodes": {"0": {"neighbors": ["1"]},
                                               "1": {"neighbors": ["0", "2"]},
                                               "2": {"neighbors": ["1"]}}}, f)
        cid = issue_operator_command(tmp, "demo", "all", "adjust_rounds", {"total_rounds": 6})
        folder = os.path.join(tmp, "demo", "operator_commands")
        with open(os.path.join(folder, f"{cid}.result.0.json"), "w") as f:
            json.dump({"id": cid, "node": "0", "status": "ok", "detail": "x", "finished_at": 1.0}, f)
        got = collect_command_results(folder, cid, ["0", "1", "2"])
        assert list(got["results"]) == ["0"] and got["missing"] == ["1", "2"]

        real_wait = smac_chat.OPERATOR_RESULT_WAIT_S
        real_poll = smac_chat.OPERATOR_POLL_S
        smac_chat.OPERATOR_RESULT_WAIT_S = 0.3
        smac_chat.OPERATOR_POLL_S = 0.05
        try:
            out = smac_chat.execute_operator_action(tmp, "demo", "all", "adjust_rounds", {"total_rounds": 6})
        finally:
            smac_chat.OPERATOR_RESULT_WAIT_S = real_wait
            smac_chat.OPERATOR_POLL_S = real_poll
        assert out["ok"] is False and out["partial"] is True
        assert out["nodes_missing"] == ["0", "1", "2"] or set(out["nodes_missing"]) <= {"0", "1", "2"}
        assert "ALERT" in out["detail"] and "partial" in out["detail"]


def test_execute_routes_and_guard_whitelist():
    import asyncio as _aio

    from nebula.frontend import smac_chat
    from nebula.frontend.cost_model import validate_for_backend

    seen = {}

    def fake_exec(config_dir, scenario, target, action, params, wait_result=True):
        seen.update(target=target, action=action, params=params)
        return {"ok": True, "detail": "fake"}

    real = smac_chat.execute_operator_action
    smac_chat.execute_operator_action = fake_exec
    try:
        out = _aio.run(smac_chat.execute_action(
            {"kind": "propose_architecture_change",
             "params": {"change": {"kind": "rounds", "total_rounds": 7}},
             "scenario": "s", "username": "u"}, "/tmp", "h", 1, "c"))
        assert seen == {"target": "all", "action": "adjust_rounds",
                        "params": {"total_rounds": 7}}
        out2 = _aio.run(smac_chat.execute_action(
            {"kind": "propose_architecture_change",
             "params": {"change": {"kind": "batch", "batch_size": 64}},
             "scenario": "s", "username": "u"}, "/tmp", "h", 1, "c"))
        assert out2["ok"] is False and "rebuild" in out2["detail"]
    finally:
        smac_chat.execute_operator_action = real

    ok, reason, _ = validate_for_backend(
        {"change": {"kind": "rounds", "total_rounds": 7, "hack": 1},
         "cost": {"compute": {}, "resources": {}, "network": {}, "transition": {}, "benefit": {}}},
        None, {})
    assert ok is False and "no editables" in reason
    ok2, reason2, _ = validate_for_backend(
        {"change": {"kind": "model", "dataset": "MNIST", "from_model": "MLP", "to_model": "ResNet9"},
         "cost": {"compute": {}, "resources": {}, "network": {}, "transition": {}, "benefit": {}}},
        None, {})
    assert ok2 is False and "compatible" in reason2


def test_prompt_and_fallback_cover_architecture():
    from nebula.frontend.smac_chat import CHAT_SYSTEM_PROMPT, deterministic_reply

    assert "architecture.suggestions" in CHAT_SYSTEM_PROMPT
    state = {"scenario": "d", "status": "running", "round": 6, "nodes": [],
             "config": {}, "metrics_summary": {"metrics": {}, "available": [], "warnings": []},
             "architecture": {"suggestions": [{"title": "Terminar en r4", "reason": "ahorro 120s"}],
                              "rejected": [{"verdict": "no conviene: beneficio desconocido"}]}}
    reply = deterministic_reply(state, "")["reply"]
    assert "Terminar en r4" in reply and "no conviene" in reply
