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

from nebula.config.defaults import METRICS_SCHEMA_VERSION, SERIES_DEFAULT_POINTS, SERIES_HARD_CAP, S_TO_MS
from nebula.frontend.exec_interpreter import (
    analyze_digest,
    due_verifications,
    load_thresholds,
    read_audit_tail,
    thresholds_mtime,
    verify_execution,
)

from .digest import build_digest
from .providers import METRIC_META, TensorBoardProvider

SCENARIO_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
SCHEMA_VERSION = METRICS_SCHEMA_VERSION

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
#: La energía por tick del Reporter se SUMA dentro de la ronda (kWh
#: acumulados); las temperaturas se promedian.
#: Fuente versionada: `nebula/frontend/metrics/tags.yaml` (bump de
#: `schema_version` si cambia). Este dict es el fallback en código.
def _load_round_agg() -> dict[str, str]:
    import os

    import yaml

    fallback = {
        "accuracy": "mean",
        "loss": "mean",
        "f1_macro": "mean",
        "recall_macro": "mean",
        "precision_macro": "mean",
        "cpu_percent": "mean",
        "ram_percent": "mean",
        "cpu_temp": "mean",
        "round_time_s": "mean",
        "bytes_sent": "last",
        "bytes_received": "last",
        "cpu_temp_est": "mean",
        "gpu_temp_est": "mean",
        "energy_total": "sum",
        "energy_estimated": "sum",
    }
    try:
        here = os.path.join(os.path.dirname(__file__), "tags.yaml")
        if os.path.isfile(here):
            with open(here) as f:
                data = yaml.safe_load(f) or {}
            agg = data.get("round_agg")
            if isinstance(agg, dict) and agg:
                return {str(k): str(v) for k, v in agg.items()}
    except Exception:
        pass
    return fallback


ROUND_AGG = _load_round_agg()

def _global_series_map(df) -> dict[str, list[tuple[int, float]]]:
    """Per-round global means from canonical node rows (for the interpreter).

    Sprint 2: the exec-interpreter needs real history (not just digest
    aggregates) for plateau / sustained-pressure rules and to cite rounds.
    """
    out: dict[str, list[tuple[int, float]]] = {}
    try:
        node_rows = df[(df["metric"].notna()) & (df["scope"] == "node")]
        for metric, m in node_rows.groupby("metric"):
            per_round = m.groupby("round")["value"].mean().sort_index()
            out[str(metric)] = [(int(r), float(v)) for r, v in per_round.items()]
    except Exception:
        pass
    return out


_DIGEST_CACHE: dict[str, tuple[tuple, dict]] = {}

FINAL_REPORT_FILE = "NEBULA_final_report.md"


