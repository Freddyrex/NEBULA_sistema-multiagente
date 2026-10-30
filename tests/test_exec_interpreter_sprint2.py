"""Sprint 2 (ROADMAP_AGENTE_ADAPTATIVO): intérprete de ejecución.

Criterios:
- Con datos sintéticos, cada regla dispara y también NO dispara.
- Cada hallazgo trae `evidence` verificable contra las series.
- Sin LLM todo funciona (determinista); el panel muestra hallazgos sin LLM.
"""
import json
import os
import tempfile

from nebula.frontend.exec_interpreter import (
    FINDING_TYPES,
    analyze_digest,
    interpreter_enabled,
    load_thresholds,
)


def _mentry(first, final, best_round, best_value, rounds=6, converged_at=None,
            outliers=None, unit="ratio"):
    return {
        "first": first, "final": final,
        "delta": round(final - first, 4),
        "best": {"round": best_round, "value": best_value},
        "converged_at": converged_at,
        "rounds_with_data": rounds,
        "unit": unit,
        "ranking_final": [],
        "outliers": outliers or [],
        "outlier_gap": 0.05,
    }


def _healthy_digest():
    return {
        "schema_version": "1.1",
        "scenario": {"id": "s", "rounds_total": 10, "rounds_completed": 6},
        "metrics": {
            "accuracy": _mentry(0.50, 0.80, 5, 0.80),
            "f1_macro": _mentry(0.48, 0.78, 5, 0.78),
            "recall_macro": _mentry(0.47, 0.75, 5, 0.75),
            "precision_macro": _mentry(0.49, 0.77, 5, 0.77),
            "cpu_percent": _mentry(30.0, 40.0, 5, 40.0, unit="%"),
            "ram_percent": _mentry(50.0, 55.0, 5, 55.0, unit="%"),
            "cpu_temp": _mentry(50.0, 60.0, 5, 60.0, unit="°C"),
            "round_time_s": _mentry(10.0, 11.0, 5, 11.0, unit="s"),
        },
        "nodes": {"stragglers": []},
        "events": {},
        "warnings": [],
    }


def _healthy_series():
    return {
        "accuracy": [(r, 0.50 + 0.06 * r) for r in range(6)],
        "f1_macro": [(r, 0.48 + 0.06 * r) for r in range(6)],
        "recall_macro": [(r, 0.47 + 0.056 * r) for r in range(6)],
        "cpu_percent": [(r, 30.0 + 2.0 * r) for r in range(6)],
        "ram_percent": [(r, 50.0 + 1.0 * r) for r in range(6)],
        "cpu_temp": [(r, 50.0 + 2.0 * r) for r in range(6)],
        "round_time_s": [(r, 10.0 + 0.2 * r) for r in range(6)],
    }


def _types(findings):
    return [f["type"] for f in findings]


def test_finding_types_contract():
    assert set(FINDING_TYPES) >= {
        "accuracy_plateau", "f1_recall_gap", "low_recall", "ram_pressure",
        "cpu_pressure", "thermal_throttle_risk", "straggler",
        "byzantine_suspected", "over_provisioned_rounds",
    }


def test_healthy_data_produces_no_findings():
    assert analyze_digest(_healthy_digest(), _healthy_series()) == []


def test_plateau_fires_and_not_fires():
    d = _healthy_digest()
    d["metrics"]["accuracy"] = _mentry(0.60, 0.802, 5, 0.802, converged_at=3)
    series = dict(_healthy_series())
    series["accuracy"] = [(0, 0.60), (1, 0.70), (2, 0.78), (3, 0.800), (4, 0.801), (5, 0.802)]
    found = analyze_digest(d, series)
    plat = [f for f in found if f["type"] == "accuracy_plateau"]
    assert len(plat) == 1
    assert plat[0]["severity"] == "info" and plat[0]["metric"] == "accuracy"
    # evidence verificable contra la serie de entrada
    ev_rounds = {e["round"]: e["value"] for e in plat[0]["evidence"] if e["detail"] == "global mean"}
    assert ev_rounds == {3: 0.800, 4: 0.801, 5: 0.802}
    # sin plateau (subida fuerte) no dispara
    assert "accuracy_plateau" not in _types(analyze_digest(_healthy_digest(), _healthy_series()))


