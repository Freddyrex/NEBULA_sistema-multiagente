"""Cost model for architecture changes (Sprint 4, ROADMAP_AGENTE_ADAPTATIVO).

No architecture change may be suggested without its cost. Pure, deterministic,
LLM-free module (roadmap rules 2 and 5: works without Groq, never invents).

Five cost items per candidate change (each value carries its basis):
- compute: extra time per round / total (from measured mean round times).
- resources: projected RAM/CPU/temperature per node vs observed limits.
- network: MB per round (model size x neighbours, from measured byte counters).
- transition: lost rounds / retraining (continuity classification; the
  checkpoint-aware ``can_resume`` of Sprint 6 will replace the heuristic).
- benefit: projected accuracy/F1 gain as a RANGE with uncertainty
  ({low, mid, high} + method + basis), never a single number.

Every figure is labeled ``measured`` (from TensorBoard/digest), ``projected``
(model arithmetic on measured inputs) or ``hypothesis`` (explicit external
assumption). Missing inputs yield ``unavailable`` (None), never a silent
estimate. ``decide()`` is a multi-criteria gate (no fabricated
accuracy-vs-seconds exchange rate): resources under limits AND affordable
transition AND expected gain above ``min_gain`` with enough history.
"""

from __future__ import annotations

import logging
from typing import Any

COST_ITEMS = ("compute", "resources", "network", "transition", "benefit")

#: Shared with nebula/core/models/registry.py (single source of truth since
#: Sprint 5; re-exported here for backward compatibility).
try:
    from nebula.core.models.registry import MODEL_REGISTRY, compatible_models
except Exception:  # pragma: no cover - registry always ships with the repo
    MODEL_REGISTRY = {}
    compatible_models = lambda dataset: []  # noqa: E731

_FALLBACK_COST_THRESHOLDS: dict[str, Any] = {
    "ram_limit": 90.0,
    "cpu_limit": 90.0,
    "temp_limit": 85.0,
    "temp_est_limit": 90.0,
    "min_gain": 0.01,
    "max_lost_rounds": 5,
    "min_rounds_for_trend": 4,
}


def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v


