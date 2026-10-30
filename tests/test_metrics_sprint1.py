"""Sprint 1 (ROADMAP_AGENTE_ADAPTATIVO): métricas completas.

Criterios:
- 3 nodos x 5 rondas producen series de las 6 métricas por nodo.
- El digest incluye las 6 con first/final/delta/best/converged_at.
- Sin sensor de temperatura: cpu_temp ausente (unavailable) y cpu_temp_est
  etiquetado como estimado (warnings), nunca un valor inventado sin etiqueta.
- Sin LLM: todo es determinista.
"""
import asyncio
from types import SimpleNamespace

import pandas as pd

from nebula.config.defaults import METRICS_SCHEMA_VERSION
from nebula.frontend.metrics.digest import build_digest
from nebula.frontend.metrics.providers import METRIC_META, TAG_MAP, TensorBoardProvider
from nebula.frontend.metrics.service import ROUND_AGG


def _rows_3x5():
    """3 nodos x 5 rondas para las 6 métricas Sprint 1."""
    rows = []
    for node in (0, 1, 2):
        for r in range(5):
            rows.append({"metric": "f1_macro", "scope": "node", "node_id": node, "round": r,
                         "value": 0.50 + 0.05 * r + 0.01 * node, "step": r, "ts": "2026-01-01T00:00:00Z"})
            rows.append({"metric": "recall_macro", "scope": "node", "node_id": node, "round": r,
                         "value": 0.48 + 0.05 * r + 0.01 * node, "step": r, "ts": "2026-01-01T00:00:00Z"})
            rows.append({"metric": "precision_macro", "scope": "node", "node_id": node, "round": r,
                         "value": 0.52 + 0.04 * r + 0.01 * node, "step": r, "ts": "2026-01-01T00:00:00Z"})
            rows.append({"metric": "cpu_percent", "scope": "node", "node_id": node, "round": r,
                         "value": 30.0 + 2.0 * r + node, "step": r, "ts": "2026-01-01T00:00:00Z"})
            rows.append({"metric": "ram_percent", "scope": "node", "node_id": node, "round": r,
                         "value": 55.0 + 1.0 * r + node, "step": r, "ts": "2026-01-01T00:00:00Z"})
            rows.append({"metric": "cpu_temp", "scope": "node", "node_id": node, "round": r,
                         "value": 50.0 + 1.5 * r + 0.5 * node, "step": r, "ts": "2026-01-01T00:00:00Z"})
    return rows


def test_schema_version_is_1_1():
    assert METRICS_SCHEMA_VERSION == "1.1"


def test_tags_yaml_schema_1_1_and_new_metrics():
    import os

    import yaml

    here = os.path.join(os.path.dirname(__file__), "..", "nebula", "frontend", "metrics", "tags.yaml")
    with open(here) as f:
        data = yaml.safe_load(f) or {}
    assert str(data.get("schema_version")) == "1.1"
    agg = data.get("round_agg") or {}
    for m in ("f1_macro", "recall_macro", "precision_macro", "cpu_percent", "ram_percent", "cpu_temp"):
        assert m in agg, f"{m} falta en tags.yaml"


def test_canonical_meta_and_tag_map_cover_sprint1():
    for m in ("f1_macro", "recall_macro", "precision_macro", "cpu_percent", "ram_percent", "cpu_temp"):
        assert m in METRIC_META, f"{m} falta en METRIC_META"
        assert m in ROUND_AGG, f"{m} falta en ROUND_AGG"
    assert TAG_MAP.get("Test (Local)/F1Score") == ("f1_macro", "step_is_round")
    assert TAG_MAP.get("Test (Local)/Recall") == ("recall_macro", "step_is_round")
    assert TAG_MAP.get("Test (Local)/Precision") == ("precision_macro", "step_is_round")
    assert TAG_MAP.get("W-CPU/CPU global (%)") == ("cpu_percent", "around_map")
    assert TAG_MAP.get("Z-RAM/RAM global (%)") == ("ram_percent", "around_map")
    assert TAG_MAP.get("W-CPU/CPU temperature (°)") == ("cpu_temp", "around_map")