def test_f1_gap_fires_and_not_fires():
    d = _healthy_digest()
    d["metrics"]["f1_macro"] = _mentry(0.40, 0.55, 5, 0.55)  # gap 0.25 > 0.10
    found = analyze_digest(d, _healthy_series())
    gap = [f for f in found if f["type"] == "f1_recall_gap"]
    assert len(gap) == 1 and gap[0]["severity"] == "warning"
    vals = {e["metric"]: e["value"] for e in gap[0]["evidence"]}
    assert vals == {"accuracy": 0.80, "f1_macro": 0.55}
    assert "f1_recall_gap" not in _types(analyze_digest(_healthy_digest(), _healthy_series()))


def test_low_recall_fires_and_not_fires():
    d = _healthy_digest()
    d["metrics"]["recall_macro"] = _mentry(0.50, 0.45, 0, 0.50)
    found = analyze_digest(d, _healthy_series())
    low = [f for f in found if f["type"] == "low_recall"]
    assert len(low) == 1 and low[0]["evidence"][0]["value"] == 0.45
    assert "low_recall" not in _types(analyze_digest(_healthy_digest(), _healthy_series()))


def test_resource_pressure_fires_and_not_fires():
    d = _healthy_digest()
    d["metrics"]["ram_percent"] = _mentry(50.0, 90.0, 5, 90.0, unit="%")
    d["metrics"]["cpu_percent"] = _mentry(30.0, 97.0, 5, 97.0, unit="%")
    series = dict(_healthy_series())
    series["ram_percent"] = [(r, 50.0) for r in range(3)] + [(3, 88.0), (4, 89.0), (5, 90.0)]
    series["cpu_percent"] = [(r, 30.0) for r in range(3)] + [(3, 96.0), (4, 97.0), (5, 98.0)]
    found = analyze_digest(d, series)
    ram = [f for f in found if f["type"] == "ram_pressure"]
    cpu = [f for f in found if f["type"] == "cpu_pressure"]
    assert len(ram) == 1 and ram[0]["severity"] == "warning"
    assert len(cpu) == 1 and cpu[0]["severity"] == "critical"
    assert [e["round"] for e in ram[0]["evidence"]] == [3, 4, 5]
    # sano no dispara
    assert "ram_pressure" not in _types(analyze_digest(_healthy_digest(), _healthy_series()))
    # sin historial suficiente se omite (no se inventa)
    assert "ram_pressure" not in _types(analyze_digest(d, {"ram_percent": [(5, 99.0)]}))


def test_thermal_sensor_critical_and_estimate_labeled():
    d = _healthy_digest()
    d["metrics"]["cpu_temp"] = _mentry(50.0, 92.0, 5, 92.0, unit="°C")
    found = analyze_digest(d, _healthy_series())
    th = [f for f in found if f["type"] == "thermal_throttle_risk"]
    assert len(th) == 1 and th[0]["severity"] == "critical" and th[0]["metric"] == "cpu_temp"
    assert "sensor" in th[0]["evidence"][0]["detail"]

    # sin sensor: el estimado se etiqueta, nunca pasa por medida
    d2 = _healthy_digest()
    del d2["metrics"]["cpu_temp"]
    d2["metrics"]["cpu_temp_est"] = _mentry(45.0, 88.0, 5, 88.0, unit="°C")
    found2 = analyze_digest(d2, _healthy_series())
    th2 = [f for f in found2 if f["type"] == "thermal_throttle_risk"]
    assert len(th2) == 1 and th2[0]["metric"] == "cpu_temp_est"
    assert "estimated" in th2[0]["evidence"][0]["detail"]

    # sano no dispara
    assert "thermal_throttle_risk" not in _types(analyze_digest(_healthy_digest(), _healthy_series()))


def test_straggler_fires_and_ratio_gate():
    d = _healthy_digest()
    d["nodes"] = {"stragglers": [
        {"node_id": 2, "mean_round_time_s": 30.0, "ratio_vs_median": 2.0},
        {"node_id": 1, "mean_round_time_s": 16.0, "ratio_vs_median": 1.1},
    ]}
    found = analyze_digest(d, _healthy_series())
    st = [f for f in found if f["type"] == "straggler"]
    assert len(st) == 1 and st[0]["nodes"] == [2]  # ratio 1.1 < 1.5 queda fuera
    assert st[0]["evidence"][0]["node_id"] == 2
    # sano no dispara
    assert "straggler" not in _types(analyze_digest(_healthy_digest(), _healthy_series()))


