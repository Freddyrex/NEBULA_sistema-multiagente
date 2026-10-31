"""Fase 4 (Copilot): cambio estructural ⇒ borrador de nueva simulación.

Criterios:
- `build_draft_scenario` conserva TODA la whitelist y aplica solo los cambios.
- Cambio de topología ⇒ suelta nodes/nodes_graph + topology_hint + pierde attrs.
- Guard: fuera de whitelist / modelo incompatible / sin costo → 400 sin borrador.
- Aceptar devuelve redirect válido y NO reanuda la original; rechazar reanuda.
- Borrador de otro usuario → 403; expirado → 404; id inválido → 400.
- Viewer no puede proponer. URL `?draft=` solo por patrón estricto.
"""
import asyncio
import json
import os
import tempfile

from nebula.frontend.smac_api import SmacDeps, handle_confirm, handle_propose, maybe_auto_pause
from nebula.frontend.smac_chat import (
    DRAFT_WHITELIST,
    build_draft_scenario,
    is_allowed_navigate_url,
    load_draft,
    save_draft,
)

_PARENT = {
    "scenario_title": "Mi prueba",
    "scenario_description": "desc",
    "deployment": "docker",
    "federation": "DFL",
    "topology": "Ring",
    "rounds": 10,
    "dataset": "MNIST",
    "iid": True,
    "partition_selection": "iid",
    "partition_parameter": 1,
    "model": "MLP",
    "agg_algorithm": "FedAvg",
    "logginglevel": "INFO",
    "report_status_data_queue": True,
    "epochs": 1,
    "dp": False,
    "feature_squeezing": False,
    "adversarial_training": False,
    "attacks": False,
    "node_joiner_in_epoch": 0,
    "mobility": False,
    "with_sa": False,
    "strict_topology": False,
    "sar_neighbor_policy": "Distance",
    "nodes": {"0": {"id": 0, "malicious": True, "ip": "10.0.0.1"}, "1": {"id": 1}},
    "nodes_graph": [[0, 1]],
    "adaptive_agent": {"enabled": True, "pause_on_suggestion": True},
    "scenario_name": "nebula_DFL_2026_01_01_00_00_00",
    "accelerator": "cuda",
    "gpu_id": [0],
}


def _cost():
    return {"compute": {"value": 1}, "resources": {"value": 1}, "network": {"value": 1},
            "transition": {"value": 1}, "benefit": {"value": 1}}


def _deps(tmp):
    return SmacDeps(config_dir=tmp, log_dir=tmp, databases_dir=None,
                    controller_host="h", controller_port=1, controller_name="c", api_key="")


def _admin():
    return {"user": "op", "role": "admin"}


def _write_parent(tmp, name="demo"):
    os.makedirs(os.path.join(tmp, name), exist_ok=True)
    with open(os.path.join(tmp, name, "scenario.json"), "w") as f:
        json.dump(_PARENT, f)


def _audit(tmp, scenario="demo"):
    path = os.path.join(tmp, scenario, "smac_audit.jsonl")
    if not os.path.isfile(path):
        return []
    return [json.loads(l) for l in open(path)]


def test_draft_keeps_whitelist_and_applies_federation_change():
    draft = build_draft_scenario(_PARENT, {"kind": "federation", "to_federation": "CFL"},
                                 parent_name="demo", round_no=3, created_by="op")
    for key in DRAFT_WHITELIST:
        if key in _PARENT and key not in ("nodes", "nodes_graph"):
            assert key in draft, key
    assert draft["federation"] == "CFL"
    assert draft["scenario_title"] == "Mi prueba (arq. v2)"
    assert draft["derived_from"]["scenario"] == "demo"
    assert draft["derived_from"]["round"] == 3 and draft["derived_from"]["created_by"] == "op"
    assert draft["parent_scenario"] == "demo"
    assert "scenario_name" not in draft and "accelerator" not in draft and "gpu_id" not in draft
    assert "resume_from" not in draft  # por defecto arranca desde cero


def test_draft_topology_change_drops_nodes_and_hints():
    draft = build_draft_scenario(_PARENT, {"kind": "topology", "to_topology": "Star", "n_nodes": 3},
                                 parent_name="demo")
    assert draft["topology_hint"] == {"type": "Star", "n_nodes": 3}
    assert "nodes" not in draft and "nodes_graph" not in draft
    assert "malicious" in draft["lost_node_attributes"]
    assert "ip" in draft["lost_node_attributes"]


def test_draft_resume_from_only_when_asked():
    draft = build_draft_scenario(
        _PARENT, {"kind": "federation", "to_federation": "SDFL"}, parent_name="demo",
        resume_from={"scenario": "demo", "checkpoint_round": 4})
    assert draft["resume_from"] == {"scenario": "demo", "checkpoint_round": 4}


def test_navigate_url_pattern_strict():
    assert is_allowed_navigate_url("/platform/dashboard/deployment/")
    assert is_allowed_navigate_url("/platform/dashboard/deployment/?draft=0123456789ab")
    assert not is_allowed_navigate_url("/platform/dashboard/deployment/?draft=XYZ")
    assert not is_allowed_navigate_url("https://evil.example/")
    assert not is_allowed_navigate_url("/platform/dashboard/deployment/?draft=0123456789ab&x=1")


