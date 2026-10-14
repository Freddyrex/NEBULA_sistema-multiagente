"""NEBULA metrics layer (SMAC improvements §4): canonical schema + providers.

Canonical row (long format, one measurement per row)::

    {"metric": "accuracy", "scope": "node", "node_id": 1, "round": 3,
     "value": 0.81, "step": 3, "ts": "2026-09-28T01:02:49Z"}

Rules (§4.2):

- ``metric`` is lowercase snake_case: ``accuracy``, ``loss``, ``f1_macro``,
  ``recall_macro``, ``precision_macro``, ``cpu_percent``, ``ram_percent``,
  ``cpu_temp``, ``round_time_s``, ``bytes_sent``, ``bytes_received``.
- ``scope`` is ``node`` (per participant) or ``global`` (mean across nodes,
  computed by the service, never stored).
- ``round`` is the federation round the measurement belongs to. Points that
  cannot be attributed to a round are dropped and counted in warnings.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
from dataclasses import dataclass, field

METRIC_META: dict[str, dict] = {
    # §6.9: unit+digits servidos desde el backend (METRIC_META canónico).
    # Sprint 1 (ROADMAP_AGENTE_ADAPTATIVO): + f1/recall/precision macro
    # (Test (Local), macro-promedio) y recursos medidos por ronda/nodo.
    "accuracy": {"unit": "ratio", "higher_is_better": True, "digits": 4},
    "loss": {"unit": "nats", "higher_is_better": False, "digits": 4},
    "f1_macro": {"unit": "ratio", "higher_is_better": True, "digits": 4},
    "recall_macro": {"unit": "ratio", "higher_is_better": True, "digits": 4},
    "precision_macro": {"unit": "ratio", "higher_is_better": True, "digits": 4},
    "cpu_percent": {"unit": "%", "higher_is_better": False, "digits": 1},
    "ram_percent": {"unit": "%", "higher_is_better": False, "digits": 1},
    "cpu_temp": {"unit": "°C", "higher_is_better": False, "digits": 1},
    "round_time_s": {"unit": "s", "higher_is_better": False, "digits": 2},
    "bytes_sent": {"unit": "MB", "higher_is_better": False, "digits": 2},
    "bytes_received": {"unit": "MB", "higher_is_better": False, "digits": 2},
    "cpu_temp_est": {"unit": "°C", "higher_is_better": False, "digits": 1},
    "gpu_temp_est": {"unit": "°C", "higher_is_better": False, "digits": 1},
    "energy_total": {"unit": "kWh", "higher_is_better": False, "digits": 6},
    "energy_estimated": {"unit": "kWh", "higher_is_better": False, "digits": 6},
}

METRIC_DIGITS: dict[str, int] = {k: v.get("digits", 2) for k, v in METRIC_META.items()}

#: TensorBoard tag -> (canonical metric, round strategy).
#: ``step_is_round``: the tag is logged once per round with step == round
#: (e.g. ``Train/Accuracy`` steps 0..R-1). ``around_map``: batch-level points
#: whose round is resolved through the ``A-Round`` step->round boundaries.
#: Los tags W-TEMP/* y E-Energy/* los escribe el Reporter desde esta versión;
#: en corridas viejas sin ellos se hace backfill desde las exógenas.
TAG_MAP: dict[str, tuple[str, str]] = {
    "Train/Accuracy": ("accuracy", "step_is_round"),
    "Train/Loss": ("loss", "around_map"),
    # Sprint 1: F1/recall/precision macro desde la evaluación local por ronda.
    # step == round (nebulamodel.log_metrics_end usa global_number[phase]).
    "Test (Local)/F1Score": ("f1_macro", "step_is_round"),
    "Test (Local)/Recall": ("recall_macro", "step_is_round"),
    "Test (Local)/Precision": ("precision_macro", "step_is_round"),
    # Sprint 1: recursos medidos del Reporter, resueltos por ronda vía A-Round.
    "W-CPU/CPU global (%)": ("cpu_percent", "around_map"),
    "Z-RAM/RAM global (%)": ("ram_percent", "around_map"),
    "W-CPU/CPU temperature (°)": ("cpu_temp", "around_map"),
    "X-Network/Network (MB sent)": ("bytes_sent", "around_map"),
    "X-Network/Network (MB received)": ("bytes_received", "around_map"),
    "W-TEMP/CPU_estimated_C": ("cpu_temp_est", "around_map"),
    "W-TEMP/GPU_estimated_C": ("gpu_temp_est", "around_map"),
    "E-Energy/Total_kWh": ("energy_total", "around_map"),
    "E-Energy/Estimated_kWh": ("energy_estimated", "around_map"),
}

#: Tags de recursos del Reporter usados como entradas exógenas del ARX
#: térmico y del modelo energético (valores medios por ronda).
CPU_UTIL_TAG = "W-CPU/CPU global (%)"
CPU_TEMP_TAG = "W-CPU/CPU temperature (°)"
RAM_TAG = "Z-RAM/RAM global (%)"
DISK_TAG = "Y-Disk/Disk (%)"
NET_SENT_TAG = "X-Network/Network (MB sent)"
NET_RECV_TAG = "X-Network/Network (MB received)"
GPU_TAG_RE = r"^W-GPU/GPU(\d+)\s+(.*)$"
#: W-TEMP/GPU{i}_estimated_C -> gpu_temp_est (promedia GPUs como W-GPU).
GPU_TEMP_EST_RE = r"^W-TEMP/GPU(\d+)_estimated_C$"

AROUND_TAG = "A-Round"


def _to_iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.UTC).isoformat()


@dataclass
class ProviderResult:
    rows: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    participants: list[int] = field(default_factory=list)


class TensorBoardProvider:
    """Read per-participant TensorBoard logs into the canonical schema.

    Layout: ``<logs_root>/<scenario>/metrics/participant_<N>/events.*``.
    """

    def __init__(self, logs_root: str | None):
        self.logs_root = logs_root

    def scenario_dir(self, scenario: str) -> str:
        return os.path.join(self.logs_root or "", scenario, "metrics")

    def exists(self, scenario: str) -> bool:
        d = self.scenario_dir(scenario)
        return bool(self.logs_root) and os.path.isdir(d)

    def participants(self, scenario: str) -> list[int]:
        found = []
        try:
            for name in os.listdir(self.scenario_dir(scenario)):
                m = re.fullmatch(r"participant_(\d+)", name)
                if m and os.path.isdir(os.path.join(self.scenario_dir(scenario), name)):
                    found.append(int(m.group(1)))
        except OSError as e:
            logging.warning(f"metrics | cannot list {scenario}: {e}")
        return sorted(found)

    def mtime(self, scenario: str) -> float:
        """Newest mtime under the scenario metrics dir (cache key)."""
        newest = 0.0
        for root, _dirs, files in os.walk(self.scenario_dir(scenario)):
            for f in files:
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(root, f)))
                except OSError:
                    pass
        return newest

    def load(self, scenario: str) -> ProviderResult:
        from tensorboard.backend.event_processing import event_accumulator as ea_mod
        from tensorboard.backend.event_processing.event_accumulator import EventAccumulator

        result = ProviderResult()
        if not self.exists(scenario):
            result.warnings.append(f"No metrics directory for scenario '{scenario}'")
            return result
        result.participants = self.participants(scenario)
        if not result.participants:
            result.warnings.append(f"No participant logs for scenario '{scenario}'")
            return result

        guidance = {
            ea_mod.IMAGES: 0,
            ea_mod.AUDIO: 0,
            ea_mod.HISTOGRAMS: 0,
            ea_mod.COMPRESSED_HISTOGRAMS: 0,
            ea_mod.TENSORS: 0,
        }
        for node_id in result.participants:
            path = os.path.join(self.scenario_dir(scenario), f"participant_{node_id}")
            try:
                acc = EventAccumulator(path, size_guidance=guidance)
                acc.Reload()
            except Exception as e:
                result.warnings.append(f"participant_{node_id}: cannot read logs ({e})")
                continue
            try:
                tags = acc.Tags()["scalars"]
            except Exception as e:
                result.warnings.append(f"participant_{node_id}: no scalar tags ({e})")
                continue
            self._load_node(result, node_id, acc, tags)
        if not result.rows:
            result.warnings.append(
                f"Scenario '{scenario}' registered no mappable metrics "
                f"(mapped tags: {sorted(TAG_MAP)})"
            )
        return result

    def _load_node(self, result: ProviderResult, node_id: int, acc, tags: list[str]) -> None:
        """Procesa un participante: series canónicas + exógenas + derivadas."""
        boundaries = self._around_boundaries(acc)
        loss_times: dict[int, list[float]] = {}
        res: dict[int, dict[str, list[float]]] = {}
        res_ts: dict[int, list[float]] = {}
        # (métrica, ronda) ya cubierta por tags crudos W-TEMP/E-Energy: el
        # backfill ARX solo rellena lo que falte (corridas viejas).
        covered: set[tuple[str, int]] = set()
        for tag in tags:
            mapped = TAG_MAP.get(tag)
            if mapped:
                self._process_mapped_tag(result, node_id, acc, tag, mapped, boundaries, loss_times, res, res_ts, covered)
            elif re.fullmatch(GPU_TEMP_EST_RE, tag):
                self._process_mapped_tag(
                    result, node_id, acc, tag, ("gpu_temp_est", "around_map"),
                    boundaries, loss_times, res, res_ts, covered,
                )
            else:
                self._process_resource_tag(acc, tag, boundaries, res, res_ts)
        for rnd, times in loss_times.items():
            if len(times) >= 2:
                result.rows.append(
                    {
                        "metric": "round_time_s",
                        "scope": "node",
                        "node_id": node_id,
                        "round": rnd,
                        "value": float(max(times) - min(times)),
                        "step": rnd,
                        "ts": _to_iso(max(times)),
                    }
                )
        self._append_thermal_energy_rows(result, node_id, res, res_ts, loss_times, covered)

    @staticmethod
    def _push_resource(
        res: dict[int, dict[str, list[float]]],
        res_ts: dict[int, list[float]],
        rnd: int,
        key: str,
        val: float,
        wall: float,
    ) -> None:
        res.setdefault(rnd, {}).setdefault(key, []).append(float(val))
        res_ts.setdefault(rnd, []).append(float(wall))

    def _process_mapped_tag(
        self, result, node_id, acc, tag, mapped, boundaries, loss_times, res, res_ts,
        covered: set[tuple[str, int]] | None = None,
    ) -> None:
        metric, strategy = mapped
        try:
            points = acc.Scalars(tag)
        except Exception:
            return
        for p in points:
            rnd = self._resolve_round(strategy, p.step, boundaries)
            if rnd is None:
                continue
            try:
                val = float(p.value)
            except (TypeError, ValueError):
                continue
            # Sprint 1, regla 5: corridas viejas logueaban 0 como centinela de
            # "sin sensor". Un 0 °C nunca es una CPU real: se descarta y la
            # ronda queda sin cpu_temp (unavailable), con cpu_temp_est estimado.
            if metric == "cpu_temp" and val == 0:
                continue
            result.rows.append(
                {
                    "metric": metric,
                    "scope": "node",
                    "node_id": node_id,
                    "round": rnd,
                    "value": val,
                    "step": int(p.step),
                    "ts": _to_iso(p.wall_time),
                }
            )
            if covered is not None and metric in (
                "cpu_temp_est", "gpu_temp_est", "energy_total", "energy_estimated",
            ):
                covered.add((metric, int(rnd)))
            if metric == "loss":
                loss_times.setdefault(rnd, []).append(p.wall_time)
            # La red acumulada también alimenta N_io del ARX CPU. Los tags de
            # recursos ahora también son canónicos, pero siguen alimentando el
            # bucket ARX/energía (no se duplica: el backfill respeta `covered`).
            if tag in (CPU_UTIL_TAG, CPU_TEMP_TAG, RAM_TAG, DISK_TAG, NET_SENT_TAG, NET_RECV_TAG):
                self._push_resource(res, res_ts, rnd, tag, val, p.wall_time)

    def _process_resource_tag(self, acc, tag: str, boundaries, res, res_ts) -> None:
        if tag in (CPU_UTIL_TAG, CPU_TEMP_TAG, RAM_TAG, DISK_TAG):
            key: str | None = tag
        else:
            m = re.fullmatch(GPU_TAG_RE, tag)
            key = f"W-GPU/GPU <{m.group(2).strip()}>" if m else None
        if key is None:
            return
        try:
            points = acc.Scalars(tag)
        except Exception:
            return
        for p in points:
            rnd = self._resolve_round("around_map", p.step, boundaries)
            if rnd is None:
                continue
            self._push_resource(res, res_ts, rnd, key, p.value, p.wall_time)

    @staticmethod
    def _around_boundaries(acc) -> list[tuple[int, int]]:
        """Sorted (step, round) boundaries from the A-Round tag."""
        try:
            pts = acc.Scalars(AROUND_TAG)
        except Exception:
            return []
        bounds = sorted({(int(p.step), int(p.value)) for p in pts})
        return bounds

    @staticmethod
    def _mean(vals: list[float]) -> float | None:
        """§6.6: `None` si no hay datos (no `0.0`: idle ≠ missing)."""
        if not vals:
            return None
        try:
            return float(sum(vals) / len(vals))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _gpu_bucket_mean(bucket: dict[str, list[float]], mean_fn, suffix: str) -> float | None:
        vals: list[float] = []
        for k, v in bucket.items():
            if k.startswith("W-GPU/GPU <") and suffix in k:
                vals.extend(v)
        return mean_fn(vals)

    def _append_thermal_energy_rows(
        self,
        result: ProviderResult,
        node_id: int,
        res: dict[int, dict[str, list[float]]],
        res_ts: dict[int, list[float]],
        loss_times: dict[int, list[float]],
        covered: set[tuple[str, int]] | None = None,
    ) -> None:
        """Backfill ARX/energía por ronda (solo coridas viejas sin tags crudos).

        - ARX CPU: T[k+1] = a1*T[k] + b1*Ucpu + b2*Uram + b3*Udisk + b4*Nio
        - ARX GPU: T[k+1] = a2*T[k] + g1*Ugpu + g2*P + g3*f - g4*vfan
        - E_total lineal y E_estimado por integración de potencia.
        Sin datos de recursos en una ronda no se emite nada para ella.
        Las rondas en ``covered`` (tags W-TEMP/E-Energy ya logueados por el
        Reporter) no se duplican. Sin telemetría GPU se usa fallback CPU.
        """
        from nebula.core.utils.thermal_energy import (
            TEMP_SEED,
            cpu_power_from_util,
            estimate_energy_physical,
            estimate_energy_total,
            predict_cpu_temp_arx,
            predict_gpu_temp_arx,
        )

        covered = covered or set()
        if not res:
            return
        # Sprint 1, regla 5: etiquetar fuente de temperatura. Si el nodo no tiene
        # ninguna fila canónica cpu_temp (sensor), el cpu_temp_est que se emita
        # es ESTIMADO (ARX) y cpu_temp queda no disponible (sin filas).
        has_sensor = any(
            r.get("metric") == "cpu_temp" and r.get("node_id") == node_id for r in result.rows
        )
        if not has_sensor:
            result.warnings.append(
                f"participant_{node_id}: sin sensor de temp CPU, cpu_temp no disponible; "
                "cpu_temp_est es estimado (ARX)"
            )
        t_cpu_prev: float | None = None
        t_gpu_prev: float | None = None
        gpu_fallback_warned = False
        for rnd in sorted(res):
            bucket = res[rnd]
            if not bucket:
                continue
            u_cpu = self._mean(bucket.get(CPU_UTIL_TAG, []))
            if u_cpu is None:
                # §6.6: sin U_cpu no se emite 0.0; se marca missing y se salta.
                result.warnings.append(f"participant_{node_id}: ronda {rnd} sin U_cpu (missing, no idle)")
                continue
            u_ram = self._mean(bucket.get(RAM_TAG, [])) or 0.0
            u_disk = self._mean(bucket.get(DISK_TAG, [])) or 0.0
            _sent = self._mean(bucket.get(NET_SENT_TAG, [])) or 0.0
            _recv = self._mean(bucket.get(NET_RECV_TAG, [])) or 0.0
            n_io = _sent + _recv
            t_cpu_last = bucket.get(CPU_TEMP_TAG, [t_cpu_prev if t_cpu_prev is not None else TEMP_SEED])[-1]
            if t_cpu_prev is None:
                t_cpu_prev = float(t_cpu_last)
            u_gpu = self._gpu_bucket_mean(bucket, self._mean, "(%)") or 0.0
            p_gpu = self._gpu_bucket_mean(bucket, self._mean, "power") or 0.0
            f_gpu = self._gpu_bucket_mean(bucket, self._mean, "clocks") or 0.0
            v_fan = self._gpu_bucket_mean(bucket, self._mean, "fan speed") or 0.0
            t_gpu_last_list = [v for k, v in bucket.items() if k.startswith("W-GPU/GPU <") and "temperature" in k]
            t_gpu_last = float(t_gpu_last_list[0][-1]) if t_gpu_last_list and t_gpu_last_list[0] else None
            if t_gpu_prev is None:
                t_gpu_prev = float(t_gpu_last) if t_gpu_last is not None else TEMP_SEED

            cpu_est = predict_cpu_temp_arx(t_cpu_prev, u_cpu, u_ram, u_disk, n_io)
            t_cpu_prev = cpu_est
            has_gpu = t_gpu_last is not None or bool(p_gpu) or bool(u_gpu)
            if has_gpu:
                gpu_est = predict_gpu_temp_arx(t_gpu_prev or TEMP_SEED, u_gpu, p_gpu, f_gpu, v_fan)
            else:
                # Fallback CPU-only: sin GPU se reutiliza U_cpu y P_cpu = f(U_cpu).
                # Solo se avisa si el backfill va a emitir gpu_temp_est de verdad.
                if not gpu_fallback_warned and ("gpu_temp_est", int(rnd)) not in covered:
                    result.warnings.append(
                        f"participant_{node_id}: sin telemetría GPU, gpu_temp_est usa fallback CPU"
                    )
                    gpu_fallback_warned = True
                if t_gpu_prev is None:
                    t_gpu_prev = float(cpu_est)
                gpu_est = predict_gpu_temp_arx(
                    t_gpu_prev, u_cpu, cpu_power_from_util(u_cpu), 0.0, 0.0
                )
            t_gpu_prev = gpu_est
            walls = res_ts.get(rnd, [])
            ts = _to_iso(max(walls)) if walls else _to_iso(0.0)
            lt = loss_times.get(rnd, [])
            dt = float(max(walls) - min(walls)) if len(walls) >= 2 else 0.0
            if not dt and len(lt) >= 2:
                dt = float(max(lt) - min(lt))
            b_net = (self._mean(bucket.get(NET_SENT_TAG, [])) or 0.0) + (
                self._mean(bucket.get(NET_RECV_TAG, [])) or 0.0
            )
            e_total = estimate_energy_total(
                dt_s=dt, u_cpu_mean=u_cpu, u_gpu_mean=u_gpu, p_gpu_mean=p_gpu, b_net_mb=b_net
            )
            e_est = estimate_energy_physical(p_gpu_w=p_gpu, u_cpu=u_cpu, dt_s=dt)
            self._emit_backfill_rows(
                result, node_id, int(rnd), ts, cpu_est, gpu_est, e_total, e_est, covered
            )

    def _emit_backfill_rows(
        self,
        result: ProviderResult,
        node_id: int,
        rnd: int,
        ts: str,
        cpu_est: float,
        gpu_est: float,
        e_total: float,
        e_est: float,
        covered: set[tuple[str, int]],
    ) -> None:
        """Emite filas backfill salvo rondas ya cubiertas por tags crudos."""
        if ("cpu_temp_est", rnd) not in covered:
            result.rows.append(
                {"metric": "cpu_temp_est", "scope": "node", "node_id": node_id,
                 "round": rnd, "value": float(cpu_est), "step": rnd, "ts": ts}
            )
        if ("gpu_temp_est", rnd) not in covered:
            result.rows.append(
                {"metric": "gpu_temp_est", "scope": "node", "node_id": node_id,
                 "round": rnd, "value": float(gpu_est), "step": rnd, "ts": ts}
            )
        if ("energy_total", rnd) not in covered:
            result.rows.append(
                {"metric": "energy_total", "scope": "node", "node_id": node_id,
                 "round": rnd, "value": float(e_total), "step": rnd, "ts": ts}
            )
        if ("energy_estimated", rnd) not in covered:
            result.rows.append(
                {"metric": "energy_estimated", "scope": "node", "node_id": node_id,
                 "round": rnd, "value": float(e_est), "step": rnd, "ts": ts}
            )

    @staticmethod
    def _resolve_round(strategy: str, step: int, boundaries: list[tuple[int, int]]) -> int | None:
        if strategy == "step_is_round":
            return int(step) if int(step) >= 0 else None
        rnd = None
        for bstep, bround in boundaries:
            if int(step) >= bstep:
                rnd = bround
            else:
                break
        return rnd
