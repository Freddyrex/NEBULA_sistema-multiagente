"""Sprint 7 (ROADMAP_AGENTE_ADAPTATIVO): integración, seguridad y evaluación.

Criterios:
- Caída de Groq no detiene el escenario ni los botones manuales.
- Toda acción ejecutada se audita con costo proyectado y resultado.
- Informe final generado automáticamente.
- E2E: hallazgo → sugerencia (con costo) → aceptar → ejecutar → verificar.
"""
import asyncio
import json
import os
import tempfile

from nebula.frontend.exec_interpreter import due_verifications, verify_execution
from nebula.frontend.smac_chat import (
    ACTION_KINDS,
    CHAT_SYSTEM_PROMPT,
    can_execute_kind,
    deterministic_reply,
)


def _admin():
    return {"user": "op", "role": "admin"}


def _viewer():
    return {"user": "v", "role": "guest"}


def test_rbac_matrix_all_kinds():
    for kind in ("pause_node", "resume_node", "select_aggregator", "propose_disconnect",
                 "propose_architecture_change", "resume_from"):
        assert can_execute_kind("admin", kind) is True
        assert can_execute_kind("operator", kind) is True
        assert can_execute_kind("viewer", kind) is False
    assert can_execute_kind("operator", "stop_scenario") is False
    assert "resume_from" in ACTION_KINDS and "propose_architecture_change" in ACTION_KINDS


def test_viewer_is_read_only_on_propose_and_confirm():
    from nebula.frontend import smac_chat
    from nebula.frontend.smac_api import SmacDeps, handle_confirm, handle_propose

    smac_chat._pending_actions.clear()
    with tempfile.TemporaryDirectory() as tmp:
        deps = SmacDeps(config_dir=tmp, log_dir=tmp, databases_dir=None,
                        controller_host="h", controller_port=1, controller_name="c", api_key="")
        s, _ = handle_propose(deps, _viewer(), "demo", "select_aggregator",
                              {"node": "all", "algorithm": "Krum"}, "x")
        assert s == 403
        s2, rec = handle_propose(deps, _admin(), "demo", "select_aggregator",
                                 {"node": "all", "algorithm": "Krum"}, "x")
        assert s2 == 200
        s3, _ = asyncio.run(handle_confirm(deps, _viewer(), rec["id"], True))
        assert s3 == 403
    smac_chat._pending_actions.clear()


def test_groq_down_degrades_but_manual_flow_survives():
    from nebula.frontend import smac_chat
    from nebula.frontend.smac_api import SmacDeps, handle_ask, handle_propose

    smac_chat._pending_actions.clear()
    smac_chat._RATE_BUCKETS.clear()
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "demo"))
        deps = SmacDeps(config_dir=tmp, log_dir=tmp, databases_dir=None,
                        controller_host="h", controller_port=1, controller_name="c", api_key="")
        status, out = asyncio.run(handle_ask(deps, _admin(), "¿cómo va?", "demo"))
        assert status == 200 and out.get("degraded") is True
        assert "deterministic" in out.get("reply", "").lower() or "determinista" in out.get("reply", "")
        # botones manuales: proponer sin Groq funciona igual
        smac_chat._RATE_BUCKETS.clear()
        s2, rec = handle_propose(deps, _admin(), "demo", "select_aggregator",
                                 {"node": "all", "algorithm": "Krum"}, "manual sin LLM")
        assert s2 == 200 and rec["id"]
    smac_chat._pending_actions.clear()


def _exec_payload(eid="e1", kind="select_aggregator", round_=5, ok=True, params=None, baseline=None):
    p = {"id": eid, "kind": kind, "round": round_, "ok": ok,
         "params": params or {}, "detail": "hot-swapped FedAvg -> Krum"}
    if baseline is not None:
        p["baseline"] = baseline
    return p


def test_due_verifications_pending_and_ready():
    audit = [{"event": "action_proposed",
              "payload": {"id": "e1", "kind": "select_aggregator", "params": {}}},
             {"event": "action_executed", "payload": _exec_payload()}]
    pend = due_verifications(audit, 6)
    assert len(pend) == 1 and pend[0]["status"] == "pending" and pend[0]["due_round"] == 8
    ready = due_verifications(audit, 9)
    assert ready[0]["status"] == "ready"
    assert due_verifications(audit, None)[0]["status"] == "pending"
    assert due_verifications([], 9) == []


