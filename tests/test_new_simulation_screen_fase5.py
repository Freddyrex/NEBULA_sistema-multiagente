"""Fase 5 (Copilot): la pantalla de creación precarga el borrador.

Cubre la parte verificable en backend:
- `Scenario.__init__` acepta `derived_from`/`topology_hint` (el POST con ?draft
  no debe romper la creación).
- El `scenario.json` hijo conserva `adaptive_agent` + `parent_scenario` +
  `derived_from` (linaje) para que la nueva simulación siga con el agente y
  enlazada a la anterior.
- El borrador servido por `load_draft` trae lo que el formulario necesita
  (topology_hint, derived_from, adaptive_agent).
"""
import inspect
import os
import tempfile

from nebula.controller.scenarios import Scenario


def _scenario_kwargs(overrides=None):
    sig = inspect.signature(Scenario.__init__)
    kwargs = {}
    for name, p in sig.parameters.items():
        if name == "self":
            continue
        kwargs[name] = p.default if p.default is not inspect.Parameter.empty else None
    kwargs.update(overrides or {})
    return kwargs


def test_scenario_accepts_lineage_and_topology_hint():
    sc = Scenario.from_dict(_scenario_kwargs({
        "adaptive_agent": {"enabled": True},
        "parent_scenario": "nebula_DFL_x",
        "derived_from": {"scenario": "nebula_DFL_x", "round": 3, "changes": {}, "created_by": "op"},
        "topology_hint": {"type": "Star", "n_nodes": 3},
    }))
    assert sc.parent_scenario == "nebula_DFL_x"
    assert sc.adaptive_agent == {"enabled": True}


def test_child_scenario_json_keeps_adaptive_agent_and_lineage():
    from nebula.frontend.smac_chat import build_draft_scenario, load_draft, save_draft

    parent = {
        "scenario_title": "Base", "deployment": "docker", "federation": "DFL",
        "topology": "Ring", "rounds": 10, "dataset": "MNIST", "model": "MLP",
        "agg_algorithm": "FedAvg", "nodes": {"0": {"id": 0}}, "nodes_graph": [[0]],
        "adaptive_agent": {"enabled": True, "pause_on_suggestion": True},
    }
    draft = build_draft_scenario(parent, {"kind": "topology", "to_topology": "Star", "n_nodes": 3},
                                 parent_name="nebula_DFL_x", round_no=3, created_by="op")

    # Simulate what collectScenarioData() sends to /deployment/run (lineage spread).
    payload = dict(draft)
    payload["derived_from"] = draft["derived_from"]
    payload["parent_scenario"] = draft["parent_scenario"]

    # The controller's Scenario.from_dict must accept the payload keys.
    sc = Scenario.from_dict(_scenario_kwargs({
        "adaptive_agent": payload["adaptive_agent"],
        "parent_scenario": payload["parent_scenario"],
        "derived_from": payload["derived_from"],
        "topology_hint": payload.get("topology_hint"),
    }))
    assert sc.adaptive_agent.get("enabled") is True
    assert sc.parent_scenario == "nebula_DFL_x"

    # Round-trip through the draft store keeps the same lineage keys.
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "nebula_DFL_x"), exist_ok=True)
        did = save_draft(tmp, "nebula_DFL_x", draft, "op")
        status, served = load_draft(tmp, did, "op")
        assert status == 200
        assert served["adaptive_agent"]["enabled"] is True
        assert served["derived_from"]["scenario"] == "nebula_DFL_x"
        assert served["parent_scenario"] == "nebula_DFL_x"


def test_deployment_template_has_draft_banner():
    path = os.path.join(
        os.path.dirname(__file__), "..",
        "nebula", "frontend", "templates", "deployment.html")
    html = open(path, encoding="utf-8").read()
    assert 'id="smac-draft-banner"' in html
    assert 'id="smac-draft-discard"' in html
    assert 'id="smac-draft-stop-parent"' in html


def test_draft_loader_module_and_main_wiring():
    base = os.path.join(os.path.dirname(__file__), "..", "nebula", "frontend", "static", "js", "deployment")
    loader = open(os.path.join(base, "draft-loader.js"), encoding="utf-8").read()
    main = open(os.path.join(base, "main.js"), encoding="utf-8").read()
    scenario = open(os.path.join(base, "scenario.js"), encoding="utf-8").read()
    assert "fetch(\"/platform/smac/draft/" in loader
    assert "topology_hint" in loader
    assert "import DraftLoader" in main
    assert "DraftLoader.init()" in main
    # bug conocido: reputation puede faltar
    assert "scenario.reputation && scenario.reputation.enabled" in scenario
    # linaje sobrevive a collectScenarioData
    assert "currentLineage" in scenario
