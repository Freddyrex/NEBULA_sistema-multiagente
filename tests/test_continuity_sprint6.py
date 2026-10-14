"""Sprint 6 (ROADMAP_AGENTE_ADAPTATIVO): continuar de una ejecución a otra.

Criterios:
- Detener, cambiar y reanudar con métricas continuas (stitching + linaje).
- Cambio parcial de modelo informa capas reutilizadas vs reiniciadas.
- Checkpoint corrupto/incompatible falla explícito, sin arrancar.
- Panel muestra la cadena padre → hijo.
"""
import asyncio
import inspect
import json
import os
import tempfile

import torch
from torch import nn

from nebula.core.checkpoint import (
    apply_resume,
    list_checkpoints,
    load_weights,
    read_meta,
    save_checkpoint,
    validate,
)
from nebula.frontend.cost_model import can_resume, estimate_cost


def _sd():
    return {"l.weight": torch.zeros(4, 4), "l.bias": torch.zeros(4)}


def _write_parent(tmp, name="parent", nodes=("0", "1"), extra=None):
    scdir = os.path.join(tmp, name)
    os.makedirs(scdir)
    data = {"scenario_title": name, "n_nodes": len(nodes),
            "nodes": {n: {"id": n, "neighbors": []} for n in nodes},
            "dataset": "MNIST", "model": "MLP", "rounds": 10}
    if extra:
        data.update(extra)
    with open(os.path.join(scdir, "scenario.json"), "w") as f:
        json.dump(data, f)
    return scdir


def _ckpt(tmp, scenario="parent", node="0", rnd=4, sd=None, model="MLP"):
    res = save_checkpoint(tmp, scenario, node, rnd, sd or _sd(),
                          {"model_class": "MNISTModelMLP", "model_name": model,
                           "dataset": "MNIST", "params": 20}, None, {}, 3)
    assert res["ok"], res
    return res


def test_save_validate_load_roundtrip_strict():
    with tempfile.TemporaryDirectory() as tmp:
        _ckpt(tmp)
        assert validate(tmp, "parent", "0", 4) == (True, validate(tmp, "parent", "0", 4)[1])
        out = load_weights(tmp, "parent", "0", 4,
                            current_shapes={"l.weight": [4, 4], "l.bias": [4]}, strict=True)
        assert out["reusable"] == ["l.bias", "l.weight"] and out["skipped"] == []
        assert list_checkpoints(tmp, "parent", "0") == [4]
        meta = read_meta(tmp, "parent", "0", 4)
        assert meta["round"] == 4 and meta["sha256"] and meta["has_optimizer"] is False


def test_strict_mismatch_and_partial_report():
    with tempfile.TemporaryDirectory() as tmp:
        _ckpt(tmp)
        try:
            load_weights(tmp, "parent", "0", 4,
                         current_shapes={"l.weight": [4, 8], "l.bias": [8]}, strict=True)
            raise SystemExit("should have raised")
        except ValueError as e:
            assert "incompatible" in str(e)
        out = load_weights(tmp, "parent", "0", 4,
                            current_shapes={"l.weight": [4, 8], "l.bias": [8],
                                            "extra.weight": [2, 2]}, strict=False)
        assert out["reusable"] == [] and "l.weight" in out["skipped"]
        # capa compatible parcial
        sd2 = {"l.weight": torch.zeros(4, 4), "l.bias": torch.zeros(4), "head.weight": torch.zeros(2, 2)}
        _ckpt(tmp, rnd=5, sd=sd2)
        out2 = load_weights(tmp, "parent", "0", 5,
                             current_shapes={"l.weight": [4, 4], "l.bias": [4]}, strict=False)
        assert out2["reusable"] == ["l.bias", "l.weight"] and out2["skipped"] == ["head.weight"]
        assert list_checkpoints(tmp, "parent", "0") == [4, 5]


def test_corrupt_checkpoint_fails_explicitly():
    with tempfile.TemporaryDirectory() as tmp:
        _ckpt(tmp)
        wpath = os.path.join(tmp, "parent", "checkpoints", "participant_0", "r4", "weights.pt")
        with open(wpath, "r+b") as f:
            f.seek(10)
            f.write(b"\x00\xff\x00")
        ok, reason = validate(tmp, "parent", "0", 4)
        assert ok is False and "sha256" in reason
        try:
            load_weights(tmp, "parent", "0", 4)
            raise SystemExit("should have raised")
        except ValueError:
            pass
        ok2, _ = validate(tmp, "parent", "0", 99)
        assert ok2 is False