def render_final_report(
    scenario: str,
    digest: dict | None,
    audit_records: list[dict] | None = None,
    verifications: dict | None = None,
) -> str:
    """Final per-scenario report (Sprint 7): decisions, projected vs actual.

    Pure rendering from digest + audit tail + verification reports. Sections:
    lineage, decisions table (with projected cost when proposed), post-change
    verifications (calibration notes), findings summary. Never raises.
    """
    try:
        digest = digest if isinstance(digest, dict) else {}
        audit_records = [r for r in (audit_records or []) if isinstance(r, dict)]
        verifications = verifications if isinstance(verifications, dict) else {}
        meta = digest.get("scenario") or {}
        metrics = digest.get("metrics") or {}
        lin = digest.get("lineage") or {}
        lines = [f"# NEBULA final report — `{scenario}`", ""]
        lines.append(f"- status: {meta.get('status', 'unknown')}")
        lines.append(f"- rounds: {meta.get('rounds_completed', '?')}/{meta.get('rounds_total', '?')}")
        lines.append(f"- aggregator: {meta.get('aggregator', '?')} · dataset: {meta.get('dataset', '?')} "
                     f"· model: {meta.get('model', '?')}")
        chain = lin.get("chain") or []
        if chain:
            trail = " → ".join(f"{l.get('parent')} r{l.get('checkpoint_round')}" for l in chain
                                if isinstance(l, dict))
            lines.append(f"- lineage: {trail} → {scenario}")
        else:
            lines.append("- lineage: single execution (no resume chain)")
        lines += ["", "## Metrics (final)"]
        if metrics:
            for name in sorted(metrics):
                entry = metrics[name] or {}
                best = entry.get("best") or {}
                lines.append(f"- {name}: final {entry.get('final')} "
                             f"(best {best.get('value')} at r{best.get('round')}, "
                             f"Δ {entry.get('delta')}) [{entry.get('unit', '')}]")
        else:
            lines.append("- no metric series available")
        lines += ["", "## Decisions (audit trail)"]
        proposed = {}
        rows = []
        for rec in audit_records:
            ev, payload = rec.get("event"), rec.get("payload") or {}
            if ev == "action_proposed":
                proposed[str(payload.get("id"))] = payload
            if ev in ("action_executed", "action_rejected"):
                cost = (proposed.get(str(payload.get("id"))) or {}).get("cost") \
                    or (payload.get("params") or {}).get("cost")
                benefit = (cost or {}).get("benefit") or {}
                rows.append(
                    f"- {ev} {payload.get('kind')} (ronda {payload.get('round', '?')}): "
                    f"ok={payload.get('ok', '—')}, "
                    f"proyectado [{benefit.get('low', '?')}, {benefit.get('mid', '?')}, "
                    f"{benefit.get('high', '?')}] ({benefit.get('basis', 'sin costo')}) — "
                    f"{str(payload.get('detail', ''))[:160]}")
        lines.extend(rows or ["- no control actions recorded"])
        lines += ["", "## Post-change verifications (projected vs measured)"]
        done = verifications.get("completed") or []
        pending = verifications.get("pending") or []
        for v in done:
            if isinstance(v, dict):
                lines.append(f"- {v.get('kind')} (r{v.get('executed_round')}): {v.get('calibration')}")
        for v in pending:
            if isinstance(v, dict):
                lines.append(f"- {v.get('kind')}: pendiente (vence en r{v.get('due_round')})")
        if not done and not pending:
            lines.append("- no verifiable changes recorded")
        lines += ["", "## Findings"]
        for f in (digest.get("findings") or []):
            if isinstance(f, dict):
                lines.append(f"- [{f.get('severity')}] {f.get('type')}: {f.get('summary')}")
        if not digest.get("findings"):
            lines.append("- none (interpreter off or no data)")
        lines += ["", "_Generated automatically by MetricsService on scenario completion. "
                  "Projections are labeled measured/projected/hypothesis in the audit trail._"]
        return "\n".join(lines) + "\n"
    except Exception as e:
        return f"# NEBULA final report — `{scenario}`\n\nReport failed: {e}\n"


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
    def __init__(self, log_dir: str | None, databases_dir: str | None, config_dir: str | None = None):
        self.log_dir = log_dir
        self.databases_dir = databases_dir
        self.config_dir = config_dir
        self.provider = TensorBoardProvider(log_dir or None)

    def _scenario_json(self, scenario: str) -> dict:
        """scenario.json dict ({} when missing). Never raises."""
        try:
            check_scenario_id(scenario)
            path = os.path.join(self.config_dir or "", scenario, "scenario.json")
            if not os.path.isfile(path):
                return {}
            with open(path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def lineage(self, scenario: str, max_depth: int = 4) -> dict:
        """Parent → child resume chain (Sprint 6), oldest first. Never raises.

        Each link {scenario, checkpoint_round|None, parent}. Loop-guarded.
        Without config_dir (or without resume links) the chain is empty and
        every consumer keeps current behavior.
        """
        chain: list[dict] = []
        try:
            seen = {scenario}
            current = scenario
            for _ in range(max(0, max_depth)):
                data = self._scenario_json(current)
                resume = data.get("resume_from") or {}
                parent = (resume.get("scenario") if isinstance(resume, dict) else None) \
                    or data.get("parent_scenario")
                if not parent or parent in seen:
                    break
                try:
                    ckpt = resume.get("checkpoint_round") if isinstance(resume, dict) else None
                    ckpt = None if ckpt is None else int(ckpt)
                except (TypeError, ValueError):
                    ckpt = None
                chain.append({"scenario": current, "checkpoint_round": ckpt, "parent": str(parent)})
                seen.add(str(parent))
                current = str(parent)
            chain.reverse()
        except Exception:
            pass
        return {"chain": chain,
                "parent": chain[-1]["parent"] if chain else None,
                "depth": len(chain)}

    def ensure_final_report(self, scenario: str, meta: dict, digest: dict, metrics_mtime: float) -> str | None:
        """Write/update NEBULA_final_report.md once the scenario completed.

        Sprint 7: automatic (regenerated when metrics are newer than the
        report). Returns the path or None. Never raises.
        """
        try:
            status = str((meta or {}).get("status") or "").lower()
            if status not in ("completed", "finished") or not self.log_dir:
                return None
            path = os.path.join(self.log_dir, scenario, FINAL_REPORT_FILE)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) >= float(metrics_mtime or 0):
                    return path
            except (TypeError, ValueError, OSError):
                pass
            audit = read_audit_tail(self.log_dir, scenario, limit=500)
            try:
                th = load_thresholds()
            except Exception:
                th = None
            completed = (meta or {}).get("rounds_completed")
            dues = due_verifications(audit, completed, thresholds=th)
            proposed_by_id = {}
            for rec in audit:
                if rec.get("event") == "action_proposed" and (rec.get("payload") or {}).get("id"):
                    proposed_by_id[str(rec["payload"]["id"])] = rec["payload"]
            done, pending = [], []
            finals = {"rounds_completed": completed}
            for _mn, _me in ((digest or {}).get("metrics") or {}).items():
                if isinstance(_me, dict) and _me.get("final") is not None:
                    finals[f"{_mn}_final"] = _me["final"]
            for due in dues:
                if due.get("status") == "ready":
                    exe = next((r for r in audit
                                if r.get("event") == "action_executed"
                                and (r.get("payload") or {}).get("id") == due.get("action_id")), {})
                    payload = exe.get("payload", {}) if isinstance(exe, dict) else {}
                    cost = (proposed_by_id.get(str(due.get("action_id"))) or {}).get("cost")
                    done.append(verify_execution(payload, cost, finals))
                else:
                    pending.append(due)
            content = render_final_report(scenario, digest, audit,
                                          {"pending": pending, "completed": done})
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            return path
        except Exception as e:
            logging.warning(f"metrics | final report failed ({scenario}): {e}")
            return None

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

    def _stitched_frames(self, scenario: str, df: pd.DataFrame, warnings: list[str]) -> tuple[pd.DataFrame, list[str]]:
        """Prepend parent-run rows with a global round offset (Sprint 6).

        A child local round r resumed from parent checkpoint R plots at
        global R+1+r; parent rows beyond R are a divergent branch and are
        trimmed. Multi-level chains accumulate offsets (loop-guarded by
        lineage()). Inactive without config_dir/lineage. Never raises.
        """
        try:
            lin = self.lineage(scenario)
            if not lin.get("chain"):
                return df, warnings
            frames: list[tuple[str, pd.DataFrame]] = []
            if not df.empty:
                child = df.copy()
                child["run"] = scenario
                frames.append(("child", child))
            offset = 0
            change_points: list[int] = []
            for link in lin["chain"]:
                parent = link.get("parent")
                ckpt = link.get("checkpoint_round")
                if parent is None or ckpt is None:
                    break
                try:
                    pres = self.provider.load(str(parent))
                except Exception:
                    break
                if not pres.rows:
                    break
                pdf = pd.DataFrame(pres.rows, columns=SERIES_COLUMNS)
                if pdf.empty:
                    break
                pdf = pdf[(pdf["scope"] == "node") & (pdf["round"] <= int(ckpt))].copy()
                if pdf.empty:
                    break
                pdf["round"] = pdf["round"] + offset
                pdf["run"] = str(parent)
                frames.append(("parent", pdf))
                offset += int(ckpt) + 1
                change_points.append(offset)
            if len(frames) <= 1:
                return df, warnings
            rebased = []
            for kind, f in reversed(frames):
                if kind == "child":
                    f = f.copy()
                    f["round"] = f["round"] + offset
                rebased.append(f)
            full = pd.concat(rebased, ignore_index=True)
            warnings = list(warnings) + [
                f"stitched resume lineage ({len(frames) - 1} parent runs, "
                f"change points at global rounds {sorted(set(change_points))})"
            ]
            return full, warnings
        except Exception as e:
            logging.warning(f"metrics | stitch failed ({scenario}): {e}")
            return df, warnings

    def series(
        self,
        scenario: str,
        metric: str,
        node_ids: list[int] | None = None,
        round_from: int | None = None,
        round_to: int | None = None,
        max_points: int = SERIES_DEFAULT_POINTS,
    ) -> tuple[list[dict], dict]:
        """Serie canónica por ronda (+ media global). Devuelve (rows, info)."""
        check_scenario_id(scenario)
        if metric not in METRIC_META:
            raise KeyError(metric)
        df, warnings = self._frames(scenario)
        # Sprint 6: continuous curves across a resume chain (no-op otherwise).
        df, warnings = self._stitched_frames(scenario, df, warnings)
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
        per_node = m.groupby(["node_id", "round"])["value"].agg(
            "last" if agg == "last" else ("sum" if agg == "sum" else "mean")
        )
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
    def summary(self, scenario: str, findings_enabled: bool = False) -> tuple[dict, dict]:
        """(payload, info). info incluye si vino de caché y tiempos (depura SLA).

        Sprint 2: con ``findings_enabled=True`` el payload incluye ``findings``
        (lista del exec-interpreter, determinista, con evidencia). Apagado por
        defecto (aditivo): los llamadores existentes no cambian.
        """
        check_scenario_id(scenario)
        t0 = time.time()
        meta = self.scenario_meta(scenario)
        if meta is None:
            raise LookupError(scenario)
        mtime = self.provider.mtime(scenario)
        # La caché también depende del flag y de los umbrales del intérprete.
        thresh_mtime = thresholds_mtime() if findings_enabled else 0.0
        cache_key = (mtime, bool(findings_enabled), thresh_mtime)
        cached = _DIGEST_CACHE.get(scenario)
        if cached and cached[0] == cache_key:
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
            # Sprint 6: resume lineage (parent → child chain for the panel).
            try:
                digest["lineage"] = self.lineage(scenario)
            except Exception:
                digest["lineage"] = {"chain": [], "parent": None, "depth": 0}
            # Sprint 2: hallazgos tipados con evidencia (deterministas, sin LLM).
            digest["findings"] = []
            if findings_enabled and not df.empty:
                try:
                    series_map = _global_series_map(df)
                    digest["findings"] = analyze_digest(
                        digest, series_map, load_thresholds()
                    )
                    # Sprint 5: medias globales por ronda para las políticas de
                    # arquitectura (pendientes, mejora, divergencia).
                    digest["series"] = series_map
                except Exception as e:
                    logging.warning(f"metrics | exec interpreter failed ({scenario}): {e}")
                    digest["warnings"] = digest.get("warnings", []) + [f"exec interpreter failed: {e}"]
            _DIGEST_CACHE[scenario] = (cache_key, digest)
            from_cache = False
        # Sprint 7: automatic final report once completed (freshness-checked).
        try:
            report_path = self.ensure_final_report(scenario, meta, digest, mtime)
        except Exception:
            report_path = None
        info = {
            "cached": from_cache,
            "elapsed_ms": int((time.time() - t0) * S_TO_MS),
            "metrics_mtime": mtime,
            "metrics_available": sorted({k for k in digest.get("metrics", {})}),
            "final_report": report_path,
        }
        return {"schema_version": SCHEMA_VERSION, "scenario": meta, **{k: v for k, v in digest.items() if k != "scenario"}}, info

    def available_metrics(self, scenario: str) -> list[str]:
        check_scenario_id(scenario)
        df, _ = self._frames(scenario)
        if df.empty:
            return []
        return sorted(df["metric"].unique().tolist())