def test_verify_execution_bands():
    base = _exec_payload(baseline={"accuracy": 0.70})
    cost = {"benefit": {"low": 0.01, "mid": 0.05, "high": 0.09, "basis": "measured"}}
    within = verify_execution(base, cost, {"accuracy_final": 0.75, "rounds_completed": 9})
    assert within["within_range"] is True and "dentro" in within["calibration"]
    assert within["measured"]["actual_gain"] == 0.05
    below = verify_execution(base, cost, {"accuracy_final": 0.70, "rounds_completed": 9})
    assert below["within_range"] is False and "sobreestimado" in below["calibration"]
    above = verify_execution(base, cost, {"accuracy_final": 0.95, "rounds_completed": 9})
    assert above["within_range"] is False and "subestimado" in above["calibration"]
    nobase = verify_execution(_exec_payload(), cost, {"accuracy_final": 0.9, "rounds_completed": 9})
    assert nobase["within_range"] is None and "baseline" in nobase["calibration"]


def test_executed_audit_carries_cost_and_result():
    from nebula.frontend import smac_chat
    from nebula.frontend.smac_api import SmacDeps, _execute_record

    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "demo"))
        deps = SmacDeps(config_dir=tmp, log_dir=tmp, databases_dir=None,
                        controller_host="h", controller_port=1, controller_name="c", api_key="")

        async def fake_exec(record, *a):
            return {"ok": True, "detail": "fake swap ok"}

        real = smac_chat.execute_action
        smac_chat.execute_action = fake_exec
        try:
            cost = {"compute": {}, "resources": {}, "network": {}, "transition": {},
                    "benefit": {"low": 0.0, "mid": 0.05, "high": 0.1}}
            out = asyncio.run(_execute_record(
                deps, {"id": "x1", "kind": "propose_architecture_change",
                       "params": {"change": {"kind": "rounds", "total_rounds": 4}, "cost": cost},
                       "scenario": "demo", "username": "op"}))
        finally:
            smac_chat.execute_action = real
        assert out["ok"] is True
        lines = [json.loads(l) for l in open(os.path.join(tmp, "demo", "smac_audit.jsonl"))]
        exe = [l for l in lines if l["event"] == "action_executed"][0]["payload"]
        assert exe["projected_cost"]["benefit"]["mid"] == 0.05
        assert exe["ok"] is True and "baseline" in exe


def _plateau_digest():
    return {
        "schema_version": "1.1",
        "scenario": {"id": "dfl3", "status": "running", "rounds_total": 10, "rounds_completed": 6,
                     "aggregator": "FedAvg", "dataset": "MNIST", "model": "MLP"},
        "metrics": {
            "accuracy": {"first": 0.6, "final": 0.802, "delta": 0.202,
                         "best": {"round": 5, "value": 0.802}, "converged_at": 3,
                         "rounds_with_data": 6, "unit": "ratio", "ranking_final": [],
                         "outliers": [], "outlier_gap": 0.05},
            "round_time_s": {"first": 20.0, "final": 20.0, "delta": 0.0,
                             "best": {"round": 0, "value": 20.0}, "converged_at": None,
                             "rounds_with_data": 6, "unit": "s", "ranking_final": [],
                             "outliers": [], "outlier_gap": 0.05},
        },
        "nodes": {}, "events": {}, "warnings": [],
    }