def test_byzantine_fires_and_not_fires():
    d = _healthy_digest()
    d["metrics"]["accuracy"]["outliers"] = [
        {"node_id": 1, "value": 0.20, "gap_vs_median": -0.60}
    ]
    found = analyze_digest(d, _healthy_series())
    bz = [f for f in found if f["type"] == "byzantine_suspected"]
    assert len(bz) == 1 and bz[0]["nodes"] == [1] and bz[0]["severity"] == "warning"
    assert bz[0]["evidence"][0]["value"] == 0.20
    assert "byzantine_suspected" not in _types(analyze_digest(_healthy_digest(), _healthy_series()))


def test_overprovisioned_fires_and_not_fires():
    d = _healthy_digest()
    d["metrics"]["accuracy"] = _mentry(0.60, 0.802, 5, 0.802, converged_at=2)
    d["scenario"] = {"id": "s", "rounds_total": 10, "rounds_completed": 6}
    found = analyze_digest(d, _healthy_series())
    op = [f for f in found if f["type"] == "over_provisioned_rounds"]
    assert len(op) == 1 and op[0]["severity"] == "info"
    # 1 ronda extra no basta (extra_need=3)
    d["scenario"] = {"id": "s", "rounds_total": 10, "rounds_completed": 4}
    assert "over_provisioned_rounds" not in _types(analyze_digest(d, _healthy_series()))


def test_insufficient_rounds_skips_history_rules_only():
    d = _healthy_digest()
    for m in d["metrics"].values():
        m["rounds_with_data"] = 2
    d["metrics"]["recall_macro"] = _mentry(0.50, 0.40, 1, 0.50, rounds=2)
    series2 = {k: v[:2] for k, v in _healthy_series().items()}
    found = analyze_digest(d, series2)
    types = _types(found)
    assert "accuracy_plateau" not in types
    assert "ram_pressure" not in types and "cpu_pressure" not in types
    assert "over_provisioned_rounds" not in types
    assert "low_recall" in types  # regla puntual sí dispara


def test_empty_or_invalid_digest_never_raises():
    assert analyze_digest(None) == []
    assert analyze_digest({}) == []
    assert analyze_digest({"metrics": None, "nodes": None}) == []


def test_thresholds_yaml_and_override():
    th = load_thresholds()
    assert th["plateau_k"] == 3 and th["min_recall"] == 0.60
    d = _healthy_digest()
    d["metrics"]["f1_macro"] = _mentry(0.40, 0.55, 5, 0.55)
    assert "f1_recall_gap" in _types(analyze_digest(d, _healthy_series()))
    # umbral permisivo apaga la regla (configurable, no hardcodeado)
    th2 = dict(th, f1_accuracy_gap=0.99)
    assert "f1_recall_gap" not in _types(analyze_digest(d, _healthy_series(), th2))


def test_interpreter_flag_off_by_default():
    with tempfile.TemporaryDirectory() as tmp:
        sc = os.path.join(tmp, "demo")
        os.makedirs(sc)
        # sin scenario.json → apagado
        assert interpreter_enabled(tmp, "demo") is False
        with open(os.path.join(sc, "scenario.json"), "w") as f:
            json.dump({"rounds": 5}, f)
        assert interpreter_enabled(tmp, "demo") is False  # sin clave → apagado
        with open(os.path.join(sc, "scenario.json"), "w") as f:
            json.dump({"adaptive_agent": {"exec_interpreter": True}}, f)
        assert interpreter_enabled(tmp, "demo") is True
        assert interpreter_enabled(None, "demo") is False


def test_prompt_instructs_findings_only():
    from nebula.frontend.smac_chat import ANALYZE_SYSTEM_PROMPT, CHAT_SYSTEM_PROMPT

    assert "findings" in CHAT_SYSTEM_PROMPT and "do not invent other" in CHAT_SYSTEM_PROMPT
    assert "findings" in ANALYZE_SYSTEM_PROMPT


def test_deterministic_reply_lists_findings_without_llm():
    from nebula.frontend.smac_chat import deterministic_reply

    state = {
        "scenario": "demo", "status": "running", "round": 6, "nodes": [],
        "config": {"aggregation": "FedAvg"},
        "metrics_summary": {"metrics": {}, "available": [], "warnings": []},
        "findings": [
            {"id": "byzantine_suspected--accuracy", "type": "byzantine_suspected",
             "severity": "warning", "metric": "accuracy", "nodes": [1],
             "evidence": [], "summary": "byzantine suspected on accuracy"},
        ],
    }
    reply = deterministic_reply(state, "")["reply"]
    assert "byzantine_suspected" in reply and "[warning]" in reply