def test_keep_last_pruning():
    with tempfile.TemporaryDirectory() as tmp:
        for r in (1, 2, 3, 4):
            _ckpt(tmp, rnd=r)
        assert list_checkpoints(tmp, "parent", "0") == [2, 3, 4]


class _FakeModel(nn.Module):
    def __init__(self, out=4):
        super().__init__()
        self.l = nn.Linear(4, out)

    def forward(self, x):
        return self.l(x)


class _FakeTrainer:
    def __init__(self, model):
        self.model = model
        self.loaded = None

    def set_model_parameters(self, params, initialize=False):
        self.model.load_state_dict(params, strict=False)
        self.loaded = params


class _FakeEngine:
    def __init__(self, model):
        self.trainer = _FakeTrainer(model)


def _cfg(tmp, parent="parent", rnd=4, model="MLP"):
    return {"participant": {"scenario_args": {"name": "child", "rounds": 10,
                                              "resume_from": {"scenario": parent,
                                                              "checkpoint_round": rnd}},
                            "data_args": {"dataset": "MNIST"},
                            "model_args": {"model": model},
                            "device_args": {"idx": "0"},
                            "tracking_args": {"config_dir": tmp}}}


def test_apply_resume_full_and_partial():
    with tempfile.TemporaryDirectory() as tmp:
        _write_parent(tmp)
        _ckpt(tmp)  # MLP r4
        eng = _FakeEngine(_FakeModel(4))
        info = apply_resume(eng, _cfg(tmp))
        assert info["resumed"] is True and info["mode"] == "full"
        assert info["checkpoint_round"] == 4 and info["optimizer_restored"] is False

        eng2 = _FakeEngine(_FakeModel(8))  # otra arquitectura
        info2 = apply_resume(eng2, _cfg(tmp, model="CNN"))
        assert info2["mode"] == "partial" and info2["skipped_layers"]


def test_apply_resume_corrupt_never_starts():
    with tempfile.TemporaryDirectory() as tmp:
        _write_parent(tmp)
        _ckpt(tmp)
        wpath = os.path.join(tmp, "parent", "checkpoints", "participant_0", "r4", "weights.pt")
        with open(wpath, "r+b") as f:
            f.seek(10)
            f.write(b"\x00\xff\x00")
        try:
            apply_resume(_FakeEngine(_FakeModel(4)), _cfg(tmp))
            raise SystemExit("should have raised")
        except ValueError as e:
            assert "sha256" in str(e) or "corrupt" in str(e) or "refused" in str(e)


def test_can_resume_matrix():
    meta = {"model_name": "MLP", "dataset": "MNIST",
            "shapes": {"l.weight": [4, 4], "l.bias": [4]}}
    assert can_resume(meta, {"kind": "aggregator"})["mode"] == "full"
    assert can_resume(meta, {"kind": "rounds"})["mode"] == "full"
    same = can_resume(meta, {"kind": "model", "dataset": "MNIST",
                             "from_model": "MLP", "to_model": "MLP"})
    assert same["mode"] == "full" and same["lost_rounds_estimate"] == 0.0
    other = can_resume(meta, {"kind": "model", "dataset": "MNIST",
                              "from_model": "MLP", "to_model": "CNN"},
                       {"l.weight": [4, 4], "l.bias": [4], "head.weight": [2, 2]}, 6)
    assert other["mode"] == "partial"
    assert other["reusable_layers"] == ["l.bias", "l.weight"]
    assert other["skipped_layers"] == ["head.weight"] and other["lost_rounds_estimate"] == 6.0
    bad = can_resume(meta, {"kind": "model", "dataset": "MNIST",
                            "from_model": "MLP", "to_model": "ResNet9"})
    assert bad["mode"] == "none" and "compatible" in bad["detail"]
    missing = can_resume(None, {"kind": "model", "to_model": "CNN"}, None, 6)
    assert missing["mode"] == "none" and missing["lost_rounds_estimate"] == 6.0