def test_e2e_finding_to_verified_result():
    """Cadena unificada con 3 nodos: plateau → recorte con costo → aceptar →
    ejecutar → verificación a +K con banda cumplida."""
    from nebula.frontend import smac_chat
    from nebula.frontend.cost_model import decide, estimate_cost
    from nebula.frontend.exec_interpreter import analyze_digest, suggest_architecture
    from nebula.frontend.smac_api import SmacDeps, handle_confirm, handle_propose

    smac_chat._pending_actions.clear()
    smac_chat._RATE_BUCKETS.clear()
    digest = _plateau_digest()
    series = {"accuracy": [(r, v) for r, v in enumerate([0.60, 0.70, 0.78, 0.800, 0.801, 0.802])],
              "loss": [(r, 0.40) for r in range(6)]}
    findings = analyze_digest(digest, series)
    assert any(f["type"] == "accuracy_plateau" for f in findings)

    measured = {"round_time_s": {"final": 20.0}, "rounds_completed": 6,
                "accuracy": {"final": 0.802}}
    declared = {"total_rounds": 10, "epochs": 1, "model": "MLP",
                "dataset": "MNIST", "n_nodes": 3, "avg_neighbors": 2.0}
    sugg, _ = suggest_architecture(findings, series, measured, declared)
    assert len(sugg) == 1 and sugg[0]["params"]["change"]["total_rounds"] == 4
    assert sugg[0]["params"]["cost"]["compute"]["value"] < 0  # ahorro
    assert decide(sugg[0]["params"]["cost"], None)["worthwhile"] is True

    with tempfile.TemporaryDirectory() as tmp:
        deps = SmacDeps(config_dir=tmp, log_dir=tmp, databases_dir=None,
                        controller_host="h", controller_port=1, controller_name="c", api_key="")

        async def fake_exec(record, *a):
            ch = record["params"]["change"]
            return {"ok": True, "detail": f"total_rounds -> {ch['total_rounds']} (3 nodos)"}

        real = smac_chat.execute_action
        smac_chat.execute_action = fake_exec
        try:
            s = sugg[0]
            st, rec = handle_propose(deps, _admin(), "dfl3", "propose_architecture_change",
                                     {"change": s["params"]["change"], "cost": s["params"]["cost"],
                                      "trigger": "accuracy_plateau@accuracy", "previous": 10},
                                     s["title"])
            assert st == 200
            st2, out = asyncio.run(handle_confirm(deps, _admin(), rec["id"], True))
            assert st2 == 200 and out["ok"] is True
        finally:
            smac_chat.execute_action = real
        lines = [json.loads(l) for l in open(os.path.join(tmp, "dfl3", "smac_audit.jsonl"))]
        exe = [l for l in lines if l["event"] == "action_executed"][0]["payload"]
        assert exe["params"]["trigger"] == "accuracy_plateau@accuracy"
        assert exe["projected_cost"]["compute"]["value"] < 0

        # verificación a +K con banda cumplida (baseline registrada a mano como
        # la registraría _execute_record con digest disponible)
        audit = [{"event": "action_proposed",
                  "payload": {"id": "v1", "kind": "propose_architecture_change",
                              "params": {"cost": {"benefit": {"low": 0.0, "mid": 0.0,
                                                              "high": 0.0}}}}},
                 {"event": "action_executed",
                  "payload": {"id": "v1", "kind": "propose_architecture_change", "round": 6,
                              "ok": True, "baseline": {"accuracy": 0.802},
                              "params": {"cost": {"benefit": {"low": 0.0, "mid": 0.0,
                                                              "high": 0.0}}}}}]
        dues = due_verifications(audit, 9)
        assert dues[0]["status"] == "ready"
        rep = verify_execution(audit[1]["payload"], audit[1]["payload"]["params"]["cost"],
                               {"accuracy_final": 0.802, "rounds_completed": 9})
        assert rep["within_range"] is True
    smac_chat._pending_actions.clear()


def test_final_report_auto_generated():
    from nebula.frontend.metrics.service import MetricsService, render_final_report

    digest = _plateau_digest()
    digest["scenario"]["status"] = "completed"
    digest["findings"] = [{"type": "accuracy_plateau", "severity": "info", "summary": "plateau"}]
    audit = [{"event": "action_proposed",
              "payload": {"id": "e1", "kind": "propose_architecture_change",
                          "params": {"cost": {"benefit": {"low": 0.0, "mid": 0.0, "high": 0.0}}}}},
             {"event": "action_executed",
              "payload": {"id": "e1", "kind": "propose_architecture_change", "round": 6,
                          "ok": True, "detail": "total_rounds -> 4",
                          "params": {"cost": {"benefit": {"low": 0.0, "mid": 0.0, "high": 0.0}}}}}]
    md = render_final_report("dfl3", digest, audit, {"pending": [], "completed": []})
    assert "# NEBULA final report" in md and "action_executed" in md and "accuracy_plateau" in md

    with tempfile.TemporaryDirectory() as tmp:
        logs = os.path.join(tmp, "logs", "dfl3")
        os.makedirs(logs)
        with open(os.path.join(logs, "smac_audit.jsonl"), "w") as f:
            for rec in audit:
                f.write(json.dumps({"ts": "2026-01-01T00:00:00+00:00", "user": "op",
                                    "event": rec["event"], "payload": rec["payload"]}) + "\n")
        svc = MetricsService(log_dir=os.path.join(tmp, "logs"), databases_dir=None)
        path = svc.ensure_final_report("dfl3", digest["scenario"], digest, 123.0)
        assert path and os.path.isfile(path)
        assert "proyectado" in open(path).read()
        assert svc.ensure_final_report("dfl3", digest["scenario"], digest, 100.0) == path  # fresco
        digest["scenario"]["status"] = "running"
        assert svc.ensure_final_report("dfl3", digest["scenario"], digest, 200.0) is None


def test_prompts_and_fallback_cover_verifications_and_docs():
    assert "verifications" in CHAT_SYSTEM_PROMPT
    state = {"scenario": "d", "status": "running", "round": 9, "nodes": [],
             "config": {}, "metrics_summary": {"metrics": {}, "available": [], "warnings": []},
             "verifications": {"pending": [{"kind": "select_aggregator", "due_round": 8}],
                               "completed": [{"kind": "select_aggregator",
                                              "calibration": "ganancia 0.05 dentro de la banda"}]}}
    reply = deterministic_reply(state, "")["reply"]
    assert "dentro de la banda" in reply and "round 8" in reply
    with open("SMAC_comunicacion_y_nueva_API.md") as f:
        doc = f.read()
    assert "PARTE 3" in doc and "propose_architecture_change" in doc