def test_guard_rejects_bad_changes_without_draft():
    with tempfile.TemporaryDirectory() as tmp:
        _write_parent(tmp)
        deps = _deps(tmp)
        # sin costo
        st, out = handle_propose(deps, _admin(), "demo", "propose_new_simulation",
                                 {"parent": "demo", "changes": {"kind": "federation", "to_federation": "CFL"}}, "x")
        assert st == 400
        assert not os.path.isdir(os.path.join(tmp, "demo", "drafts"))
        # cambio fuera de whitelist
        st2, _ = handle_propose(deps, _admin(), "demo", "propose_new_simulation",
                                {"parent": "demo", "changes": {"kind": "rounds", "total_rounds": 5, "hack": 1},
                                 "cost": _cost()}, "x")
        assert st2 == 400
        # federación inválida
        st3, _ = handle_propose(deps, _admin(), "demo", "propose_new_simulation",
                                {"parent": "demo", "changes": {"kind": "federation", "to_federation": "XXX"},
                                 "cost": _cost()}, "x")
        assert st3 == 400
        events = [l["event"] for l in _audit(tmp)]
        assert events.count("action_rejected") == 3
        assert all(l["payload"].get("guard") == "cost-model"
                   for l in _audit(tmp) if l["event"] == "action_rejected")
        assert _audit(tmp)[0]["payload"].get("params") is not None


def test_accept_creates_draft_and_keeps_paused():
    from nebula.frontend import smac_chat

    smac_chat._pending_actions.clear()
    with tempfile.TemporaryDirectory() as tmp:
        _write_parent(tmp)
        deps = _deps(tmp)
        maybe_auto_pause(deps, "demo", {"scenario": "demo", "status": "running",
                                        "nodes": [{"idx": "0", "state": "running"}],
                                        "suggestions": [{"id": "arch-1", "title": "x"}],
                                        "architecture": {"suggestions": []}})
        ok, reason, rec = None, None, None
        st, rec = handle_propose(deps, _admin(), "demo", "propose_new_simulation",
                                 {"parent": "demo",
                                  "changes": {"kind": "federation", "to_federation": "CFL"},
                                  "cost": _cost()}, "nueva sim")
        assert st == 200, (st, rec)
        st2, out = asyncio.run(handle_confirm(deps, _admin(), rec["id"], True))
        assert st2 == 200 and out["ok"] is True
        assert out["auto_resumed"] is False
        assert out["redirect"].startswith("/platform/dashboard/deployment/?draft=")
        draft_id = out["draft_id"]
        assert is_allowed_navigate_url(out["redirect"])
        # el borrador existe y es del dueño
        status, draft = load_draft(tmp, draft_id, "op")
        assert status == 200 and draft["federation"] == "CFL"
        # la original sigue pausada: no se emitió pause(all, False)
        cmds = []
        folder = os.path.join(tmp, "demo", "operator_commands")
        if os.path.isdir(folder):
            for name in os.listdir(folder):
                if name.endswith(".json"):
                    cmds.append(json.load(open(os.path.join(folder, name))))
        assert {"paused": False} not in [c.get("params") for c in cmds]
        events = [l["event"] for l in _audit(tmp)]
        assert "action_executed" in events
        assert any(l["payload"].get("draft_id") == draft_id
                   for l in _audit(tmp) if l["event"] == "action_executed")
    smac_chat._pending_actions.clear()


def test_draft_owner_and_ttl_and_invalid_id():
    with tempfile.TemporaryDirectory() as tmp:
        _write_parent(tmp)
        did = save_draft(tmp, "demo", {"federation": "CFL"}, "op")
        assert did and len(did) == 12
        assert load_draft(tmp, did, "other")[0] == 403
        assert load_draft(tmp, did, "op")[0] == 200
        assert load_draft(tmp, "nothexid!!!" , "op")[0] == 400
        assert load_draft(tmp, "0" * 12, "op")[0] == 404
        # expiración
        path = os.path.join(tmp, "demo", "drafts", f"{did}.json")
        rec = json.load(open(path))
        rec["created_at"] = 0
        rec["ttl_s"] = 1
        json.dump(rec, open(path, "w"))
        assert load_draft(tmp, did, "op")[0] == 404


def test_reject_resumes_and_viewer_forbidden():
    from nebula.frontend import smac_chat

    smac_chat._pending_actions.clear()
    with tempfile.TemporaryDirectory() as tmp:
        _write_parent(tmp)
        deps = _deps(tmp)
        maybe_auto_pause(deps, "demo", {"scenario": "demo", "status": "running",
                                        "nodes": [{"idx": "0", "state": "running"}],
                                        "suggestions": [{"id": "arch-1", "title": "x"}],
                                        "architecture": {"suggestions": []}})
        st, rec = handle_propose(deps, _admin(), "demo", "propose_new_simulation",
                                 {"parent": "demo",
                                  "changes": {"kind": "federation", "to_federation": "CFL"},
                                  "cost": _cost()}, "x")
        assert st == 200
        st2, out = asyncio.run(handle_confirm(deps, _admin(), rec["id"], False))
        assert st2 == 200 and out["ok"] is False
        # rechazar reanuda
        cmds = []
        folder = os.path.join(tmp, "demo", "operator_commands")
        if os.path.isdir(folder):
            for name in os.listdir(folder):
                if name.endswith(".json"):
                    cmds.append(json.load(open(os.path.join(folder, name))))
        assert {"paused": False} in [c.get("params") for c in cmds]
        # viewer no puede proponer
        st3, _ = handle_propose(deps, {"user": "v", "role": "guest"}, "demo",
                                "propose_new_simulation",
                                {"parent": "demo", "changes": {"kind": "federation", "to_federation": "CFL"},
                                 "cost": _cost()}, "x")
        assert st3 == 403
    smac_chat._pending_actions.clear()
