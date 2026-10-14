"""Sprint 3 (ROADMAP_AGENTE_ADAPTATIVO): cambio dinámico de agregador.

Criterios:
- Ataque simulado (hallazgo bizantino) → sugiere Krum/Median con evidencia.
- Aceptar ejecuta el cambio (hot-swap verificado); rechazar no cambia nada y
  queda auditado (kind+params en el rechazo).
- Sin cambios dentro de la ventana de cooldown.
- La sugerencia viaja por /propose → confirmación → OperatorBus (existente);
  auto-ejecución nunca permitida aunque el flag exista.
"""
import asyncio
import inspect
import json
import os
import tempfile

from nebula.frontend.exec_interpreter import (
    adaptive_flags,
    auto_allowed,
    cooldown_state,
    current_aggregator,
    load_thresholds,
    read_audit_tail,
    suggest_aggregator,
)


def _finding(ftype, metric="accuracy", nodes=None, severity="warning", evidence=None, summary="s"):
    return {
        "id": f"{ftype}--{metric}", "type": ftype, "severity": severity,
        "metric": metric, "nodes": list(nodes or []),
        "evidence": list(evidence or []), "summary": summary,
    }


def _byz_evidence():
    return [{"metric": "accuracy", "round": 5, "node_id": 1, "value": 0.20,
             "detail": "last-round outlier, gap vs median -0.60"}]


def _audit_exec(algorithm="Krum", previous="FedAvg", round_=5, eid="aaa"):
    return {"ts": "2026-01-01T00:00:00+00:00", "user": "ADMIN", "event": "action_executed",
            "payload": {"id": eid, "kind": "select_aggregator",
                        "params": {"node": "all", "algorithm": algorithm,
                                   "trigger": "byzantine_suspected@accuracy", "previous": previous},
                        "round": round_, "ok": True, "detail": "hot-swapped"}}


def _audit_propose(eid, algorithm="Krum"):
    return {"ts": "2026-01-01T00:00:00+00:00", "user": "ADMIN", "event": "action_proposed",
            "payload": {"id": eid, "kind": "select_aggregator",
                        "params": {"node": "all", "algorithm": algorithm}, "round": 5}}


def _audit_reject(eid):
    return {"ts": "2026-01-01T00:00:01+00:00", "user": "ADMIN", "event": "action_rejected",
            "payload": {"id": eid}}


def test_byzantine_suggests_krum_with_evidence():
    findings = [_finding("byzantine_suspected", nodes=[1], evidence=_byz_evidence(), summary="outlier node 1")]
    sugg, cd = suggest_aggregator(findings, "FedAvg", rounds_completed=6, audit_records=[])
    assert sugg is not None and sugg["kind"] == "select_aggregator"
    assert sugg["params"]["algorithm"] == "Krum"
    assert sugg["params"]["node"] == "all"
    assert sugg["params"]["trigger"] == "byzantine_suspected@accuracy"
    assert sugg["params"]["previous"] == "FedAvg"
    assert sugg["title"] == "Cambiar FedAvg → Krum"
    assert sugg["evidence"] == _byz_evidence()
    assert sugg["requires_confirm"] is True and sugg["auto"] is False
    assert cd["in_cooldown"] is False


def test_straggler_suggests_trimmedmean_and_byzantine_wins():
    strag = [_finding("straggler", metric="round_time_s", nodes=[2], summary="slow node 2")]
    sugg, _ = suggest_aggregator(strag, "FedAvg", rounds_completed=6, audit_records=[])
    assert sugg["params"]["algorithm"] == "TrimmedMean"
    both = strag + [_finding("byzantine_suspected", nodes=[1], evidence=_byz_evidence())]
    sugg2, _ = suggest_aggregator(both, "FedAvg", rounds_completed=6, audit_records=[])
    assert sugg2["params"]["algorithm"] == "Krum"  # prioridad bizantina


def test_clean_conditions_return_to_fedavg_and_noop_when_on_target():
    sugg, _ = suggest_aggregator([], "Krum", rounds_completed=8, audit_records=[])
    assert sugg is not None and sugg["params"]["algorithm"] == "FedAvg"
    assert sugg["params"]["trigger"] == "clean_conditions@accuracy"
    # ya en el objetivo: sin sugerencia
    assert suggest_aggregator([], "FedAvg", rounds_completed=8, audit_records=[])[0] is None
    byz = [_finding("byzantine_suspected", nodes=[1], evidence=_byz_evidence())]
    assert suggest_aggregator(byz, "Krum", rounds_completed=6, audit_records=[])[0] is None
    # adverso leve (low_recall) bloquea el retorno a FedAvg pero no sugiere robusto
    low = [_finding("low_recall", metric="recall_macro", summary="recall 0.4")]
    assert suggest_aggregator(low, "Krum", rounds_completed=6, audit_records=[])[0] is None