def can_resume(
    checkpoint: dict[str, Any] | None,
    new_config: dict[str, Any] | None = None,
    candidate_shapes: dict[str, list] | None = None,
    completed_rounds: Any = None,
) -> dict[str, Any]:
    """Sprint 6: can we continue from a checkpoint under a new config?

    ``checkpoint``: meta dict as stored in meta.json (plus ``valid`` bool when
    pre-checked; unknown → treated as missing). ``new_config``: {dataset,
    from_model, to_model}. ``candidate_shapes``: {tensor: shape} of the target
    model when inspectable (drives the partial-layer report).

    Returns {mode: full|partial|none, reusable_layers, skipped_layers,
    lost_rounds_estimate, checkpoint, detail}. Feeds the Sprint 4 transition
    cost. Never raises; unknown data → mode none with explicit reasons.
    """
    try:
        new_config = new_config if isinstance(new_config, dict) else {}
        kind = new_config.get("kind", "model")
        if kind == "aggregator":
            return {"mode": "full", "reusable_layers": ["all"], "skipped_layers": [],
                    "lost_rounds_estimate": 0.0, "checkpoint": bool(checkpoint),
                    "detail": "hot-swap, mismo modelo"}
        if kind in ("rounds", "epochs", "batch"):
            return {"mode": "full", "reusable_layers": ["all"], "skipped_layers": [],
                    "lost_rounds_estimate": 0.0, "checkpoint": bool(checkpoint),
                    "detail": "solo parámetros del bucle"}
        try:
            completed = None if completed_rounds is None else float(completed_rounds)
        except (TypeError, ValueError):
            completed = None
        if not checkpoint:
            return {"mode": "none", "reusable_layers": [], "skipped_layers": [],
                    "lost_rounds_estimate": completed,
                    "checkpoint": False, "detail": "sin checkpoint: nueva ejecución desde cero"}
        try:
            from nebula.core.models.registry import check_compatible

            ok, msg = check_compatible(checkpoint.get("dataset") or new_config.get("dataset"),
                                       checkpoint.get("model_name") or new_config.get("from_model"),
                                       new_config.get("to_model"))
        except Exception:
            return {"mode": "none", "reusable_layers": [], "skipped_layers": [],
                    "lost_rounds_estimate": completed, "checkpoint": True,
                    "detail": "registro no disponible: nueva ejecución desde cero"}
        if not ok:
            if "already using" in msg:
                return {"mode": "full", "reusable_layers": ["all"], "skipped_layers": [],
                        "lost_rounds_estimate": 0.0, "checkpoint": True,
                        "detail": "mismo modelo: recarga de state_dict"}
            return {"mode": "none", "reusable_layers": [], "skipped_layers": [],
                    "lost_rounds_estimate": completed, "checkpoint": True,
                    "detail": f"{msg}: nueva ejecución desde cero, con baseline guardado"}
        # Same dataset, other model: partial when shapes inspectable.
        ckpt_shapes = checkpoint.get("shapes") or {}
        if candidate_shapes and ckpt_shapes:
            try:
                cand = {str(k): list(v) for k, v in dict(candidate_shapes).items()}
                reusable = sorted(k for k, s in ckpt_shapes.items()
                                  if k in cand and list(s) == cand[k])
                skipped = sorted((set(ckpt_shapes) | set(cand)) - set(reusable))
            except Exception:
                reusable, skipped = [], sorted(ckpt_shapes)
            return {"mode": "partial", "reusable_layers": reusable, "skipped_layers": skipped,
                    "lost_rounds_estimate": completed,
                    "checkpoint": True,
                    "detail": (f"capas parcialmente compatibles: {len(reusable)} se reutilizan, "
                               f"{len(skipped)} se reinician (strict=False)")}
        return {"mode": "partial", "reusable_layers": [], "skipped_layers": [],
                "lost_rounds_estimate": completed, "checkpoint": True,
                "detail": "mismo dataset, otro modelo: capas sin inspeccionar "
                          "(ver en ejecución); conservador: se pierde lo andado"}
    except Exception as e:
        return {"mode": "none", "reusable_layers": [], "skipped_layers": [],
                "lost_rounds_estimate": None, "checkpoint": bool(checkpoint),
                "detail": f"evaluación fallida: {e}"}


#: Sprint 5: editable change fields (roadmap whitelist: rounds, epochs,
#: batch_size, aggregator, compatible model). Anything else is rejected.
EDITABLE_CHANGE_FIELDS = frozenset({
    "kind", "total_rounds", "current_rounds", "epochs", "batch_size",
    "dataset", "from_dataset", "from_model", "to_model", "algorithm",
    "trend_slope", "expected_gain",
})


def _check_change_structure(change: dict) -> tuple[bool, str]:
    """Whitelist + per-kind structural validation. Never raises."""
    try:
        unknown = sorted(set(change) - EDITABLE_CHANGE_FIELDS)
        if unknown:
            return False, f"campos no editables: {unknown} (whitelist: rounds, epochs, batch_size, aggregator, model)"
        kind = change.get("kind")
        if kind == "rounds":
            try:
                total = int(change.get("total_rounds"))
            except (TypeError, ValueError):
                return False, "rounds necesita total_rounds entero"
            if total <= 0 or total > 100000:
                return False, f"total_rounds {total} fuera de rango (1..100000)"
        elif kind == "epochs":
            try:
                epochs = int(change.get("epochs"))
            except (TypeError, ValueError):
                return False, "epochs necesita epochs entero"
            if epochs <= 0 or epochs > 1000:
                return False, f"epochs {epochs} fuera de rango (1..1000)"
        elif kind == "model":
            ds = change.get("dataset")
            ok, msg = True, ""
            try:
                from nebula.core.models.registry import check_compatible

                ok, msg = check_compatible(ds, change.get("from_model"), change.get("to_model"))
            except Exception as e:
                return False, f"registro de modelos no disponible: {e}"
            if not ok:
                # Same-model no-op is a rejection with a clear message too.
                return False, msg
        elif kind == "aggregator":
            if change.get("algorithm") not in ("FedAvg", "Krum", "Median", "TrimmedMean"):
                return False, f"algoritmo {change.get('algorithm')!r} no soportado"
        elif kind == "batch":
            return True, ""  # execution explains the rebuild requirement
        else:
            return False, f"change kind {kind!r} no soportado (rounds|epochs|model|aggregator|batch)"
        return True, ""
    except Exception as e:
        return False, f"change ilegible: {e}"


