"""Sprint 4 (ROADMAP_AGENTE_ADAPTATIVO): costo de cambiar de arquitectura.

Criterios:
- Toda acción propose_architecture_change incluye cost con los 5 rubros.
- Un cambio que excede RAM/temperatura proyectada es rechazado por el backend
  aunque el LLM lo proponga.
- Tests con casos "conviene" y "no conviene".
"""
import asyncio

from nebula.frontend.cost_model import (
    COST_ITEMS,
    classify_continuity,
    compatible_models,
    decide,
    estimate_cost,
    model_size_mb,
    validate_for_backend,
)

TH = {"cost_model": {"ram_limit": 90.0, "cpu_limit": 90.0, "temp_limit": 85.0,
                     "temp_est_limit": 90.0, "min_gain": 0.01,
                     "max_lost_rounds": 5, "min_rounds_for_trend": 4}}


def _measured(**over):
    m = {"round_time_s": {"final": 20.0}, "bytes_sent": {"final": 100.0},
         "bytes_received": {"final": 100.0}, "cpu_percent": {"final": 40.0},
         "ram_percent": {"final": 55.0}, "cpu_temp": {"final": 60.0},
         "cpu_temp_est": {"final": 62.0}, "accuracy": {"final": 0.70},
         "f1_macro": {"final": 0.68}, "rounds_completed": 6}
    m.update(over)
    return m


def test_model_registry_mirrors_node_mapping():
    from nebula.frontend.cost_model import MODEL_REGISTRY

    assert ("MNIST", "MLP") in MODEL_REGISTRY and ("CIFAR10", "RESNET9") in MODEL_REGISTRY
    assert set(compatible_models("MNIST")) >= {"MLP", "CNN"}
    assert compatible_models("Nope") == []
    assert model_size_mb("MNIST", "Nope") is None
    assert model_size_mb("Nope", "MLP") is None


def test_model_size_measured_when_stack_available():
    """Medición real (CPU, sin datos). En .venv: MNIST MLP 235146 params."""
    import pytest

    try:
        import lightning  # noqa: F401
    except ImportError:
        pytest.skip("torch/lightning stack missing in test env (verified in .venv)")
    info = model_size_mb("MNIST", "MLP")
    assert info is not None and info["basis"] == "measured"
    assert info["params"] > 1000 and info["size_mb"] > 0


def test_continuity_matrix():
    assert classify_continuity({"kind": "aggregator"})["mode"] == "full"
    assert classify_continuity({"kind": "rounds"})["mode"] == "full"
    assert classify_continuity({"kind": "model", "dataset": "MNIST",
                                "from_model": "MLP", "to_model": "MLP"})["mode"] == "full"
    assert classify_continuity({"kind": "model", "dataset": "MNIST",
                                "from_model": "MLP", "to_model": "CNN"})["mode"] == "none"
    assert classify_continuity({"kind": "weird"})["mode"] == "none"


def test_rounds_extension_conviene_with_5_items_labeled():
    change = {"kind": "rounds", "total_rounds": 10, "current_rounds": 6, "trend_slope": 0.03}
    cost = estimate_cost(change, _measured(), {"avg_neighbors": 2.0, "n_nodes": 3}, thresholds=TH)
    assert all(k in cost for k in COST_ITEMS)
    assert cost["compute"]["value"] == 80.0 and cost["compute"]["basis"] == "measured"
    assert cost["benefit"]["basis"] == "measured"
    assert set(cost["benefit"]) >= {"low", "mid", "high"}
    assert cost["transition"]["lost_rounds"]["value"] == 0.0
    d = decide(cost, TH)
    assert d["worthwhile"] is True and d["verdict"] == "conviene"


def test_rounds_extension_no_conviene_when_gain_too_small():
    change = {"kind": "rounds", "total_rounds": 10, "current_rounds": 6, "trend_slope": 0.001}
    cost = estimate_cost(change, _measured(), {"avg_neighbors": 2.0, "n_nodes": 3}, thresholds=TH)
    d = decide(cost, TH)
    assert d["worthwhile"] is False and "mínima" in d["verdict"]


def test_model_swap_over_ram_is_no_conviene():
    change = {"kind": "model", "dataset": "MNIST", "from_model": "MLP", "to_model": "CNN",
              "total_rounds": 10,
              "expected_gain": {"low": 0.01, "mid": 0.05, "high": 0.09}}
    sizes = {"MNIST/MLP": {"params": 1000, "size_mb": 0.01, "basis": "measured"},
             "MNIST/CNN": {"params": 3000, "size_mb": 0.03, "basis": "measured"}}
    measured = _measured(**{"ram_percent": {"final": 40.0}})
    cost = estimate_cost(change, measured, {"avg_neighbors": 2.0, "n_nodes": 3},
                         sizes=sizes, thresholds=TH)
    # ratio 3x: 40% -> capped 100% > 90 => no conviene por RAM
    assert cost["resources"]["ram_percent"]["basis"] == "projected"
    d = decide(cost, TH)
    assert d["worthwhile"] is False and any("ram_percent" in r for r in d["reasons"])
    assert cost["transition"]["lost_rounds"]["value"] == 6.0  # se pierde lo andado


