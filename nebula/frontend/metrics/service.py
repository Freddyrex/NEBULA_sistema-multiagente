"""MetricsService: una única fuente de verdad para UI y agente (§4).

Lo que el usuario ve y lo que el agente sabe salen del mismo servicio.
El digest se cachea por ``(scenario_id, última_modificación)``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time

import pandas as pd

from .digest import build_digest
from .providers import METRIC_META, TensorBoardProvider

SCENARIO_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

#: events.jsonl del backend -> (tipo canónico, fuente). Solo se exponen eventos
#: de federación; los mensajes de chat ("ask", "ask_answered"…) no son eventos.
AUDIT_MAP: dict[str, tuple[str, str]] = {
    "action_proposed": ("action_proposed", "user"),
    "action_confirmed": ("action_confirmed", "user"),
    "action_rejected": ("action_rejected", "user"),
    "action_executed": ("action_executed", "system"),
    "analyze": ("analyze", "agent"),
    "ask_answered": ("answer", "agent"),
    "ask_degraded": ("answer_degraded", "agent"),
}

SERIES_COLUMNS = ["metric", "scope", "node_id", "round", "value", "ts"]
EVENTS_COLUMNS = ["ts", "round", "node_id", "type", "source", "detail"]

#: Agregación canónica por ronda para cada métrica (los puntos TB crudos de
#: ``loss``/red son por batch; la serie canónica es un valor por ronda).
ROUND_AGG = {
    "accuracy": "mean",
    "loss": "mean",
    "round_time_s": "mean",
    "bytes_sent": "last",
    "bytes_received": "last",
}

_DIGEST_CACHE: dict[str, tuple[float, dict]] = {}


def check_scenario_id(scenario_id: str) -> str:
    if not scenario_id or scenario_id in (".", "..") or not SCENARIO_RE.fullmatch(scenario_id):
        raise ValueError(f"Invalid scenario id: {scenario_id!r}")
    return scenario_id


def _norm_ts(value: str | None) -> str | None:
    if not value:
        return None
    return str(value).replace(" ", "T", 1)


def _query(db_path: str, sql: str, args: tuple = ()) -> list[dict]:
    if not db_path or not os.path.isfile(db_path):
        return []
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute(sql, args)]
        db.close()
        return rows
    except Exception as e:
        logging.warning(f"metrics | db query failed ({db_path}): {e}")
        return []


class MetricsService:
    def __init__(self, log_dir: str | None, databases_dir: str | None):
        self.log_dir = log_dir
        self.databases_dir = databases_dir
        self.provider = TensorBoardProvider(log_dir or None)

    # -- meta -------------------------------------------------------------
    def scenario_meta(self, scenario: str) -> dict | None:
        from nebula.frontend.smac_chat import _summarize_node

        check_scenario_id(scenario)
        srows = _query(
            os.path.join(self.databases_dir or "", "scenarios.db"),
            "SELECT name, status, start_time, end_time, rounds, agg_algorithm,"
            " dataset, model, username FROM scenarios WHERE name = ?",
            (scenario,),
        )
        if not srows:
            return None
        s = srows[0]
        nrows = _query(
            os.path.join(self.databases_dir or "", "nodes.db"),
            "SELECT idx, role, neighbors, round, malicious, timestamp, ip, port"
            " FROM nodes WHERE scenario = ?",
            (scenario,),
        )
        completed = (s.get("status") or "").lower() == "completed"
        nodes = []
        for r in nrows:
            try:
                summary, _ = _summarize_node(
                    {
                        "idx": r.get("idx"),
                        "role": r.get("role"),
                        "neighbors": r.get("neighbors"),
                        "round": r.get("round"),
                        "malicious": r.get("malicious"),
                        "timestamp": r.get("timestamp"),
                    },
                    {},
                )
                state = summary.get("state", "unknown")
            except Exception:
                state = "unknown"
            if completed and state in ("stopped", "offline"):
                state = "finished"
            try:
                nid = int(str(r.get("idx")))
            except (TypeError, ValueError):
                continue
            nodes.append({"id": nid, "role": r.get("role"), "final_state": state})
        nodes.sort(key=lambda n: n["id"])
        try:
            rounds_total = int(str(s.get("rounds"))) if s.get("rounds") not in (None, "") else None
        except (TypeError, ValueError):
            rounds_total = None
        return {
            "id": scenario,
            "status": s.get("status"),
            "started_at": _norm_ts(s.get("start_time")),
            "finished_at": _norm_ts(s.get("end_time")),
            "rounds_total": rounds_total,
            "rounds_completed": None,  # se rellena con las series
            "aggregator": s.get("agg_algorithm"),
            "dataset": s.get("dataset"),
            "model": s.get("model"),
            "owner": s.get("username"),
            "nodes": nodes,
        }

    # -- series ------------------------------------------------------------
    def _frames(self, scenario: str) -> tuple[pd.DataFrame, list[str]]:
        res = self.provider.load(scenario)
        df = pd.DataFrame(res.rows, columns=SERIES_COLUMNS) if res.rows else pd.DataFrame(columns=SERIES_COLUMNS)
        return df, res.warnings

    def series(
        self,
        scenario: str,
        metric: str,
        node_ids: list[int] | None = None,
        round_from: int | None = None,
        round_to: int | None = None,
        max_points: int = 500,
    ) -> tuple[list[dict], dict]:
        """Serie canónica por ronda (+ media global). Devuelve (rows, info)."""
        check_scenario_id(scenario)
        if metric not in METRIC_META:
            raise KeyError(metric)
        df, warnings = self._frames(scenario)
        m = df[(df["metric"] == metric) & (df["scope"] == "node")]
        if node_ids:
            m = m[m["node_id"].isin(node_ids)]
        if round_from is not None:
            m = m[m["round"] >= round_from]
        if round_to is not None:
            m = m[m["round"] <= round_to]
        if m.empty:
            return [], {"metric": metric, "points": 0, "warnings": warnings}
        agg = ROUND_AGG.get(metric, "mean")
        per_node = m.groupby(["node_id", "round"])["value"].agg("last" if agg == "last" else "mean")
        rows = [
            {"metric": metric, "scope": "node", "node_id": int(n), "round": int(r), "value": float(v)}
            for (n, r), v in per_node.items()
        ]
        rows.sort(key=lambda d: (d["node_id"], d["round"]))
        if len(rows) > max_points:  # downsample uniforme por nodo
            keep: list[dict] = []
            by_node: dict[int, list[dict]] = {}
            for d in rows:
                by_node.setdefault(d["node_id"], []).append(d)
            per = max(1, max_points // max(1, len(by_node)))
            for n, ds in by_node.items():
                stride = max(1, len(ds) // per)
                keep.extend(ds[::stride][:per])
            rows = sorted(keep, key=lambda d: (d["node_id"], d["round"]))
        global_mean = m.groupby("round")["value"].mean().sort_index()
        for r, v in global_mean.items():
            rows.append({"metric": metric, "scope": "global", "node_id": None, "round": int(r), "value": float(v)})
        rows.sort(key=lambda d: (d["round"], d["node_id"] if d["node_id"] is not None else -1))
        return rows, {"metric": metric, "points": len(rows), "warnings": warnings}

    # -- events ------------------------------------------------------------
    def events(
        self, scenario: str, node_id: int | None = None, types: list[str] | None = None
    ) -> list[dict]:
        check_scenario_id(scenario)
        out: list[dict] = []
        path = os.path.join(self.log_dir or "", scenario, "smac_audit.jsonl")
        try:
            with open(path) as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    mapped = AUDIT_MAP.get(e.get("event", ""))
                    if not mapped:
                        continue
                    etype, source = mapped
                    payload = e.get("payload") or {}
                    params = payload.get("params") or {}
                    nid = params.get("node", payload.get("node_id", payload.get("node")))
                    try:
                        nid = int(str(nid)) if nid is not None else None
                    except (TypeError, ValueError):
                        nid = None
                    out.append(
                        {
                            "ts": e.get("ts"),
                            "round": payload.get("round"),
                            "node_id": nid,
                            "type": payload.get("kind", etype) if etype == "action_proposed" else etype,
                            "source": source,
                            "detail": payload.get("description", e.get("event")),
                        }
                    )
        except OSError:
            pass
        meta = self.scenario_meta(scenario)
        if meta and (meta.get("status") or "").lower() == "completed" and meta.get("finished_at"):
            out.append(
                {
                    "ts": meta["finished_at"],
                    "round": meta.get("rounds_total"),
                    "node_id": None,
                    "type": "scenario_finished",
                    "source": "system",
                    "detail": f"Scenario {scenario} completed",
                }
            )
        if node_id is not None:
            out = [e for e in out if e["node_id"] == node_id]
        if types:
            out = [e for e in out if e["type"] in types]
        out.sort(key=lambda e: (e["ts"] or "", e["type"]))
        return out

    # -- summary (digest con caché) -----------------------------------------
    def summary(self, scenario: str) -> tuple[dict, dict]:
        """(payload, info). info incluye si vino de caché y tiempos (depura SLA)."""
        check_scenario_id(scenario)
        t0 = time.time()
        meta = self.scenario_meta(scenario)
        if meta is None:
            raise LookupError(scenario)
        mtime = self.provider.mtime(scenario)
        cached = _DIGEST_CACHE.get(scenario)
        if cached and cached[0] == mtime:
            digest = cached[1]
            from_cache = True
        else:
            df, warnings = self._frames(scenario)
            if not df.empty:
                meta["rounds_completed"] = int(df["round"].max())
            else:
                meta["rounds_completed"] = None
            events = pd.DataFrame(self.events(scenario), columns=EVENTS_COLUMNS)
            digest = build_digest(df, events, meta)
            digest["warnings"] = warnings + digest.get("warnings", [])
            _DIGEST_CACHE[scenario] = (mtime, digest)
            from_cache = False
        info = {
            "cached": from_cache,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "metrics_mtime": mtime,
            "metrics_available": sorted({k for k in digest.get("metrics", {})}),
        }
        return {"schema_version": "1.0", "scenario": meta, **{k: v for k, v in digest.items() if k != "scenario"}}, info

    def available_metrics(self, scenario: str) -> list[str]:
        check_scenario_id(scenario)
        df, _ = self._frames(scenario)
        if df.empty:
            return []
        return sorted(df["metric"].unique().tolist())