def model_size_mb(dataset: Any, model_name: Any) -> dict[str, Any] | None:
    """Measured parameter count and MB for a (dataset, model) pair.

    Instantiates the class with defaults on CPU (no data, no GPU), exactly as
    ``nebula/core/node.py`` does. Returns ``{params, size_mb, basis}`` or None
    when unknown / not instantiable (callers must surface ``unavailable``).
    Heavy imports (torch/lightning) stay inside this function so importing
    this module is always cheap. Never raises.
    """
    try:
        from nebula.core.models.registry import instantiate

        model = instantiate(dataset, model_name)
        try:
            params = int(model.count_parameters())
        except Exception:
            params = int(sum(p.numel() for p in model.parameters() if p.requires_grad))
        try:
            size_b = int(sum(p.numel() * p.element_size() for p in model.parameters()))
        except Exception:
            size_b = int(params * 4)
        return {"params": params, "size_mb": round(size_b / 1024**2, 4), "basis": "measured"}
    except Exception as e:
        logging.warning(f"cost-model | cannot measure {(dataset, model_name)}: {e}")
        return None


def compatible_models(dataset: Any) -> list[str]:
    """Model names supported for a dataset (swap candidates, Sprint 5)."""
    try:
        ds = str(dataset).upper()
        return sorted(name for (d, name) in MODEL_REGISTRY if d == ds)
    except Exception:
        return []


def classify_continuity(change: dict[str, Any] | None) -> dict[str, Any]:
    """Provisional continuity matrix (Sprint 6 replaces it with checkpoint-aware
    ``can_resume``). Never raises."""
    try:
        kind = (change or {}).get("kind")
    except Exception:
        kind = None
    if kind == "aggregator":
        return {"mode": "full", "reusable": "all (hot-swap, same model)",
                "detail": "aggregator hot-swap keeps model and optimizer state"}
    if kind in ("rounds", "epochs", "batch"):
        return {"mode": "full", "reusable": "all (loop parameters only)",
                "detail": "only the training loop changes"}
    if kind == "model":
        try:
            same_ds = str((change or {}).get("dataset", "")).upper() == \
                str((change or {}).get("from_dataset", (change or {}).get("dataset", ""))).upper()
            same_model = str((change or {}).get("from_model", "")).upper() == \
                str((change or {}).get("to_model", "")).upper()
        except Exception:
            same_ds, same_model = False, False
        if same_model:
            return {"mode": "full", "reusable": "all (state_dict reload)",
                    "detail": "same architecture, weights reload"}
        if same_ds:
            return {"mode": "none", "reusable": "none (incompatible state_dicts, retrain)",
                    "detail": "same I/O signature, incompatible weights: retrain from scratch "
                              "(partial layer reuse arrives with Sprint 6)"}
        return {"mode": "none", "reusable": "none (other dataset)",
                "detail": "incompatible model/dataset: new execution from scratch"}
    return {"mode": "none", "reusable": "unknown change kind",
            "detail": f"unknown change kind {kind!r}: conservative, no continuity assumed"}


def _measured_final(measured: dict, name: str) -> float | None:
    try:
        return _num((measured.get(name) or {}).get("final"))
    except Exception:
        return None