def test_transition_uses_resume_info():
    change = {"kind": "model", "dataset": "MNIST", "from_model": "MLP", "to_model": "CNN",
              "total_rounds": 10,
              "expected_gain": {"low": 0.02, "mid": 0.05, "high": 0.08}}
    measured = {"round_time_s": {"final": 20.0}, "ram_percent": {"final": 40.0},
                "cpu_percent": {"final": 40.0}, "rounds_completed": 6}
    resume = {"mode": "full", "reusable_layers": ["all"], "lost_rounds_estimate": 0.0,
              "detail": "mismo modelo"}
    cost = estimate_cost(change, measured, {"avg_neighbors": 2.0},
                         thresholds={"cost_model": {"max_lost_rounds": 0}},
                         resume_info=resume)
    assert cost["transition"]["lost_rounds"]["value"] == 0.0


def test_scenario_accepts_lineage_params_and_child_builder():
    from nebula.controller.scenarios import Scenario
    from nebula.frontend.smac_chat import _build_child_scenario

    sig = inspect.signature(Scenario.__init__)
    kwargs = {}
    for name, p in sig.parameters.items():
        if name == "self":
            continue
        kwargs[name] = p.default if p.default is not inspect.Parameter.empty else None
    kwargs.update(parent_scenario="p", checkpoint_args={"every_n_rounds": 2},
                  resume_from={"scenario": "p", "checkpoint_round": 4})
    sc = Scenario.from_dict(kwargs)
    assert sc.parent_scenario == "p" and sc.checkpoint_args == {"every_n_rounds": 2}

    parent = {"scenario_title": "p", "rounds": 10, "epochs": 2, "model": "MLP",
              "agg_algorithm": "FedAvg", "n_nodes": 2}
    child = _build_child_scenario(parent, {"parent": "p", "checkpoint_round": 4,
                                           "changes": {"rounds": 14, "hack": 1}})
    assert child["rounds"] == 14 and "hack" not in child
    assert child["parent_scenario"] == "p"
    assert child["resume_from"] == {"scenario": "p", "checkpoint_round": 4}
    assert child["scenario_title"].endswith("(resume r4)")


def test_validate_resume_and_propose_guard():
    import json as _json

    from nebula.frontend import smac_chat
    from nebula.frontend.smac_api import SmacDeps, handle_propose

    with tempfile.TemporaryDirectory() as tmp:
        _write_parent(tmp)
        ok, reason, _ = smac_chat._validate_resume(tmp, {"parent": "nope", "checkpoint_round": 4})
        assert ok is False
        ok2, reason2, _ = smac_chat._validate_resume(tmp, {"parent": "parent", "checkpoint_round": 9})
        assert ok2 is False and "nodo 0" in reason2
        _ckpt(tmp, node="0")
        _ckpt(tmp, node="1")
        ok3, reason3, child = smac_chat._validate_resume(
            tmp, {"parent": "parent", "checkpoint_round": 4, "changes": {"rounds": 14}})
        assert ok3 is True and child["rounds"] == 14, reason3
        ok4, reason4, _ = smac_chat._validate_resume(
            tmp, {"parent": "parent", "checkpoint_round": 4, "changes": {"model": "ResNet9"}})
        assert ok4 is False and "compatible" in reason4

        smac_chat._pending_actions.clear()
        deps = SmacDeps(config_dir=tmp, log_dir=tmp, databases_dir=None,
                        controller_host="h", controller_port=1, controller_name="c", api_key="")
        session = {"user": "tester", "role": "admin"}
        s, out = handle_propose(deps, session, "demo", "resume_from",
                                {"parent": "parent", "checkpoint_round": 9}, "resume")
        assert s == 400 and "nodo" in out["error"]
        audit = [_json.loads(l) for l in open(os.path.join(tmp, "demo", "smac_audit.jsonl"))]
        assert audit[-1]["payload"]["guard"] == "checkpoint"
        smac_chat._pending_actions.clear()