def test_missing_data_is_no_conviene_explicit():
    cost = estimate_cost({"kind": "rounds", "total_rounds": 10}, {}, {}, thresholds=TH)
    assert cost["compute"]["basis"] == "unavailable"
    d = decide(cost, TH)
    assert d["worthwhile"] is False


def test_backend_rejects_missing_cost_and_over_claims():
    ok, reason, _ = validate_for_backend({"change": {"kind": "rounds", "total_rounds": 10}}, _measured(), {})
    assert ok is False and "5 rubros" in reason
    change = {"kind": "rounds", "total_rounds": 10, "current_rounds": 6, "trend_slope": 0.03}
    cost = estimate_cost(change, _measured(), {"avg_neighbors": 2.0}, thresholds=TH)
    cost["resources"]["ram_percent"] = {"value": 99.0, "unit": "%", "basis": "hypothesis"}
    ok2, reason2, _ = validate_for_backend({"change": change, "cost": cost}, _measured(), {})
    assert ok2 is False and "ram_percent" in reason2


def test_backend_rejects_even_if_llm_claim_passes():
    """El recálculo con medidos manda sobre lo declarado por el LLM."""
    change = {"kind": "model", "dataset": "MNIST", "from_model": "MLP", "to_model": "CNN",
              "total_rounds": 10,
              "expected_gain": {"low": 0.01, "mid": 0.05, "high": 0.09}}
    sizes = {"MNIST/MLP": {"params": 1000, "size_mb": 0.01, "basis": "measured"},
             "MNIST/CNN": {"params": 3000, "size_mb": 0.03, "basis": "measured"}}
    import nebula.frontend.cost_model as cm

    real_size = cm.model_size_mb
    cm.model_size_mb = lambda ds, mn: sizes.get(f"{str(ds).upper()}/{str(mn).upper()}")
    try:
        claimed = estimate_cost(
            change, _measured(**{"ram_percent": {"final": 10.0}, "cpu_percent": {"final": 10.0}}),
            {"avg_neighbors": 2.0}, sizes=sizes, thresholds=TH)
        # el LLM declara recursos bajos aunque el recálculo con RAM real 40%*3 la excede
        measured = _measured(**{"ram_percent": {"final": 40.0}, "cpu_percent": {"final": 10.0}})
        ok, reason, _ = validate_for_backend({"change": change, "cost": claimed}, measured,
                                             {"avg_neighbors": 2.0}, TH)
    finally:
        cm.model_size_mb = real_size
    assert ok is False and "recálculo" in reason


def test_propose_guard_end_to_end():
    import json
    import os
    import tempfile

    from nebula.frontend import smac_chat
    from nebula.frontend.smac_api import SmacDeps, handle_propose

    smac_chat._pending_actions.clear()
    with tempfile.TemporaryDirectory() as tmp:
        deps = SmacDeps(config_dir=tmp, log_dir=tmp, databases_dir=None,
                        controller_host="h", controller_port=1, controller_name="c", api_key="")
        session = {"user": "tester", "role": "admin"}
        # sin costo → 400
        s1, _ = handle_propose(deps, session, "demo", "propose_architecture_change",
                               {"change": {"kind": "rounds", "total_rounds": 10}}, "x")
        assert s1 == 400
        # con costo que excede (sin medidos: claim-only) → 400
        change = {"kind": "rounds", "total_rounds": 10, "current_rounds": 6, "trend_slope": 0.03}
        cost = estimate_cost(change, _measured(), {"avg_neighbors": 2.0}, thresholds=TH)
        cost["resources"]["cpu_temp"] = {"value": 99.0, "unit": "°C", "basis": "hypothesis"}
        s2, out2 = handle_propose(deps, session, "demo", "propose_architecture_change",
                                  {"change": change, "cost": cost}, "x")
        assert s2 == 400 and "cpu_temp" in out2["error"]
        audit = [json.loads(l) for l in open(os.path.join(tmp, "demo", "smac_audit.jsonl"))]
        assert audit[-1]["event"] == "action_rejected" and audit[-1]["payload"]["guard"] == "cost-model"
    smac_chat._pending_actions.clear()


def test_kind_whitelisted_rbac_and_execution_routes():
    import asyncio as _aio

    from nebula.frontend import smac_chat
    from nebula.frontend.smac_chat import ACTION_KINDS, CHAT_SYSTEM_PROMPT, can_execute_kind

    assert "propose_architecture_change" in ACTION_KINDS
    assert "propose_architecture_change" in CHAT_SYSTEM_PROMPT and "5 items" in CHAT_SYSTEM_PROMPT
    assert can_execute_kind("admin", "propose_architecture_change") is True
    assert can_execute_kind("operator", "propose_architecture_change") is True
    assert can_execute_kind("viewer", "propose_architecture_change") is False
    seen = {}
    real = smac_chat.execute_operator_action
    smac_chat.execute_operator_action = lambda *a, **k: seen.update(
        action=a[3], params=a[4]) or {"ok": True, "detail": "fake"}
    try:
        out = _aio.run(smac_chat.execute_action(
            {"kind": "propose_architecture_change",
             "params": {"change": {"kind": "rounds", "total_rounds": 7}},
             "scenario": "s", "username": "u"}, "/tmp", "h", 1, "c"))
    finally:
        smac_chat.execute_operator_action = real
    assert out["ok"] is True and seen["action"] == "adjust_rounds"
