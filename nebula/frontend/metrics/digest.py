"""Digest precomputado para el agente (SMAC improvements §4.4).

El modelo **no recibe series crudas**: este resumen determinista es lo primero
que lee. Se cachea por ``(scenario_id, última_modificación)`` en el servicio.
"""

from __future__ import annotations

import pandas as pd

from nebula.config.defaults import (
    CONVERGENCE_EPS,
    CONVERGENCE_K,
    DIGEST_ROUND_ND,
    METRICS_SCHEMA_VERSION,
    NO_OUTLIER_METRICS,
    STRAGGLER_RATIO,
    outlier_gap_for,
)

METRIC_META = {
    "accuracy": {"higher_is_better": True},
    "loss": {"higher_is_better": False},
    # Sprint 1 (ROADMAP_AGENTE_ADAPTATIVO): F1/recall/precision macro + recursos.
    "f1_macro": {"higher_is_better": True},
    "recall_macro": {"higher_is_better": True},
    "precision_macro": {"higher_is_better": True},
    "cpu_percent": {"higher_is_better": False},
    "ram_percent": {"higher_is_better": False},
    "cpu_temp": {"higher_is_better": False},
    "round_time_s": {"higher_is_better": False},
    "bytes_sent": {"higher_is_better": False},
    "bytes_received": {"higher_is_better": False},
    "cpu_temp_est": {"higher_is_better": False},
    "gpu_temp_est": {"higher_is_better": False},
    "energy_total": {"higher_is_better": False},
    "energy_estimated": {"higher_is_better": False},
}


def _r(x, nd=DIGEST_ROUND_ND):
    return None if x is None or pd.isna(x) else round(float(x), nd)


def convergence_round(s: pd.Series, eps=CONVERGENCE_EPS, k=CONVERGENCE_K):
    """Primera ronda desde la que la variación entre rondas es < eps durante k rondas."""
    vals, rounds = s.to_list(), s.index.to_list()
    for i in range(1, len(vals) - k + 1):
        if all(abs(vals[j] - vals[j - 1]) < eps for j in range(i, i + k)):
            return int(rounds[i - 1])
    return None


def build_digest(
    series: pd.DataFrame,
    events: pd.DataFrame,
    meta: dict,
    gap=None,
    straggler_ratio=STRAGGLER_RATIO,
) -> dict:
    """series: [metric, scope, node_id, round, value, ts]
    events: [ts, round, node_id, type, source, detail]"""
    digest = {
        "schema_version": METRICS_SCHEMA_VERSION,
        "scenario": meta,
        "metrics": {},
        "nodes": {},
        "events": {},
        "warnings": [],
    }
    if series.empty:
        digest["warnings"].append("no metric series available")
        return digest

    node_rows = series[series["scope"] == "node"]
    if node_rows.empty:
        digest["warnings"].append("no node-scoped series available")
        return digest

    for metric, m in node_rows.groupby("metric"):
        hib = METRIC_META.get(metric, {}).get("higher_is_better", True)
        per_round = m.groupby("round")["value"].mean().sort_index()
        if per_round.empty:
            continue
        last = per_round.index.max()
        best = per_round.idxmax() if hib else per_round.idxmin()
        digest["metrics"][metric] = {
            "first": _r(per_round.iloc[0]),
            "final": _r(per_round.loc[last]),
            "delta": _r(per_round.loc[last] - per_round.iloc[0]),
            "best": {"round": int(best), "value": _r(per_round.loc[best])},
            "converged_at": convergence_round(per_round) if metric != "round_time_s" else None,
            "rounds_with_data": int(per_round.shape[0]),
            "unit": _unit(metric),
        }
        # Ranking por nodo en la última ronda con datos
        final = m[m["round"] == last].set_index("node_id")["value"].sort_values(ascending=not hib)
        digest["metrics"][metric]["ranking_final"] = [
            {"node_id": int(n), "value": _r(v)} for n, v in final.items()
        ]
        median = final.median()
        # §6.7: gap por métrica (JSON env) con fallback al global.
        _gap = float(gap) if gap is not None else outlier_gap_for(metric)
        digest["metrics"][metric]["outliers"] = [
            {"node_id": int(n), "value": _r(v), "gap_vs_median": _r(v - median)}
            for n, v in final.items()
            if abs(v - median) > _gap and metric not in NO_OUTLIER_METRICS
        ]
        digest["metrics"][metric]["outlier_gap"] = _gap

    # Nodos lentos (tiempo medio de ronda vs mediana)
    if "round_time_s" in node_rows["metric"].values:
        t = node_rows[node_rows["metric"] == "round_time_s"].groupby("node_id")["value"].mean()
        med = t.median()
        if med and med > 0:
            digest["nodes"]["stragglers"] = [
                {"node_id": int(n), "mean_round_time_s": _r(v), "ratio_vs_median": _r(v / med, 2)}
                for n, v in t.items()
                if v / med > straggler_ratio
            ]

    # Cobertura: nodos sin datos en la última ronda
    expected = {n["id"] for n in meta.get("nodes", [])}
    for metric, m in node_rows.groupby("metric"):
        last_round = m["round"].max()
        missing = expected - set(m[m["round"] == last_round]["node_id"])
        if missing:
            digest["warnings"].append(
                f"{metric}: sin datos en la última ronda para los nodos {sorted(missing)}"
            )

    if not events.empty:
        digest["events"] = {
            "count": len(events),
            "by_type": events["type"].value_counts().to_dict(),
            "first_per_node": {
                int(n): {"round": _round_or_none(g["round"].min()), "type": g.sort_values("ts").iloc[0]["type"]}
                for n, g in events.dropna(subset=["node_id"]).groupby("node_id")
            },
        }
    return digest


def _unit(metric: str) -> str:
    from .providers import METRIC_META as PROVIDER_META

    return PROVIDER_META.get(metric, {}).get("unit", "")


def _round_or_none(v):
    return None if pd.isna(v) else int(v)