def test_digest_includes_6_metrics_with_required_keys():
    series = pd.DataFrame(_rows_3x5())
    events = pd.DataFrame(columns=["ts", "round", "node_id", "type", "source", "detail"])
    meta = {"nodes": [{"id": 0}, {"id": 1}, {"id": 2}]}
    digest = build_digest(series, events, meta)
    for m in ("f1_macro", "recall_macro", "precision_macro", "cpu_percent", "ram_percent", "cpu_temp"):
        assert m in digest["metrics"], f"{m} falta en digest"
        entry = digest["metrics"][m]
        for key in ("first", "final", "delta", "best", "converged_at", "rounds_with_data", "unit"):
            assert key in entry, f"{m} sin {key}"
        assert entry["rounds_with_data"] == 5
        assert entry["best"]["round"] in range(5)


def _fake_acc(points_by_tag):
    class FakeAcc:
        def Scalars(self, tag):
            return [SimpleNamespace(step=s, value=v, wall_time=1000.0 + s) for s, v in points_by_tag[tag]]
    return FakeAcc()


def test_cpu_temp_zero_sentinel_is_dropped():
    """Regla 5: el 0 de corridas viejas (sin sensor) no se sirve como medida."""
    from nebula.frontend.metrics.providers import ProviderResult

    p = TensorBoardProvider(logs_root=None)
    res, res_bucket, res_ts, covered = ProviderResult(), {}, {}, set()
    acc = _fake_acc({"W-CPU/CPU temperature (°)": [(0, 0.0), (1, 0.0)]})
    p._process_mapped_tag(res, 0, acc, "W-CPU/CPU temperature (°)",
                           ("cpu_temp", "around_map"), [(0, 0)], {}, res_bucket, res_ts, covered)
    assert [r for r in res.rows if r["metric"] == "cpu_temp"] == []


def test_f1_step_is_round_and_resources_feed_arx_bucket():
    from nebula.frontend.metrics.providers import (
        CPU_TEMP_TAG,
        CPU_UTIL_TAG,
        RAM_TAG,
        ProviderResult,
    )

    p = TensorBoardProvider(logs_root=None)
    res, res_bucket, res_ts = ProviderResult(), {}, {}
    acc = _fake_acc({"Test (Local)/F1Score": [(r, 0.5 + 0.05 * r) for r in range(5)]})
    p._process_mapped_tag(res, 1, acc, "Test (Local)/F1Score",
                           ("f1_macro", "step_is_round"), [], {}, res_bucket, res_ts, set())
    rounds = sorted(r["round"] for r in res.rows if r["metric"] == "f1_macro")
    assert rounds == [0, 1, 2, 3, 4]

    # CPU/RAM canónicos siguen alimentando el bucket ARX (backfill no se rompe).
    res2, bucket2, ts2 = ProviderResult(), {}, {}
    acc2 = _fake_acc({CPU_UTIL_TAG: [(10, 42.0)], RAM_TAG: [(10, 61.0)]})
    bounds = [(10, 2)]
    p._process_mapped_tag(res2, 0, acc2, CPU_UTIL_TAG, ("cpu_percent", "around_map"),
                           bounds, {}, bucket2, ts2, set())
    p._process_mapped_tag(res2, 0, acc2, RAM_TAG, ("ram_percent", "around_map"),
                           bounds, {}, bucket2, ts2, set())
    assert bucket2[2][CPU_UTIL_TAG] == [42.0]
    assert bucket2[2][RAM_TAG] == [61.0]
    assert CPU_TEMP_TAG not in bucket2[2]  # sin sensor: sin entrada inventada


def test_system_monitor_temperature_never_invents():
    """SystemMonitor.get_temperature() devuelve float o None, nunca falla."""
    from nebula.core.situationalawareness.awareness.sautils.sasystemmonitor import SystemMonitor

    async def go():
        mon = SystemMonitor()
        t = await mon.get_temperature()
        assert t is None or isinstance(t, float)
        res = await mon.get_system_resources()
        assert "cpu_temperature" in res
        assert res["cpu_temperature"] is None or isinstance(res["cpu_temperature"], float)

    asyncio.run(go())