def test_finished_scenario_suppresses_hot_swap():
    byz = [_finding("byzantine_suspected", nodes=[1], evidence=_byz_evidence())]
    sugg, _ = suggest_aggregator(byz, "FedAvg", rounds_completed=10, audit_records=[],
                                 scenario_completed=True)
    assert sugg is None
    sugg2, _ = suggest_aggregator(byz, "FedAvg", rounds_completed=10, audit_records=[],
                                  scenario_completed=False)
    assert sugg2 is not None and sugg2["params"]["algorithm"] == "Krum"


def test_unknown_current_or_target_never_suggests():
    byz = [_finding("byzantine_suspected", nodes=[1], evidence=_byz_evidence())]
    assert suggest_aggregator(byz, None, rounds_completed=6, audit_records=[])[0] is None
    assert suggest_aggregator(byz, "???", rounds_completed=6, audit_records=[])[0] is None
    th = dict(load_thresholds(), aggregator_policy={"byzantine_target": "Nope"})
    assert suggest_aggregator(byz, "FedAvg", rounds_completed=6, audit_records=[], thresholds=th)[0] is None


def test_cooldown_suppresses_and_expires():
    exec5 = _audit_exec(round_=5)
    sugg, cd = suggest_aggregator(
        [_finding("byzantine_suspected", nodes=[1], evidence=_byz_evidence())],
        "FedAvg", rounds_completed=6, audit_records=[exec5])
    assert sugg is None and cd["in_cooldown"] is True and cd["remaining_rounds"] == 2
    assert cd["last_change_algorithm"] == "Krum" and cd["last_change_round"] == 5
    # fuera de la ventana (cooldown 3): se vuelve a sugerir
    sugg2, cd2 = suggest_aggregator(
        [_finding("byzantine_suspected", nodes=[1], evidence=_byz_evidence())],
        "FedAvg", rounds_completed=9, audit_records=[exec5])
    assert sugg2 is not None and cd2["in_cooldown"] is False


def test_rejection_suppresses_same_target():
    audit = [_audit_propose("p1", "Krum"), _audit_reject("p1")]
    sugg, cd = suggest_aggregator(
        [_finding("byzantine_suspected", nodes=[1], evidence=_byz_evidence())],
        "FedAvg", rounds_completed=6, audit_records=audit)
    assert sugg is None and cd["last_rejected_algorithm"] == "Krum"


def test_current_aggregator_declared_vs_audit():
    assert current_aggregator("FedAvg", []) == ("FedAvg", "declared")
    assert current_aggregator("FedAvg", [_audit_exec("Krum")]) == ("Krum", "audit")
    assert current_aggregator(None, []) == (None, "unknown")


def test_flags_default_off_and_auto_never_allowed():
    with tempfile.TemporaryDirectory() as tmp:
        sc = os.path.join(tmp, "demo")
        os.makedirs(sc)
        assert adaptive_flags(tmp, "demo") == {
            "exec_interpreter": False, "aggregator_suggestions": False,
            "architecture_suggestions": False, "aggregator_auto": False}
        with open(os.path.join(sc, "scenario.json"), "w") as f:
            json.dump({"adaptive_agent": {"exec_interpreter": True,
                                          "aggregator_suggestions": True,
                                          "aggregator_auto": True}}, f)
        flags = adaptive_flags(tmp, "demo")
        assert flags["aggregator_suggestions"] is True
        assert auto_allowed(flags) is False  # reservado Sprint 7: nunca auto-ejecuta
        assert auto_allowed(None) is False


def test_read_audit_tail_never_raises():
    assert read_audit_tail(None, "x") == []
    assert read_audit_tail("/no/existe", "x") == []
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "s"))
        with open(os.path.join(tmp, "s", "smac_audit.jsonl"), "w") as f:
            f.write('{"event": "action_executed", "payload": {}}\nnot-json\n')
        recs = read_audit_tail(tmp, "s")
        assert len(recs) == 1 and recs[0]["event"] == "action_executed"