def estimate_cost(
    change: dict[str, Any] | None,
    measured: dict[str, Any] | None = None,
    topology: dict[str, Any] | None = None,
    sizes: dict[str, dict] | None = None,
    thresholds: dict[str, Any] | None = None,
    resume_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Estimate the 5 cost items for a candidate change. Never raises.

    ``change``: {kind: rounds|model|aggregator, ...kind-specific fields...,
    optional expected_gain {low,mid,high} (hypothesis) and trend_slope}.
    ``measured``: digest-style finals {round_time_s,cpu_percent,ram_percent,
    cpu_temp,cpu_temp_est,bytes_sent,bytes_received,accuracy,f1_macro,
    rounds_completed,...} each as {final,...} or plain numbers.
    ``topology``: {avg_neighbors, n_nodes}. ``sizes``: optional precomputed
    model_size_mb entries keyed "DATASET/MODEL".
    ``resume_info`` (Sprint 6): can_resume() output; overrides the transition
    estimate with checkpoint-aware continuity (mode + lost_rounds_estimate).
    """
    measured = measured if isinstance(measured, dict) else {}
    topology = topology if isinstance(topology, dict) else {}
    change = change if isinstance(change, dict) else {}
    kind = change.get("kind")
    cont = classify_continuity(change)
    t_mean = _measured_final(measured, "round_time_s")
    completed = _num(measured.get("rounds_completed"))
    avg_nb = _num(topology.get("avg_neighbors"))
    n_nodes = _num(topology.get("n_nodes"))

    def size_of(ds: Any, mn: Any) -> dict | None:
        try:
            if sizes and f"{str(ds).upper()}/{str(mn).upper()}" in sizes:
                return sizes[f"{str(ds).upper()}/{str(mn).upper()}"]
        except Exception:
            pass
        return model_size_mb(ds, mn)

    # --- compute + network + resources depend on the change kind ------------
    extra_rounds: float | None = None
    per_round_s_new: float | None = None
    net_per_round_mb: float | None = None
    proj_ram = _measured_final(measured, "ram_percent")
    proj_cpu = _measured_final(measured, "cpu_percent")
    proj_temp = _measured_final(measured, "cpu_temp")
    proj_temp_est = _measured_final(measured, "cpu_temp_est")
    res_basis = "measured"
    extra_time_s: float | None = None
    extra_net_mb: float | None = None

    if kind == "rounds":
        try:
            new_total = int(change.get("total_rounds"))
            cur_total = change.get("current_rounds")
            cur_total = int(cur_total) if cur_total is not None else (
                int(completed) if completed is not None else None)
        except (TypeError, ValueError):
            new_total, cur_total = None, None
        if new_total is not None and cur_total is not None:
            extra_rounds = float(new_total - cur_total)
        if extra_rounds is not None and t_mean is not None:
            extra_time_s = round(extra_rounds * t_mean, 2)
        b_sent, b_recv = _measured_final(measured, "bytes_sent"), _measured_final(measured, "bytes_received")
        if extra_rounds is not None and b_sent is not None and b_recv is not None and n_nodes:
            # bytes_* are cumulative counters: per-round rate from totals.
            per_round_net = (b_sent + b_recv) / max(1.0, completed or 1.0)
            extra_net_mb = round(extra_rounds * per_round_net, 3)
            net_per_round_mb = round(per_round_net, 3)
    elif kind == "epochs":
        # Per-round training time scales with epochs; remaining rounds pay it.
        try:
            new_ep = int(change.get("epochs"))
            cur_ep = change.get("current_epochs")
            cur_ep = int(cur_ep) if cur_ep is not None else None
            total = int(change.get("total_rounds", completed or 0))
            remaining = max(0.0, float(total) - float(completed or 0))
        except (TypeError, ValueError):
            new_ep, cur_ep, remaining = None, None, None
        if new_ep is not None and cur_ep and t_mean is not None and remaining is not None:
            extra_time_s = round((new_ep - cur_ep) / cur_ep * t_mean * remaining, 2)
            extra_rounds = 0.0
    elif kind == "model":
        ds = change.get("dataset")
        s_old = size_of(ds, change.get("from_model"))
        s_new = size_of(ds, change.get("to_model"))
        ratio = None
        try:
            if s_old and s_new and s_old["params"] > 0:
                ratio = s_new["params"] / s_old["params"]
        except Exception:
            ratio = None
        if ratio is not None:
            if proj_ram is not None:
                proj_ram = round(min(100.0, proj_ram * ratio), 1)
            if proj_cpu is not None:
                proj_cpu = round(min(100.0, proj_cpu * ratio), 1)
            res_basis = "projected"
        try:
            total = int(change.get("total_rounds", completed or 0))
            remaining = max(0.0, float(total) - float(completed or 0))
        except (TypeError, ValueError):
            remaining = None
        if t_mean is not None and ratio is not None and remaining is not None:
            per_round_s_new = round(t_mean * ratio, 2)
            extra_time_s = round(remaining * (per_round_s_new - t_mean), 2)
        if s_new and avg_nb is not None and remaining is not None:
            net_per_round_mb = round(s_new["size_mb"] * avg_nb, 3)
            extra_net_mb = round(net_per_round_mb * remaining, 3)
    elif kind == "aggregator":
        extra_rounds, extra_time_s, extra_net_mb = 0.0, 0.0, 0.0

    # --- transition ---------------------------------------------------------
    # Sprint 6: checkpoint-aware continuity overrides the matrix heuristic.
    resume_mode: str | None = None
    if isinstance(resume_info, dict) and resume_info.get("mode") in ("full", "partial", "none"):
        resume_mode = resume_info["mode"]
        cont = {"mode": resume_mode,
                "reusable": resume_info.get("reusable_layers", []),
                "detail": resume_info.get("detail", "")}
    if cont["mode"] == "full":
        lost_rounds: float | None = 0.0
    elif completed is not None and kind == "model":
        lost_rounds = float(completed)
    else:
        lost_rounds = None
    if resume_mode in ("partial", "none"):
        try:
            est = resume_info.get("lost_rounds_estimate")
            lost_rounds = None if est is None else float(est)
        except (TypeError, ValueError):
            pass
    retrain_s = round(lost_rounds * t_mean, 2) if lost_rounds and t_mean else (0.0 if lost_rounds == 0 else None)

    # --- benefit (range, never a single number) ------------------------------
    benefit = _estimate_benefit(change, measured, thresholds)

    def item(value: Any, unit: str, basis: str, detail: str = "") -> dict:
        return {"value": value, "unit": unit, "basis": basis, "detail": detail}

    return {
        "change": {"kind": kind, **{k: v for k, v in change.items() if k != "expected_gain"}},
        "compute": item(extra_time_s, "s", "projected" if extra_time_s is not None and kind == "model"
                        else ("measured" if extra_time_s is not None else "unavailable"),
                        f"extra_rounds={extra_rounds}, mean_round_s={t_mean}"),
        "resources": {
            "ram_percent": item(proj_ram, "%", res_basis if proj_ram is not None else "unavailable",
                                "projected scales with param ratio" if res_basis == "projected" else ""),
            "cpu_percent": item(proj_cpu, "%", res_basis if proj_cpu is not None else "unavailable", ""),
            "cpu_temp": item(proj_temp, "°C", "measured" if proj_temp is not None else "unavailable",
                              "sensor only; no projection without Sprint 5 thermal data"),
            "cpu_temp_est": item(proj_temp_est, "°C",
                                 "estimated" if proj_temp_est is not None else "unavailable", "ARX estimate"),
        },
        "network": item(extra_net_mb, "MB",
                        "projected" if extra_net_mb is not None else "unavailable",
                        f"per_round_mb={net_per_round_mb}, avg_neighbors={avg_nb}"),
        "transition": {"continuity": cont, "lost_rounds": item(
            lost_rounds, "rounds", "measured" if lost_rounds is not None else "unavailable",
            f"retrain_s={retrain_s}")},
        "benefit": benefit,
    }


def _estimate_benefit(
    change: dict, measured: dict, thresholds: dict | None
) -> dict[str, Any]:
    """Projected gain as {low, mid, high} + method + basis. Never raises."""
    try:
        hyp = (change or {}).get("expected_gain") or {}
        if hyp.get("mid") is not None:
            return {
                "low": _num(hyp.get("low")), "mid": _num(hyp.get("mid")), "high": _num(hyp.get("high")),
                "unit": "accuracy_points", "basis": "hypothesis",
                "method": "explicit external hypothesis (operator/analyst); verify post-change (Sprint 7)",
            }
    except Exception:
        pass
    try:
        slope = _num((change or {}).get("trend_slope"))
        kind = (change or {}).get("kind")
        final = _measured_final(measured, "accuracy")
        completed = _num(measured.get("rounds_completed"))
        th = thresholds if isinstance(thresholds, dict) else {}
        min_hist = int((th.get("cost_model") or {}).get("min_rounds_for_trend",
                                                        _FALLBACK_COST_THRESHOLDS["min_rounds_for_trend"]))
        if kind == "rounds" and slope is not None and slope > 0 and final is not None:
            delta = _num(change.get("total_rounds")) - _num(change.get("current_rounds", completed))
            if delta is not None and delta > 0:
                mid = round(slope * delta, 4)
                headroom = max(0.0, 1.0 - final)
                return {
                    "low": 0.0, "mid": mid, "high": round(min(mid * 2, headroom), 4),
                    "unit": "accuracy_points", "basis": "measured",
                    "method": (f"trend slope {slope}/round x {delta} rounds; low=0 (no guarantee); "
                               f"high capped by headroom {headroom:.4f}"),
                    "history_ok": bool(completed is not None and completed >= min_hist),
                }
        if kind == "rounds" and slope is not None and slope <= 0:
            return {"low": 0.0, "mid": 0.0, "high": 0.0, "unit": "accuracy_points",
                    "basis": "measured", "method": "flat/negative trend: no gain expected from more rounds"}
    except Exception:
        pass
    return {"low": None, "mid": None, "high": None, "unit": "accuracy_points",
            "basis": "unavailable",
            "method": "no hypothesis and no usable trend: gain unknown, do not suggest on benefit"}


def decide(cost: dict[str, Any] | None, thresholds: dict[str, Any] | None = None) -> dict[str, Any]:
    """Multi-criteria gate: resources under limits AND affordable transition
    AND expected gain above min_gain with enough history. Returns
    {worthwhile, reasons[], verdict}. Never raises; missing data → not
    worthwhile with explicit reasons (roadmap rule 5)."""
    th = dict(_FALLBACK_COST_THRESHOLDS)
    try:
        th.update((thresholds or {}).get("cost_model") or {})
    except Exception:
        pass
    reasons: list[str] = []
    ok = True
    if not isinstance(cost, dict) or any(k not in cost for k in COST_ITEMS):
        return {"worthwhile": False, "reasons": ["cost incomplete: 5 items required"],
                "verdict": "no conviene: costo incompleto"}
    try:
        res = cost.get("resources") or {}
        for key, lim_key in (("ram_percent", "ram_limit"), ("cpu_percent", "cpu_limit"),
                             ("cpu_temp", "temp_limit"), ("cpu_temp_est", "temp_est_limit")):
            entry = res.get(key) or {}
            v, lim = _num(entry.get("value")), _num(th.get(lim_key))
            if v is not None and lim is not None and v > lim:
                ok = False
                reasons.append(f"{key} proyectado {v} supera el límite {lim} "
                               f"(basis {entry.get('basis')})")
        trans = cost.get("transition") or {}
        lost = _num((trans.get("lost_rounds") or {}).get("value"))
        max_lost = _num(th.get("max_lost_rounds"))
        if lost is not None and max_lost is not None and lost > max_lost:
            ok = False
            reasons.append(f"transición pierde {lost} rondas (> máx {max_lost})")
        ben = cost.get("benefit") or {}
        mid, low = _num(ben.get("mid")), _num(ben.get("low"))
        min_gain = _num(th.get("min_gain"))
        change = cost.get("change") or {}
        extra_t = _num((cost.get("compute") or {}).get("value"))
        saving_cut = change.get("kind") in ("rounds", "epochs") and extra_t is not None and extra_t < 0
        if saving_cut:
            reasons.append(f"ahorro de {-extra_t}s por recorte")
        elif mid is None:
            ok = False
            reasons.append("beneficio desconocido (sin hipótesis ni tendencia usable)")
        elif min_gain is not None and mid < min_gain:
            ok = False
            reasons.append(f"ganancia media {mid} < mínima {min_gain} (rango "
                           f"[{low}, {ben.get('high')}])")
        elif mid is None:
            ok = False
            reasons.append("beneficio desconocido (sin hipótesis ni tendencia usable)")
        elif min_gain is not None and mid < min_gain:
            ok = False
            reasons.append(f"ganancia media {mid} < mínima {min_gain} (rango "
                           f"[{low}, {ben.get('high')}])")
        if ben.get("basis") == "measured" and ben.get("history_ok") is False:
            ok = False
            reasons.append("poco historial para proyectar tendencia")
    except Exception as e:
        return {"worthwhile": False, "reasons": [f"error evaluando costo: {e}"],
                "verdict": "no conviene: error de evaluación"}
    if ok:
        reasons.append("beneficio esperado supera el costo y recursos bajo umbrales")
    return {"worthwhile": ok, "reasons": reasons,
            "verdict": "conviene" if ok else f"no conviene: {'; '.join(reasons)}"}


def _claimed_within_limits(cost: dict, th: dict) -> tuple[bool, str]:
    try:
        res = cost.get("resources") or {}
        for key, lim_key in (("ram_percent", "ram_limit"), ("cpu_percent", "cpu_limit"),
                             ("cpu_temp", "temp_limit"), ("cpu_temp_est", "temp_est_limit")):
            entry = res.get(key) or {}
            v, lim = _num(entry.get("value")), _num(th.get(lim_key))
            if v is not None and lim is not None and v > lim:
                return False, f"{key} declarado {v} supera el límite {lim}"
    except Exception as e:
        return False, f"costo declarado ilegible: {e}"
    return True, ""


def validate_for_backend(
    params: dict[str, Any] | None,
    measured: dict[str, Any] | None = None,
    topology: dict[str, Any] | None = None,
    thresholds: dict[str, Any] | None = None,
) -> tuple[bool, str, dict]:
    """Backend guard for propose_architecture_change (Sprint 4).

    Rejects when: cost missing/incomplete; claimed resources already over
    limits; or an independent recomputation from measured inputs exceeds
    limits — even if the LLM-claimed cost says otherwise. Without measured
    inputs the check is claim-only and marked ``unverified``. Never raises.
    """
    th = dict(_FALLBACK_COST_THRESHOLDS)
    try:
        th.update((thresholds or {}).get("cost_model") or {})
    except Exception:
        pass
    try:
        params = params if isinstance(params, dict) else {}
        change = params.get("change") if isinstance(params.get("change"), dict) else None
        cost = params.get("cost") if isinstance(params.get("cost"), dict) else None
        if change is None:
            return False, "change inválido: kind rounds|epochs|model|aggregator|batch requerido", {}
        ok_struct, why_struct = _check_change_structure(change)
        if not ok_struct:
            return False, f"rechazado por el backend: {why_struct}", {}
        if cost is None or any(k not in cost for k in COST_ITEMS):
            return False, "cost requerido con los 5 rubros (compute/resources/network/transition/benefit)", {}
        ok_claim, why = _claimed_within_limits(cost, th)
        if not ok_claim:
            return False, f"rechazado por el backend: {why}", cost
        if measured:
            recomputed = estimate_cost(change, measured, topology, thresholds=thresholds)
            decision = decide(recomputed, thresholds)
            res = recomputed.get("resources") or {}
            over = [k for k, lk in (("ram_percent", "ram_limit"), ("cpu_percent", "cpu_limit"),
                                    ("cpu_temp", "temp_limit"), ("cpu_temp_est", "temp_est_limit"))
                    if _num((res.get(k) or {}).get("value")) is not None
                    and _num(th.get(lk)) is not None
                    and _num((res.get(k) or {}).get("value")) > _num(th.get(lk))]
            if over:
                return False, (f"rechazado por el backend: recálculo con datos medidos excede "
                               f"{over} aunque lo declarado pase"), cost
            if not decision["worthwhile"]:
                return False, f"rechazado por el backend: {decision['verdict']}", cost
            return True, "costo verificado con datos medidos", cost
        cost = dict(cost, _unverified="no measured inputs: claim-only check")
        return True, "aceptado sin verificar (sin datos medidos)", cost
    except Exception as e:
        return False, f"error del guard: {e}", {}