def test_execute_resume_posts_child():
    from nebula.frontend import smac_chat

    with tempfile.TemporaryDirectory() as tmp:
        _write_parent(tmp)
        _ckpt(tmp, node="0")
        _ckpt(tmp, node="1")
        posted = {}

        class FakeResp:
            def raise_for_status(self): pass
            def json(self): return {"scenario_name": "nebula_DFL_child"}

        class FakeClient:
            async def post(self, url, json=None):
                posted.update(url=url, json=json)
                return FakeResp()

        out = asyncio.run(smac_chat.execute_resume_from(
            tmp, "h", 1, "demo", "tester",
            {"parent": "parent", "checkpoint_round": 4, "changes": {"rounds": 14}},
            http_client=FakeClient()))
        assert out["ok"] is True and "linked execution" in out["detail"]
        child = posted["json"]["scenario_data"]
        assert child["parent_scenario"] == "parent" and child["rounds"] == 14
        assert posted["url"].endswith("/scenarios/run")

        bad = asyncio.run(smac_chat.execute_resume_from(
            tmp, "h", 1, "demo", "tester",
            {"parent": "parent", "checkpoint_round": 9}, http_client=FakeClient()))
        assert bad["ok"] is False


def test_lineage_chain_and_loop_guard():
    from nebula.frontend.metrics.service import MetricsService

    with tempfile.TemporaryDirectory() as tmp:
        cfg = os.path.join(tmp, "config")
        os.makedirs(cfg)
        _write_parent(cfg, "gp")
        _write_parent(cfg, "p", extra={"parent_scenario": "gp",
                                       "resume_from": {"scenario": "gp", "checkpoint_round": 3}})
        _write_parent(cfg, "c", extra={"parent_scenario": "p",
                                       "resume_from": {"scenario": "p", "checkpoint_round": 4}})
        svc = MetricsService(log_dir=os.path.join(tmp, "logs"), databases_dir=None, config_dir=cfg)
        lin = svc.lineage("c")
        assert lin["depth"] == 2
        assert [(l["parent"], l["checkpoint_round"]) for l in lin["chain"]] == [("gp", 3), ("p", 4)]
        assert svc.lineage("gp")["depth"] == 0
        # self-loop no cuelga
        _write_parent(cfg, "loop", extra={"resume_from": {"scenario": "loop", "checkpoint_round": 1}})
        assert svc.lineage("loop")["depth"] == 0


def test_series_stitching_offsets_and_trims():
    from types import SimpleNamespace

    from nebula.frontend.metrics.service import MetricsService

    with tempfile.TemporaryDirectory() as tmp:
        cfg = os.path.join(tmp, "config")
        os.makedirs(cfg)
        _write_parent(cfg, "p")
        _write_parent(cfg, "c", extra={"resume_from": {"scenario": "p", "checkpoint_round": 4}})
        svc = MetricsService(log_dir=os.path.join(tmp, "logs"), databases_dir=None, config_dir=cfg)

        def rows(scn, vals):
            return [dict(metric="accuracy", scope="node", node_id=0, round=r, value=v, step=r,
                         ts="2026-01-01T00:00:00Z") for r, v in enumerate(vals)]

        parent_rows = rows("p", [0.5, 0.6, 0.7, 0.78, 0.80, 0.81, 0.82])  # r5-6 divergen, se podan
        child_rows = rows("c", [0.805, 0.81])
        svc.provider.load = lambda scn: SimpleNamespace(
            rows=list(parent_rows if scn == "p" else child_rows), warnings=[], participants=[0])
        got, info = svc.series("c", "accuracy")
        by_round = {r["round"]: r["value"] for r in got if r["scope"] == "node"}
        # padre podado a r<=4, hijo desplazado +5: curva continua 0..6
        assert by_round == {0: 0.5, 1: 0.6, 2: 0.7, 3: 0.78, 4: 0.80, 5: 0.805, 6: 0.81}
        assert any("stitched" in w for w in info["warnings"])


def test_prompt_kinds_and_lineage_fallback():
    from nebula.frontend.smac_chat import CHAT_SYSTEM_PROMPT, deterministic_reply

    assert "resume_from" in CHAT_SYSTEM_PROMPT and "lineage" in CHAT_SYSTEM_PROMPT
    state = {"scenario": "c", "status": "running", "round": 2, "nodes": [],
             "config": {}, "metrics_summary": {"metrics": {}, "available": [], "warnings": []},
             "lineage": {"chain": [{"parent": "p", "checkpoint_round": 4}], "depth": 1}}
    assert "p r4" in deterministic_reply(state, "")["reply"]