def test_operatorbus_hot_swap_uses_new_algorithm():
    """Aceptar → OperatorBus aplica hot-swap (el siguiente ciclo agrega con el nuevo)."""
    from nebula.core import operatorbus as ob
    from nebula.core.aggregation import aggregator as agg_mod

    calls = {}

    class FakeAgg:
        async def init(self):
            calls["init"] = True

    def fake_create(config=None, engine=None):
        calls["algorithm"] = config.participant["aggregator_args"]["algorithm"]
        return FakeAgg()

    class FakeConfig:
        participant = {"aggregator_args": {"algorithm": "FedAvg"}}

    class FakeEngine:
        config = FakeConfig()
        _aggregator = "OLD"

    bus = ob.OperatorBus.__new__(ob.OperatorBus)
    bus._engine = FakeEngine()
    real = agg_mod.create_aggregator
    agg_mod.create_aggregator = fake_create
    try:
        status, detail = asyncio.run(bus._do_select_aggregator({"algorithm": "Krum"}))
    finally:
        agg_mod.create_aggregator = real
    assert status == "ok" and "FedAvg -> Krum" in detail
    assert bus._engine.config.participant["aggregator_args"]["algorithm"] == "Krum"
    assert isinstance(bus._engine._aggregator, FakeAgg) and calls.get("init") is True
    assert calls.get("algorithm") == "Krum"


def test_propose_and_reject_audit_trail():
    """Proponer audita kind+params+ronda; rechazar no cambia nada y audita kind+params."""
    from nebula.frontend import smac_chat
    from nebula.frontend.smac_api import SmacDeps, handle_confirm, handle_propose

    smac_chat._pending_actions.clear()
    with tempfile.TemporaryDirectory() as tmp:
        deps = SmacDeps(config_dir=tmp, log_dir=tmp, databases_dir=None,
                        controller_host="h", controller_port=1, controller_name="c", api_key="")
        session = {"user": "tester", "role": "admin"}
        status, rec = handle_propose(
            deps, session, "demo", "select_aggregator",
            {"node": "all", "algorithm": "Krum",
             "trigger": "byzantine_suspected@accuracy", "previous": "FedAvg"},
            "Cambiar FedAvg → Krum")
        assert status == 200
        status2, out = asyncio.run(handle_confirm(deps, session, rec["id"], False))
        assert status2 == 200 and out["ok"] is False
        lines = [json.loads(l) for l in
                 open(os.path.join(tmp, "demo", "smac_audit.jsonl"))]
        by_event = {l["event"]: l["payload"] for l in lines}
        assert by_event["action_proposed"]["kind"] == "select_aggregator"
        assert by_event["action_proposed"]["params"]["trigger"] == "byzantine_suspected@accuracy"
        assert by_event["action_proposed"]["params"]["previous"] == "FedAvg"
        assert by_event["action_rejected"]["kind"] == "select_aggregator"
        assert by_event["action_rejected"]["params"]["algorithm"] == "Krum"
    smac_chat._pending_actions.clear()


def test_scenario_accepts_adaptive_agent_flag():
    """La creación con adaptive_agent ya no rompe Scenario.from_dict."""
    from nebula.controller.scenarios import Scenario

    sig = inspect.signature(Scenario.__init__)
    kwargs = {}
    for name, p in sig.parameters.items():
        if name == "self":
            continue
        kwargs[name] = {"adaptive_agent": {"exec_interpreter": True}}.get(
            name, p.default if p.default is not inspect.Parameter.empty else None)
    sc = Scenario.from_dict(kwargs)
    assert sc.adaptive_agent == {"exec_interpreter": True}


def test_prompt_and_fallback_cover_suggestions():
    from nebula.frontend.smac_chat import CHAT_SYSTEM_PROMPT, deterministic_reply

    assert "suggestions" in CHAT_SYSTEM_PROMPT and "never claim" in CHAT_SYSTEM_PROMPT
    state = {
        "scenario": "demo", "status": "running", "round": 6, "nodes": [],
        "config": {"aggregation": "FedAvg"},
        "metrics_summary": {"metrics": {}, "available": [], "warnings": []},
        "findings": [],
        "suggestions": [{"id": "agg-Krum-x", "kind": "select_aggregator",
                         "title": "Cambiar FedAvg → Krum", "reason": "outlier node 1"}],
    }
    assert "Cambiar FedAvg → Krum" in deterministic_reply(state, "")["reply"]
