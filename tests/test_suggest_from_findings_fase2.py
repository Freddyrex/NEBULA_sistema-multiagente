"""Fase 2 (Copilot): mapa hallazgo → sugerencia accionable y cost model extendido.

Criterios:
- Un nodo con RAM sostenida alta genera sugerencia estructural (batch_size).
- Un hallazgo sin sugerencia aplicable (p. ej. f1 sin hipótesis) va a rejected
  y NO se sugiere.
- Cooldown: un objetivo rechazado no vuelve a dispararse.
- Cambios estructurales fuera de whitelist/valores inválidos se rechazan.
- `classify_change` separa hot de structural.
- Una ejecución finalizada nunca sugiere (scenario_completed).
"""
from nebula.frontend.cost_model import (
    _check_change_structure,
    classify_change,
)
from nebula.frontend.exec_interpreter import suggest_from_findings


def _finding(ftype, nodes=None, evidence=None, severity="warning", **kw):
    return {
        "id": kw.get("id", f"{ftype}-1"),
        "type": ftype,
        "severity": severity,
        "metric": kw.get("metric", "round_time_s"),
        "nodes": nodes or [],
        "evidence": evidence or ["measured"],
        "summary": kw.get("summary", ftype),
        **({"hypothesis": kw["hypothesis"]} if "hypothesis" in kw else {}),
    }


def test_ram_pressure_suggests_batch_structural():
    sugg, rejected = suggest_from_findings(
        [_finding("ram_pressure", nodes=[2], evidence=["ram 95%"])],
        measured={"ram_percent": {"final": 95.0}, "rounds_completed": 3},
        declared={"n_nodes": 5, "aggregation": "FedAvg"},
    )
    assert len(sugg) == 1
    s = sugg[0]
    assert s["class"] == "structural"
    assert s["kind"] == "propose_new_simulation"
    assert s["params"]["change"] == {"kind": "batch", "batch_size": 8}
    assert s["finding_ids"] == ["ram_pressure-1"]
    assert rejected == []


def test_low_recall_without_hypothesis_is_rejected():
    sugg, rejected = suggest_from_findings(
        [_finding("low_recall", evidence=["f1 0.30"])],
        measured={"rounds_completed": 5},
        declared={"n_nodes": 5, "federation": "DFL", "iid": False},
    )
    assert sugg == []
    assert rejected and "sin hipótesis" in rejected[0]["verdict"]


def test_low_recall_with_hypothesis_suggests_federation():
    sugg, rejected = suggest_from_findings(
        [_finding("low_recall", evidence=["f1 0.30"],
                  hypothesis={"low": 0.0, "mid": 0.02, "high": 0.05})],
        measured={"rounds_completed": 5},
        declared={"n_nodes": 5, "federation": "DFL", "iid": False},
    )
    assert len(sugg) == 1
    assert sugg[0]["class"] == "structural"
    assert sugg[0]["params"]["change"]["kind"] == "federation"
    assert sugg[0]["params"]["change"]["to_federation"] == "CFL"


def test_straggler_topology_bound_suggests_topology():
    sugg, _ = suggest_from_findings(
        [_finding("straggler", nodes=[3], evidence=["2x median"],
                  hypothesis={"low": 0.0, "mid": 0.02, "high": 0.05})],
        measured={"rounds_completed": 5},
        declared={"n_nodes": 5, "avg_neighbors": 1, "aggregation": "FedAvg"},
    )
    assert len(sugg) == 1
    assert sugg[0]["params"]["change"]["kind"] == "topology"
    assert sugg[0]["params"]["change"]["to_topology"] == "Ring"


def test_byzantine_defers_to_aggregator_policy():
    # The Sprint 3 aggregator policy already emits the hot card → no duplicate.
    sugg, _ = suggest_from_findings(
        [_finding("byzantine_suspected", nodes=[1], evidence=["diverges"])],
        measured={"rounds_completed": 5},
        declared={"n_nodes": 5, "aggregation": "FedAvg"},
    )
    assert all(s["params"]["change"]["kind"] != "aggregator" for s in sugg)


def test_rejected_target_respects_cooldown():
    sugg, _ = suggest_from_findings(
        [_finding("byzantine_suspected", nodes=[1], evidence=["diverges"])],
        measured={"rounds_completed": 5},
        declared={"n_nodes": 5, "aggregation": "FedAvg"},
        cooldown={"last_rejected_algorithm": "Krum"},
    )
    assert sugg == []


def test_completed_scenario_never_suggests():
    sugg, rejected = suggest_from_findings(
        [_finding("ram_pressure", nodes=[2])],
        measured={"ram_percent": {"final": 95.0}, "rounds_completed": 10},
        declared={"n_nodes": 5},
        scenario_completed=True,
    )
    assert sugg == [] and rejected == []


def test_classify_change_hot_vs_structural():
    assert classify_change({"kind": "aggregator", "algorithm": "Krum"}) == "hot"
    assert classify_change({"kind": "rounds", "total_rounds": 9}) == "hot"
    assert classify_change({"kind": "federation", "to_federation": "CFL"}) == "structural"
    assert classify_change({"kind": "topology", "to_topology": "Ring"}) == "structural"
    assert classify_change({"kind": "batch", "batch_size": 8}) == "structural"


def test_check_change_structure_structural_values():
    ok, _ = _check_change_structure({"kind": "federation", "to_federation": "SDFL"})
    assert ok
    ok, why = _check_change_structure({"kind": "federation", "to_federation": "Nope"})
    assert not ok and "no soportada" in why
    ok, _ = _check_change_structure({"kind": "topology", "to_topology": "Ring", "n_nodes": 5})
    assert ok
    ok, why = _check_change_structure({"kind": "topology", "to_topology": "NoSuch"})
    assert not ok and "no soportada" in why
    ok, why = _check_change_structure({"kind": "topology", "to_topology": "Ring", "n_nodes": 0})
    assert not ok and "fuera de rango" in why
    ok, why = _check_change_structure({"kind": "nodes", "n_nodes": 8})
    assert ok
    ok, why = _check_change_structure({"kind": "policy", "sar_neighbor_policy": "Distance"})
    assert ok
    ok, why = _check_change_structure({"kind": "policy", "sar_neighbor_policy": "Bad"})
    assert not ok and "no soportada" in why
    ok, why = _check_change_structure({"kind": "federation", "to_federation": "CFL", "hack": 1})
    assert not ok and "no editables" in why


def test_suggestion_ids_helper():
    from nebula.frontend.smac_api import _suggestion_ids, _suggestion_titles

    state = {
        "suggestions": [{"id": "agg-Krum-x", "title": "Cambiar FedAvg → Krum"}],
        "architecture": {"suggestions": [
            {"id": "find-ram_pressure-r1", "finding_ids": ["ram_pressure-1"],
             "title": "Reducir batch_size"},
        ]},
    }
    sids, fids = _suggestion_ids(state)
    assert sids == ["agg-Krum-x", "find-ram_pressure-r1"]
    assert fids == ["ram_pressure-1"]
    assert len(_suggestion_titles(state)) == 2
