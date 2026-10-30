"""Execution interpreter (Sprint 2, ROADMAP_AGENTE_ADAPTATIVO).

Deterministic, LLM-free translation of the metrics digest into typed findings.

Contract:
- Input: a digest dict as produced by
  :func:`nebula.frontend.metrics.digest.build_digest` (full or compacted for
  the LLM — both keep first/final/delta/best/converged_at/rounds_with_data,
  ranking_final/outliers and nodes.stragglers), plus an optional per-round
  global-mean series map ``{metric: [(round, mean_value), ...]}`` for rules
  with memory (plateau, sustained pressure) and for citing concrete rounds.
- Output: ``list[Finding]`` where
  ``Finding = {id, type, severity, metric, nodes[], evidence[], summary}``
  and each evidence entry is
  ``{metric, round|None, node_id|None, value|None, detail}``.
- Rules are configured in ``interpreter_thresholds.yaml`` (never hardcoded);
  a minimal fallback lives below following the ``tags.yaml`` precedent.
- Never invents numbers (roadmap rule 5): a rule without data simply does not
  fire; estimates keep their ``estimated`` label in evidence details.

Straggler note: live, latency-based prediction stays in
``StragglerPredictor.predict_stragglers`` (in-training, per-neighbor update
latencies — not available from a digest). This module surfaces the digest-level
``nodes.stragglers`` (mean round-time vs median, same ratio family) as
findings, gated by the YAML ``straggler_ratio``.

Byzantine note: digest only carries last-round outliers (gap vs median), so
``byzantine_suspected`` is a point proxy. True update-variance detection
arrives with the Sprint 3 policy engine.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

FINDING_TYPES = (
    "accuracy_plateau",
    "f1_recall_gap",
    "low_recall",
    "ram_pressure",
    "cpu_pressure",
    "thermal_throttle_risk",
    "straggler",
    "byzantine_suspected",
    "over_provisioned_rounds",
)

SEVERITIES = ("info", "warning", "critical")

SCENARIO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

THRESHOLDS_FILE = os.path.join(os.path.dirname(__file__), "interpreter_thresholds.yaml")

#: Fallback when the YAML is missing (mirrors the tags.yaml precedent).
_FALLBACK_THRESHOLDS: dict[str, Any] = {
    "min_rounds": 4,
    "plateau_eps": 0.005,
    "plateau_k": 3,
    "f1_accuracy_gap": 0.10,
    "min_recall": 0.60,
    "ram_high": 85.0,
    "ram_critical": 95.0,
    "cpu_high": 85.0,
    "cpu_critical": 95.0,
    "pressure_min_rounds": 3,
    "temp_high": 80.0,
    "temp_critical": 90.0,
    "temp_est_high": 85.0,
    "straggler_ratio": 1.5,
    "byzantine_metrics": ["accuracy", "f1_macro"],
    "overprovision_extra_rounds": 3,
}


def load_thresholds(path: str | None = None) -> dict[str, Any]:
    """Load interpreter thresholds (YAML overrides fallback). Never raises."""
    merged = dict(_FALLBACK_THRESHOLDS)
    try:
        import yaml

        here = path or THRESHOLDS_FILE
        if os.path.isfile(here):
            with open(here) as f:
                data = yaml.safe_load(f) or {}
            if isinstance(data, dict):
                for k, v in data.items():
                    merged[k] = v
    except Exception as e:
        logging.warning(f"exec-interpreter | thresholds fallback ({e})")
    return merged


def thresholds_mtime(path: str | None = None) -> float:
    """mtime of the thresholds file (0.0 when missing) for cache keys."""
    try:
        return os.path.getmtime(path or THRESHOLDS_FILE)
    except OSError:
        return 0.0


def interpreter_enabled(config_dir: str | None, scenario: str) -> bool:
    """Feature flag: ``scenario.json :: adaptive_agent.exec_interpreter``.

    Absent file / absent key → False (off by default, roadmap rule 3).
    Never raises.
    """
    return bool(adaptive_flags(config_dir, scenario).get("exec_interpreter", False))


def adaptive_flags(config_dir: str | None, scenario: str) -> dict[str, bool]:
    """Sprint 3+5: all ``scenario.json :: adaptive_agent`` flags (off by default).

    Keys: ``exec_interpreter`` (Sprint 2 findings), ``aggregator_suggestions``
    (Sprint 3 suggestion card), ``architecture_suggestions`` (Sprint 5
    rounds/model policies), ``pause_on_suggestion`` (Copilot UI Fase 1:
    auto-pause while suggestions are pending), ``aggregator_auto`` (reserved:
    Sprint 7 decides; even when true nothing executes without human
    confirmation).

    ``pause_on_suggestion`` default is context-dependent for backward
    compatibility: when there is no ``adaptive_agent`` block at all (legacy
    Sprint 7 deployments that never touched the new UI) it stays True, so the
    original auto-pause keeps working; when a non-empty ``adaptive_agent``
    dict lacks the key (hand-edited JSON), it defaults to False. The new UI
    always writes the key explicitly, so this nuance only affects edited files.
    Never raises. Reading a post-hoc edited scenario.json is safe (all disk
    readers are ``json.load`` + ``.get``); for creation-time payloads the
    controller ``Scenario`` accepts ``adaptive_agent`` since Sprint 3.
    """
    off = {"exec_interpreter": False, "aggregator_suggestions": False,
           "architecture_suggestions": False, "pause_on_suggestion": True,
           "aggregator_auto": False}
    try:
        if not config_dir or not scenario or not SCENARIO_ID_RE.fullmatch(str(scenario)):
            return off
        import json

        path = os.path.join(config_dir, str(scenario), "scenario.json")
        if not os.path.isfile(path):
            return off
        with open(path) as f:
            data = json.load(f) or {}
        raw = data.get("adaptive_agent")
        if not isinstance(raw, dict) or not raw:
            return off
        return {
            "exec_interpreter": bool(raw.get("exec_interpreter", False)),
            "aggregator_suggestions": bool(raw.get("aggregator_suggestions", False)),
            "architecture_suggestions": bool(raw.get("architecture_suggestions", False)),
            "pause_on_suggestion": bool(raw.get("pause_on_suggestion", False)),
            "aggregator_auto": bool(raw.get("aggregator_auto", False)),
        }
    except Exception:
        return off


#: Algorithms the policy may recommend (mirrors
#: aggregationselector.SUPPORTED_ALGORITHMS; kept local so this module stays
#: dependency-free — the frontend must not pull the training-loop import chain).
SUPPORTED_AGGREGATORS = ("FedAvg", "Krum", "Median", "TrimmedMean")

#: Findings that count as adverse context (block the return-to-FedAvg path).
_ADVERSE_FINDING_TYPES = frozenset({"byzantine_suspected", "straggler", "low_recall"})


def read_audit_tail(log_dir: str | None, scenario: str, limit: int = 200) -> list[dict]:
    """Last ``limit`` SMAC audit records for a scenario (oldest first).

    Pure file read of ``<log_dir>/<scenario>/smac_audit.jsonl``. Never raises;
    malformed lines are skipped.
    """
    out: list[dict] = []
    try:
        if not log_dir or not scenario or not SCENARIO_ID_RE.fullmatch(str(scenario)):
            return out
        import json

        path = os.path.join(log_dir, str(scenario), "smac_audit.jsonl")
        if not os.path.isfile(path):
            return out
        with open(path) as f:
            lines = f.readlines()
        for line in lines[-max(1, limit):]:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if isinstance(rec, dict) and rec.get("event"):
                out.append(rec)
    except Exception:
        return out
    return out


def _audit_param(rec: dict, key: str) -> Any:
    try:
        return (rec.get("payload") or {}).get(key)
    except Exception:
        return None


def current_aggregator(declared: Any, audit_records: list[dict] | None) -> tuple[Any, str]:
    """Effective aggregation algorithm: declared vs runtime (roadmap rule 4).

    The DB-declared ``agg_algorithm`` goes stale after a hot-swap (the swap
    only mutates the nodes' engine config). The audit log is the runtime
    record: the newest executed ``select_aggregator`` wins. Returns
    ``(algorithm, source)`` with source in ``{"audit", "declared", "unknown"}``.
    """
    try:
        for rec in reversed(audit_records or []):
            if rec.get("event") != "action_executed":
                continue
            params = _audit_param(rec, "params") or {}
            if _audit_param(rec, "kind") == "select_aggregator" and params.get("algorithm"):
                return params["algorithm"], "audit"
    except Exception:
        pass
    if isinstance(declared, str) and declared:
        return declared, "declared"
    return None, "unknown"


def cooldown_state(
    audit_records: list[dict] | None,
    rounds_completed: int | None,
    thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Hysteresis window for aggregator changes (Sprint 3).

    Suppresses a new suggestion while ``(rounds_completed - last_change_round)
    < cooldown_rounds`` after an executed ``select_aggregator``, or right after
    the operator rejected the same target (joined via the proposal id: the
    ``action_rejected`` record points at the ``action_proposed`` params).
    Round-less records fall back to recency (newest relevant record wins).
    Never raises.
    """
    try:
        policy = (thresholds or {}).get("aggregator_policy") or {}
        cooldown = max(1, int(policy.get("cooldown_rounds", 3)))
    except (TypeError, ValueError):
        cooldown = 3
    state: dict[str, Any] = {
        "in_cooldown": False,
        "remaining_rounds": 0,
        "cooldown_rounds": cooldown,
        "last_change_round": None,
        "last_change_algorithm": None,
        "last_rejected_algorithm": None,
    }
    try:
        records = list(audit_records or [])
    except Exception:
        return state
    proposed_by_id: dict[str, dict] = {}
    last_exec: dict | None = None
    last_rej: dict | None = None
    for rec in records:
        ev = rec.get("event")
        if ev == "action_proposed" and _audit_param(rec, "kind") == "select_aggregator":
            pid = _audit_param(rec, "id")
            if pid:
                proposed_by_id[str(pid)] = rec
        elif ev == "action_executed" and _audit_param(rec, "kind") == "select_aggregator":
            last_exec = rec
        elif ev == "action_rejected":
            last_rej = rec
    if last_exec is not None:
        params = _audit_param(last_exec, "params") or {}
        state["last_change_algorithm"] = params.get("algorithm")
        state["last_change_round"] = _audit_param(last_exec, "round")
    if last_rej is not None:
        prop = proposed_by_id.get(str(_audit_param(last_rej, "id") or ""))
        if prop is not None:
            state["last_rejected_algorithm"] = ((_audit_param(prop, "params") or {}).get("algorithm"))
    # Newest relevant record decides when rounds are missing.
    try:
        cur = None if rounds_completed is None else int(rounds_completed)
    except (TypeError, ValueError):
        cur = None
    try:
        last_round = state["last_change_round"]
        last_round = None if last_round is None else int(last_round)
    except (TypeError, ValueError):
        last_round = None
    if cur is not None and last_round is not None:
        remaining = cooldown - (cur - last_round)
        if remaining > 0:
            state["in_cooldown"] = True
            state["remaining_rounds"] = int(remaining)
    return state


def auto_allowed(flags: dict[str, Any] | None = None) -> bool:
    """Sprint 3: auto-execution is never allowed.

    The ``aggregator_auto`` scenario flag is reserved for the Sprint 7
    decision (roadmap open risk 3: no action without confirmation by default).
    Every suggestion carries ``requires_confirm: True`` regardless.
    """
    return False


_FALLBACK_ARCHITECTURE_POLICY: dict[str, Any] = {
    "plateau_buffer": 1,
    "improvement_slope": 0.02,
    "end_margin": 2,
    "extend_rounds": 3,
    "loss_rise_eps": 0.005,
    "divergence_k": 3,
    "epochs_step": 1,
    "epochs_min": 1,
}


def _slope_last(pts: list[tuple[int, float]], k: int) -> float | None:
    """Mean per-round slope over the last k points (None when insufficient)."""
    try:
        window = pts[-max(2, k):]
        if len(window) < 2:
            return None
        return (window[-1][1] - window[0][1]) / (window[-1][0] - window[0][0] or 1)
    except Exception:
        return None


def suggest_architecture(
    findings: list[dict] | None,
    series: dict[str, list[tuple[int, float]]] | None = None,
    measured: dict[str, Any] | None = None,
    declared: dict[str, Any] | None = None,
    thresholds: dict[str, Any] | None = None,
    model_options: list[dict[str, Any]] | None = None,
    scenario_completed: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Sprint 5 round/model policies with cost gate (Sprint 4).

    - Confirmed plateau → suggest finishing early (quantified savings).
    - Still-strong improvement near the end → suggest extending rounds.
    - Divergence (loss rising while accuracy flat) → suggest fewer local
      epochs; model swaps are only evaluated from explicit ``model_options``
      ({to_model, expected_gain?}) — without a gain hypothesis the candidate
      lands in ``rejected`` with an explicit "no conviene" verdict instead of
      being suggested.
    Every candidate goes through ``cost_model.estimate_cost`` + ``decide``;
    only worthwhile ones become ``propose_architecture_change`` suggestions
    (always ``requires_confirm``). Returns ``(suggestions, rejected)``.
    Never raises, never invents (roadmap rule 5).
    """
    from nebula.frontend.cost_model import decide, estimate_cost

    suggestions: list[dict] = []
    rejected: list[dict] = []
    if scenario_completed:
        # Hot changes are meaningless with no rounds left; the way forward is
        # a linked resume, not an in-place tweak.
        return [], [{"change": {}, "trigger": "completed",
                     "verdict": "no conviene: escenario finalizado; para continuar usa resume_from"}]
    try:
        th = dict(load_thresholds())
        if thresholds:
            th.update(thresholds)
        ap = dict(_FALLBACK_ARCHITECTURE_POLICY)
        ap.update(th.get("architecture_policy") or {})
        items = [f for f in (findings or []) if isinstance(f, dict)]
        series = series if isinstance(series, dict) else {}
        measured = measured if isinstance(measured, dict) else {}
        declared = declared if isinstance(declared, dict) else {}

        def _num(v: Any) -> float | None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        total = _num(declared.get("total_rounds"))
        completed = _num(measured.get("rounds_completed"))
        epochs = _num(declared.get("epochs"))
        acc_pts = _points(series, "accuracy")
        loss_pts = _points(series, "loss")
        plateau = [f for f in items if f.get("type") in ("accuracy_plateau", "over_provisioned_rounds")]
        by_type = {f.get("type"): f for f in items}

        def _push(change: dict, trigger: str, title: str, max_keep: int = 2) -> None:
            try:
                cost = estimate_cost(change, measured,
                                     {"avg_neighbors": declared.get("avg_neighbors"),
                                      "n_nodes": declared.get("n_nodes")},
                                     thresholds=th)
                verdict = decide(cost, th)
            except Exception as e:
                rejected.append({"change": change, "trigger": trigger,
                                 "verdict": f"no conviene: error de costo ({e})"})
                return
            if verdict.get("worthwhile") and len(suggestions) < max_keep:
                suggestions.append({
                    "id": f"arch-{change.get('kind')}-{trigger}",
                    "kind": "propose_architecture_change",
                    "params": {"change": change, "cost": cost, "trigger": trigger,
                               "previous": _previous_of(change, declared)},
                    "title": title,
                    "reason": verdict.get("verdict", "conviene"),
                    "evidence": list((by_type.get(trigger) or {}).get("evidence", []) or [])[:10],
                    "severity": "info",
                    "requires_confirm": True,
                    "auto": False,
                })
            else:
                rejected.append({"change": {k: change.get(k) for k in ("kind",) if k in change},
                                 "trigger": trigger,
                                 "verdict": verdict.get("verdict", "no conviene")})

        # 1. Confirmed plateau → finish early.
        if plateau and total is not None and completed is not None:
            try:
                conv = None
                for f in plateau:
                    for e in f.get("evidence", []) or []:
                        if isinstance(e, dict) and e.get("detail") == "converged_at (digest)" \
                                and e.get("round") is not None:
                            conv = int(e["round"])
                if conv is None:
                    # Plateau finding without a converged round: stop after
                    # the current round (+buffer) instead of inventing one.
                    conv = int(completed)
                new_total = (conv if conv is not None else int(completed)) + int(ap["plateau_buffer"])
                if new_total < int(total) and new_total >= 1:
                    _push({"kind": "rounds", "total_rounds": new_total,
                           "current_rounds": int(total)},
                          plateau[0].get("type", "accuracy_plateau"),
                          f"Terminar en r{new_total} (ahorro cuantificado)")
            except Exception:
                pass

        # 2. Strong improvement near the end → extend.
        try:
            slope = _slope_last(acc_pts, 3)
            margin = int(ap["end_margin"])
            if (slope is not None and slope > float(ap["improvement_slope"])
                    and total is not None and completed is not None
                    and 0 <= (total - completed) <= margin):
                new_total = int(total) + int(ap["extend_rounds"])
                _push({"kind": "rounds", "total_rounds": new_total,
                       "current_rounds": int(total), "trend_slope": slope},
                      "strong_improvement",
                      f"Ampliar a r{new_total} (mejora +{slope:.4f}/ronda)")
        except Exception:
            pass

        # 3. Divergence → fewer local epochs (model change needs hypothesis).
        diverged = False
        try:
            k = max(2, int(ap["divergence_k"]))
            loss_slope = _slope_last(loss_pts, k)
            acc_slope = _slope_last(acc_pts, k)
            if (loss_slope is not None and loss_slope > float(ap["loss_rise_eps"])
                    and (acc_slope is None or acc_slope <= float(ap["improvement_slope"]) / 2)
                    and epochs is not None and int(epochs) > int(ap["epochs_min"])):
                diverged = True
                _push({"kind": "epochs", "epochs": max(int(ap["epochs_min"]), int(epochs) - int(ap["epochs_step"])),
                       "current_epochs": int(epochs), "total_rounds": int(total) if total else None},
                      "divergence",
                      f"Bajar epochs a {max(int(ap['epochs_min']), int(epochs) - int(ap['epochs_step']))} (divergencia)")
        except Exception:
            pass

        # 4. Explicit model candidates (hypothesis-driven; otherwise rejected).
        try:
            for opt in (model_options or []):
                if not isinstance(opt, dict) or not opt.get("to_model"):
                    continue
                _push({"kind": "model", "dataset": declared.get("dataset"),
                       "from_model": declared.get("model"), "to_model": opt.get("to_model"),
                       "total_rounds": int(total) if total else None,
                       **({"expected_gain": opt["expected_gain"]} if opt.get("expected_gain") else {})},
                      "divergence" if diverged else "model_candidate",
                      f"Probar {opt.get('to_model')} (requiere hipótesis de ganancia)")
        except Exception:
            pass
    except Exception as e:
        logging.warning(f"exec-interpreter | architecture policies failed: {e}")
    return suggestions, rejected


def _previous_of(change: dict, declared: dict) -> Any:
    """Current value before the change (for the audit trail)."""
    try:
        kind = change.get("kind")
        if kind == "rounds":
            return declared.get("total_rounds")
        if kind == "epochs":
            return declared.get("epochs")
        if kind == "model":
            return declared.get("model")
        if kind == "aggregator":
            return declared.get("aggregation")
        if kind in ("federation",):
            return declared.get("federation")
        if kind in ("topology", "nodes"):
            return declared.get("topology")
        if kind == "policy":
            return declared.get("sar_neighbor_policy")
        if kind == "batch":
            return declared.get("batch_size")
    except Exception:
        pass
    return None


_FALLBACK_SUGGEST_MAP: dict[str, Any] = {
    "byzantine_aggregator": "Median",
    "straggler_aggregator": "TrimmedMean",
    "topology_min_neighbors": 2,
    "topology_target": "Ring",
    "pressure_batch_size": 8,
    "low_recall_federation": "CFL",
    "federation_gain_hypothesis": {"low": 0.0, "mid": 0.01, "high": 0.03},
}


def _suggest_num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def suggest_from_findings(
    findings: list[dict] | None,
    measured: dict[str, Any] | None = None,
    declared: dict[str, Any] | None = None,
    thresholds: dict[str, Any] | None = None,
    cooldown: dict[str, Any] | None = None,
    scenario_completed: bool = False,
    audit_records: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Fase 2 (Copilot): finding → actionable suggestion, LLM-free.

    Maps each typed finding to a candidate change (hot or structural), routes
    it through ``cost_model.estimate_cost`` + ``decide`` and returns
    ``(suggestions, rejected)``. A finding with no applicable suggestion is
    simply not suggested (never pauses). Every suggestion carries a stable
    ``id``, ``class`` (hot|structural), ``finding_ids``, ``nodes``, ``reason``,
    ``evidence[]`` and the ``change``. Structural changes need an explicit
    gain hypothesis (``expected_gain``) to be worthwhile; otherwise they land
    in ``rejected`` with "no conviene". Never raises, never invents.
    """
    from nebula.frontend.cost_model import classify_change, decide, estimate_cost

    suggestions: list[dict] = []
    rejected: list[dict] = []
    if scenario_completed:
        return [], []
    try:
        th = dict(load_thresholds())
        if thresholds:
            th.update(thresholds)
        sm = dict(_FALLBACK_SUGGEST_MAP)
        sm.update(th.get("suggest_from_findings") or {})
        items = [f for f in (findings or []) if isinstance(f, dict)]
        measured = measured if isinstance(measured, dict) else {}
        declared = declared if isinstance(declared, dict) else {}
        cd = cooldown if isinstance(cooldown, dict) else {}
        covered: set[str] = set()

        def _push(change: dict, trigger: str, title: str, finding: dict,
                  hypothesis: dict | None = None) -> None:
            fid = finding.get("id")
            if fid:
                covered.add(str(fid))
            chg = dict(change)
            if hypothesis:
                chg["expected_gain"] = hypothesis
            try:
                cost = estimate_cost(chg, measured,
                                     {"avg_neighbors": declared.get("avg_neighbors"),
                                      "n_nodes": declared.get("n_nodes")},
                                     thresholds=th)
                verdict = decide(cost, th)
            except Exception as e:
                rejected.append({"change": change, "trigger": trigger,
                                 "verdict": f"no conviene: error de costo ({e})"})
                return
            cls = classify_change(change)
            ev = list(finding.get("evidence", []) or [])[:10]
            nodes = list(finding.get("nodes", []) or [])
            if verdict.get("worthwhile"):
                kind = change.get("kind")
                action = ("propose_new_simulation" if cls == "structural"
                          else ("select_aggregator" if kind == "aggregator"
                                else "propose_architecture_change"))
                suggestions.append({
                    "id": f"find-{trigger}-{fid or 'x'}",
                    "kind": action,
                    "class": cls,
                    "finding_ids": [str(fid)] if fid else [],
                    "nodes": nodes,
                    "params": {"change": change, "cost": cost, "trigger": trigger,
                               "previous": _previous_of(change, declared)},
                    "title": title,
                    "reason": verdict.get("verdict", "conviene"),
                    "evidence": ev,
                    "severity": finding.get("severity", "info"),
                    "requires_confirm": True,
                    "auto": False,
                })
            else:
                rejected.append({"change": {k: change.get(k) for k in ("kind",) if k in change},
                                 "trigger": trigger, "finding_ids": [str(fid)] if fid else [],
                                 "verdict": verdict.get("verdict", "no conviene")})

        # Cooldown: a rejected hot target must not re-fire within the window.
        byz_target = str(sm.get("byzantine_aggregator", "Median"))
        strag_target = str(sm.get("straggler_aggregator", "TrimmedMean"))
        last_rejected = cd.get("last_rejected_algorithm")

        # The Sprint 3 aggregator policy already emits a hot select_aggregator
        # card for byzantine/straggler; don't duplicate it. Only emit an
        # aggregator suggestion here when the policy is suppressed (target
        # already active or cooldown) so the Copilot map still covers the case.
        agg_covered = False
        try:
            from nebula.frontend.exec_interpreter import suggest_aggregator

            cur_agg = declared.get("aggregation", declared.get("algorithm"))
            agg_sugg, _ = suggest_aggregator(items, cur_agg, declared.get("rounds_completed"),
                                             audit_records, th, scenario_completed)
            agg_covered = agg_sugg is not None
        except Exception:
            agg_covered = False

        for f in items:
            ftype = f.get("type")
            if ftype == "byzantine_suspected":
                if agg_covered:
                    continue
                if last_rejected != byz_target:
                    _push({"kind": "aggregator", "algorithm": byz_target},
                          "byzantine_suspected", f"Cambiar agregador → {byz_target} (robusto)", f)
                else:
                    # Delegate to the existing policy when the alternate target
                    # is the one already rejected.
                    _push({"kind": "aggregator", "algorithm": "Krum"},
                          "byzantine_suspected", "Cambiar agregador → Krum (robusto)", f)
            elif ftype == "straggler":
                # Topology straggler (too few neighbours / star bottleneck) →
                # re-topologize; otherwise a delay-tolerant aggregator.
                nodes = f.get("nodes") or []
                min_nb = int(sm.get("topology_min_neighbors", 2))
                nb_by_node = declared.get("neighbors_by_node") or {}
                if nb_by_node and nodes:
                    topology_bound = all(
                        len(nb_by_node.get(str(n), nb_by_node.get(n, [])) or []) < min_nb
                        for n in nodes
                    )
                elif nb_by_node:
                    topology_bound = all(len(v or []) < min_nb for v in nb_by_node.values())
                else:
                    avg_nb = _suggest_num(declared.get("avg_neighbors"))
                    topology_bound = avg_nb is not None and avg_nb < min_nb
                topo_target = str(sm.get("topology_target", "Ring"))
                if topology_bound and last_rejected != topo_target:
                    topo_hyp = (f.get("hypothesis") or {}) if isinstance(f.get("hypothesis"), dict) else None
                    _push({"kind": "topology", "to_topology": topo_target,
                           "from_topology": declared.get("topology")},
                          "straggler_topology",
                          f"Re-topologizar → {topo_target} (cuello de botella)", f,
                          hypothesis=topo_hyp)
                elif last_rejected != strag_target and not agg_covered:
                    _push({"kind": "aggregator", "algorithm": strag_target},
                          "straggler", f"Cambiar agregador → {strag_target} (tolerante al retraso)", f)
            elif ftype in ("ram_pressure", "cpu_pressure", "thermal_throttle_risk"):
                bs = int(sm.get("pressure_batch_size", 8))
                # Structural (dataloader rebuild): needs a gain hypothesis.
                _push({"kind": "batch", "batch_size": bs},
                      ftype, f"Reducir batch_size a {bs} (reconstruye el dataloader)", f,
                      hypothesis={"low": 0.0, "mid": 0.01, "high": 0.02})
            elif ftype in ("f1_recall_gap", "low_recall"):
                # Only with an explicit IID-low hypothesis; otherwise rejected.
                iid = declared.get("iid")
                hyp = (f.get("hypothesis") or {}) if isinstance(f.get("hypothesis"), dict) else None
                if iid is False and hyp:
                    _push({"kind": "federation", "to_federation": str(sm.get("low_recall_federation", "CFL")),
                           "from_federation": declared.get("federation")},
                          ftype, f"Cambiar federación → {sm.get('low_recall_federation', 'CFL')} "
                                 "(solo con hipótesis)", f, hypothesis=hyp)
                else:
                    if f.get("id"):
                        covered.add(str(f.get("id")))
                    rejected.append({"change": {"kind": "federation"},
                                     "trigger": ftype,
                                     "finding_ids": [str(f.get("id"))] if f.get("id") else [],
                                     "verdict": "no conviene: sin hipótesis explícita de ganancia para "
                                                "cambiar de federación"})
            # Other finding types (accuracy_plateau, over_provisioned_rounds) are
            # handled by suggest_architecture; here they simply don't pause.
    except Exception as e:
        logging.warning(f"exec-interpreter | suggest_from_findings failed: {e}")
    return suggestions, rejected


_VERIFIED_KINDS = frozenset({"select_aggregator", "propose_architecture_change", "resume_from"})

_FALLBACK_VERIFICATION = {"k_rounds": 3}


def due_verifications(
    audit_records: list[dict] | None,
    rounds_completed: int | None,
    k_rounds: int | None = None,
    thresholds: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Post-change checks due at execution round + K (Sprint 7).

    Scans executed records of verifiable kinds; each becomes
    {action_id, kind, executed_round, due_round, status: pending|ready}.
    Round-less records stay pending (never assumed). Never raises.
    """
    try:
        k = k_rounds
        if k is None:
            k = ((thresholds or {}).get("verification") or {}).get(
                "k_rounds", _FALLBACK_VERIFICATION["k_rounds"])
        k = max(1, int(k))
    except (TypeError, ValueError):
        k = 3
    out: list[dict] = []
    try:
        cur = None if rounds_completed is None else int(rounds_completed)
    except (TypeError, ValueError):
        cur = None
    try:
        for rec in audit_records or []:
            if not isinstance(rec, dict) or rec.get("event") != "action_executed":
                continue
            payload = rec.get("payload") or {}
            if payload.get("kind") not in _VERIFIED_KINDS:
                continue
            if payload.get("ok") is not True:
                continue
            try:
                eround = None if payload.get("round") is None else int(payload["round"])
            except (TypeError, ValueError):
                eround = None
            due = None if eround is None else eround + k
            status = "pending" if (cur is None or due is None or cur < due) else "ready"
            out.append({"action_id": payload.get("id"), "kind": payload.get("kind"),
                        "executed_round": eround, "due_round": due, "status": status})
    except Exception:
        pass
    return out


def verify_execution(
    exec_payload: dict[str, Any] | None,
    proposed_cost: dict[str, Any] | None,
    current: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compare projected cost/benefit vs measured values (Sprint 7).

    ``exec_payload``: action_executed payload (round, kind, baseline…).
    ``current``: {accuracy_final, f1_final, rounds_completed}.
    Returns {action_id, kind, projected, measured, within_range|None,
    calibration}. Missing baselines yield explicit notes, never invented
    comparisons. Never raises.
    """
    try:
        exec_payload = exec_payload if isinstance(exec_payload, dict) else {}
        current = current if isinstance(current, dict) else {}
        baseline = exec_payload.get("baseline") or {}
        benefit = (proposed_cost or {}).get("benefit") or {}
        report: dict[str, Any] = {
            "action_id": exec_payload.get("id"), "kind": exec_payload.get("kind"),
            "executed_round": exec_payload.get("round"),
            "rounds_completed": current.get("rounds_completed"),
            "projected": {"benefit": {k: benefit.get(k) for k in ("low", "mid", "high", "basis")}},
            "measured": {}, "within_range": None, "calibration": "",
        }
        try:
            base_acc = None if baseline.get("accuracy") is None else float(baseline["accuracy"])
            cur_acc = None if current.get("accuracy_final") is None else float(current["accuracy_final"])
        except (TypeError, ValueError):
            base_acc, cur_acc = None, None
        if base_acc is None or cur_acc is None:
            report["calibration"] = ("sin baseline en la ejecución: se reporta el valor actual "
                                     "sin comparar banda (registrar baseline en Sprint 7+)")
            report["measured"] = {"accuracy_final": cur_acc}
            return report
        gain = round(cur_acc - base_acc, 4)
        report["measured"] = {"accuracy_final": cur_acc, "baseline_accuracy": base_acc,
                              "actual_gain": gain}
        try:
            low = None if benefit.get("low") is None else float(benefit["low"])
            high = None if benefit.get("high") is None else float(benefit["high"])
        except (TypeError, ValueError):
            low, high = None, None
        if low is None or high is None:
            report["calibration"] = (f"ganancia actual {gain}: banda proyectada desconocida, "
                                     "sin calibrar")
            return report
        within = low <= gain <= high
        report["within_range"] = bool(within)
        if within:
            report["calibration"] = (f"ganancia {gain} dentro de la banda [{low}, {high}]: "
                                     "costo calibrado")
        elif gain < low:
            report["calibration"] = (f"ganancia {gain} bajo la banda [{low}, {high}]: "
                                     "beneficio sobreestimado, ampliar incertidumbre")
        else:
            report["calibration"] = (f"ganancia {gain} sobre la banda [{low}, {high}]: "
                                     "beneficio subestimado, revisar tendencia")
        return report
    except Exception as e:
        return {"action_id": None, "kind": None, "projected": {}, "measured": {},
                "within_range": None, "calibration": f"verificación fallida: {e}"}


def suggest_aggregator(
    findings: list[dict] | None,
    current: Any,
    rounds_completed: int | None = None,
    audit_records: list[dict] | None = None,
    thresholds: dict[str, Any] | None = None,
    scenario_completed: bool = False,
) -> tuple[dict | None, dict[str, Any]]:
    """Deterministic aggregator policy (Sprint 3).

    Priority: byzantine → Krum, stragglers → TrimmedMean, clean → FedAvg.
    Returns ``(suggestion|None, cooldown)``. ``suggestion`` is a ready-to-
    propose ``select_aggregator`` action (``params`` carry ``trigger`` +
    ``previous`` so the audit keeps metric/algorithm trail); it always
    requires human confirmation. Suppressed (None) when the target already
    runs, the current algorithm is unknown, the cooldown window is active, or
    the scenario already finished (a hot-swap with no rounds left is
    meaningless — continue via resume_from instead).
    Never raises, never invents (roadmap rule 5).
    """
    th = dict(_FALLBACK_THRESHOLDS)
    try:
        if thresholds:
            th.update(thresholds)
    except Exception:
        pass
    cd = cooldown_state(audit_records, rounds_completed, th)
    try:
        policy = th.get("aggregator_policy") or {}
        byz_target = str(policy.get("byzantine_target", "Krum"))
        strag_target = str(policy.get("straggler_target", "TrimmedMean"))
        clean_target = str(policy.get("clean_target", "FedAvg"))
    except Exception:
        byz_target, strag_target, clean_target = "Krum", "TrimmedMean", "FedAvg"
    for t in (byz_target, strag_target, clean_target):
        if t not in SUPPORTED_AGGREGATORS:
            return None, cd
    if current not in SUPPORTED_AGGREGATORS:
        return None, cd
    if scenario_completed:
        return None, cd
    if cd.get("in_cooldown"):
        return None, cd
    items = [f for f in (findings or []) if isinstance(f, dict)]
    byz = [f for f in items if f.get("type") == "byzantine_suspected"]
    strag = [f for f in items if f.get("type") == "straggler"]
    target: str | None = None
    trigger_finding: dict | None = None
    if byz:
        target, trigger_finding = byz_target, byz[0]
    elif strag:
        target, trigger_finding = strag_target, strag[0]
    else:
        adverse = [f for f in items if f.get("type") in _ADVERSE_FINDING_TYPES]
        if not adverse and current != clean_target:
            target = clean_target
    if target is None or target == current:
        return None, cd
    if cd.get("last_rejected_algorithm") == target:
        cd = dict(cd, in_cooldown=True, remaining_rounds=max(cd.get("remaining_rounds", 0), 1))
        return None, cd
    trigger = trigger_finding.get("type", "clean_conditions") if trigger_finding else "clean_conditions"
    metric = trigger_finding.get("metric", "accuracy") if trigger_finding else "accuracy"
    evidence = list(trigger_finding.get("evidence", []) or []) if trigger_finding else []
    summary = trigger_finding.get("summary", "clean homogeneous conditions") if trigger_finding else \
        "clean homogeneous conditions"
    return {
        "id": f"agg-{target}-{trigger}",
        "kind": "select_aggregator",
        "params": {"node": "all", "algorithm": target, "trigger": f"{trigger}@{metric}", "previous": current},
        "title": f"Cambiar {current} → {target}",
        "reason": summary,
        "evidence": evidence[:10],
        "severity": trigger_finding.get("severity", "info") if trigger_finding else "info",
        "requires_confirm": True,
        "auto": False,
    }, cd


def _metric(digest: dict, name: str) -> dict | None:
    m = (digest.get("metrics") or {}).get(name)
    return m if isinstance(m, dict) else None


def _points(series: dict | None, name: str) -> list[tuple[int, float]]:
    if not series:
        return []
    pts = series.get(name) or []
    try:
        clean = [(int(r), float(v)) for r, v in pts]
    except (TypeError, ValueError):
        return []
    return sorted(clean)


def _last_round(series: dict | None, entry: dict | None) -> int | None:
    """Newest round with data: from series, else best.round as fallback."""
    try:
        if series:
            rounds = [r for pts in series.values() for r, _ in pts]
            if rounds:
                return int(max(rounds))
    except (TypeError, ValueError):
        pass
    try:
        if entry and (entry.get("best") or {}).get("round") is not None:
            return int(entry["best"]["round"])
    except (TypeError, ValueError):
        pass
    return None


def _ev(metric: str, round_: int | None, value: Any, node_id: Any = None, detail: str = "") -> dict:
    try:
        value = None if value is None else float(value)
    except (TypeError, ValueError):
        value = None
    try:
        node_id = None if node_id is None else int(node_id)
    except (TypeError, ValueError):
        node_id = None
    return {"metric": metric, "round": round_, "node_id": node_id, "value": value, "detail": detail}


def analyze_digest(
    digest: dict[str, Any] | None,
    series: dict[str, list[tuple[int, float]]] | None = None,
    thresholds: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Turn a metrics digest (+ optional per-round series) into findings.

    Pure function: no I/O, no LLM, deterministic. Rules without sufficient
    data are skipped (never estimated silently).
    """
    if not isinstance(digest, dict):
        return []
    th = dict(_FALLBACK_THRESHOLDS)
    try:
        if thresholds:
            th.update(thresholds)
    except Exception:
        pass
    try:
        min_rounds = max(1, int(th.get("min_rounds", 4)))
    except (TypeError, ValueError):
        min_rounds = 4
    findings: list[dict[str, Any]] = []

    acc = _metric(digest, "accuracy")
    f1 = _metric(digest, "f1_macro")
    rec = _metric(digest, "recall_macro")
    ram = _metric(digest, "ram_percent")
    cpu = _metric(digest, "cpu_percent")
    tmp = _metric(digest, "cpu_temp")
    tmp_est = _metric(digest, "cpu_temp_est")

    acc_pts = _points(series, "accuracy")
    last_rnd = _last_round(series, acc)

    # 1. accuracy plateau (history rule).
    try:
        k = max(2, int(th.get("plateau_k", 3)))
        eps = float(th.get("plateau_eps", 0.005))
    except (TypeError, ValueError):
        k, eps = 3, 0.005
    if acc is not None and len(acc_pts) >= max(k, min_rounds):
        window = acc_pts[-k:]
        spread = max(v for _, v in window) - min(v for _, v in window)
        if spread < eps:
            ev = [_ev("accuracy", r, v, detail="global mean") for r, v in window]
            if acc.get("converged_at") is not None:
                ev.append(_ev("accuracy", acc.get("converged_at"), None, detail="converged_at (digest)"))
            findings.append({
                "id": "accuracy_plateau--accuracy",
                "type": "accuracy_plateau",
                "severity": "info",
                "metric": "accuracy",
                "nodes": [],
                "evidence": ev,
                "summary": (
                    f"accuracy plateau: range {spread:.4f} < {eps} over last {k} "
                    f"rounds (r{window[0][0]}-r{window[-1][0]})"
                ),
            })

    # 2. F1 vs accuracy gap (possible class imbalance). Point rule.
    try:
        gap_th = float(th.get("f1_accuracy_gap", 0.10))
    except (TypeError, ValueError):
        gap_th = 0.10
    if acc is not None and f1 is not None and acc.get("final") is not None and f1.get("final") is not None:
        try:
            gap = abs(float(acc["final"]) - float(f1["final"]))
        except (TypeError, ValueError):
            gap = None
        if gap is not None and gap > gap_th:
            findings.append({
                "id": "f1_recall_gap--f1_macro",
                "type": "f1_recall_gap",
                "severity": "warning",
                "metric": "f1_macro",
                "nodes": [],
                "evidence": [
                    _ev("accuracy", last_rnd, acc["final"], detail="final (global mean)"),
                    _ev("f1_macro", last_rnd, f1["final"], detail="final (global mean)"),
                ],
                "summary": (
                    f"F1/accuracy gap {gap:.4f} > {gap_th}: accuracy {acc['final']} vs "
                    f"F1 macro {f1['final']} — possible class imbalance"
                ),
            })

    # 3. Low macro recall. Point rule.
    try:
        min_rec = float(th.get("min_recall", 0.60))
    except (TypeError, ValueError):
        min_rec = 0.60
    if rec is not None and rec.get("final") is not None:
        try:
            rec_final = float(rec["final"])
        except (TypeError, ValueError):
            rec_final = None
        if rec_final is not None and rec_final < min_rec:
            ev = [_ev("recall_macro", last_rnd, rec_final, detail="final (global mean)")]
            try:
                best = rec.get("best") or {}
                if best.get("round") is not None and best.get("value") is not None:
                    ev.append(_ev("recall_macro", int(best["round"]), float(best["value"]), detail="best"))
            except (TypeError, ValueError):
                pass
            findings.append({
                "id": "low_recall--recall_macro",
                "type": "low_recall",
                "severity": "warning",
                "metric": "recall_macro",
                "nodes": [],
                "evidence": ev,
                "summary": f"low macro recall: final {rec_final:.4f} < {min_rec}",
            })

    # 4-5. Sustained RAM / CPU pressure (history rules).
    try:
        n_hist = max(1, int(th.get("pressure_min_rounds", 3)))
    except (TypeError, ValueError):
        n_hist = 3
    need_hist = max(n_hist, min_rounds)
    for entry, sname, ftype, hi_key, crit_key in (
        (ram, "ram_percent", "ram_pressure", "ram_high", "ram_critical"),
        (cpu, "cpu_percent", "cpu_pressure", "cpu_high", "cpu_critical"),
    ):
        pts = _points(series, sname)
        if entry is None or len(pts) < need_hist:
            continue
        try:
            hi = float(th.get(hi_key, 85.0))
            crit = float(th.get(crit_key, 95.0))
        except (TypeError, ValueError):
            hi, crit = 85.0, 95.0
        window = pts[-n_hist:]
        mean_w = sum(v for _, v in window) / len(window)
        if mean_w > hi:
            sev = "critical" if mean_w > crit else "warning"
            findings.append({
                "id": f"{ftype}--{sname}",
                "type": ftype,
                "severity": sev,
                "metric": sname,
                "nodes": [],
                "evidence": [_ev(sname, r, v, detail="global mean") for r, v in window],
                "summary": (
                    f"sustained {sname} pressure: mean {mean_w:.1f}% over last "
                    f"{n_hist} rounds (r{window[0][0]}-r{window[-1][0]}) > {hi}%"
                ),
            })

    # 6. Thermal throttle risk. Point rule (sensor preferred, estimate labeled).
    try:
        t_hi = float(th.get("temp_high", 80.0))
        t_crit = float(th.get("temp_critical", 90.0))
        t_est_hi = float(th.get("temp_est_high", 85.0))
    except (TypeError, ValueError):
        t_hi, t_crit, t_est_hi = 80.0, 90.0, 85.0
    if tmp is not None and tmp.get("final") is not None:
        try:
            t_final = float(tmp["final"])
        except (TypeError, ValueError):
            t_final = None
        if t_final is not None and t_final > t_hi:
            findings.append({
                "id": "thermal_throttle_risk--cpu_temp",
                "type": "thermal_throttle_risk",
                "severity": "critical" if t_final > t_crit else "warning",
                "metric": "cpu_temp",
                "nodes": [],
                "evidence": [_ev("cpu_temp", last_rnd, t_final, detail="final, source: sensor")],
                "summary": f"thermal risk (sensor): CPU {t_final:.1f} °C > {t_hi} °C",
            })
    elif tmp_est is not None and tmp_est.get("final") is not None:
        try:
            te_final = float(tmp_est["final"])
        except (TypeError, ValueError):
            te_final = None
        if te_final is not None and te_final > t_est_hi:
            findings.append({
                "id": "thermal_throttle_risk--cpu_temp_est",
                "type": "thermal_throttle_risk",
                "severity": "warning",
                "metric": "cpu_temp_est",
                "nodes": [],
                "evidence": [_ev("cpu_temp_est", last_rnd, te_final,
                                 detail="final, source: estimated (ARX), sensor unavailable")],
                "summary": f"thermal risk (estimated): CPU ~{te_final:.1f} °C > {t_est_hi} °C",
            })

    # 7. Stragglers (digest-level, ratio-gated). Point rule.
    try:
        s_ratio = float(th.get("straggler_ratio", 1.5))
    except (TypeError, ValueError):
        s_ratio = 1.5
    try:
        stragglers = (digest.get("nodes") or {}).get("stragglers") or []
    except Exception:
        stragglers = []
    flagged = []
    for s in stragglers:
        try:
            if isinstance(s, dict) and float(s.get("ratio_vs_median", 0)) >= s_ratio:
                flagged.append(s)
        except (TypeError, ValueError):
            continue
    if flagged:
        nodes = sorted({int(s["node_id"]) for s in flagged if s.get("node_id") is not None})
        findings.append({
            "id": "straggler--round_time_s",
            "type": "straggler",
            "severity": "warning",
            "metric": "round_time_s",
            "nodes": nodes,
            "evidence": [
                _ev("round_time_s", None, s.get("mean_round_time_s"), node_id=s.get("node_id"),
                    detail=f"mean round time, {s.get('ratio_vs_median')}x vs median")
                for s in flagged
            ],
            "summary": f"straggler nodes {nodes}: round time >= {s_ratio}x median",
        })

    # 8. Byzantine suspicion (last-round outliers proxy). Point rule.
    try:
        byz_metrics = th.get("byzantine_metrics", ["accuracy", "f1_macro"]) or []
    except Exception:
        byz_metrics = ["accuracy", "f1_macro"]
    for bname in byz_metrics:
        b_entry = _metric(digest, bname)
        if not b_entry:
            continue
        outs = b_entry.get("outliers") or []
        if not outs:
            continue
        nodes = sorted({int(o["node_id"]) for o in outs if isinstance(o, dict) and o.get("node_id") is not None})
        findings.append({
            "id": f"byzantine_suspected--{bname}",
            "type": "byzantine_suspected",
            "severity": "warning",
            "metric": bname,
            "nodes": nodes,
            "evidence": [
                _ev(bname, last_rnd, o.get("value"), node_id=o.get("node_id"),
                    detail=f"last-round outlier, gap vs median {o.get('gap_vs_median')}")
                for o in outs if isinstance(o, dict)
            ],
            "summary": f"byzantine suspected on {bname}: outlier nodes {nodes} in last round",
        })

    # 9. Over-provisioned rounds (history rule).
    try:
        extra_need = max(1, int(th.get("overprovision_extra_rounds", 3)))
    except (TypeError, ValueError):
        extra_need = 3
    try:
        scenario = digest.get("scenario") or {}
        completed = scenario.get("rounds_completed")
        total = scenario.get("rounds_total")
        completed = None if completed is None else int(completed)
    except (TypeError, ValueError):
        completed, total = None, None
    if acc is not None and acc.get("converged_at") is not None and completed is not None:
        try:
            conv = int(acc["converged_at"])
        except (TypeError, ValueError):
            conv = None
        if conv is not None and completed - conv >= extra_need and completed >= min_rounds:
            findings.append({
                "id": "over_provisioned_rounds--accuracy",
                "type": "over_provisioned_rounds",
                "severity": "info",
                "metric": "accuracy",
                "nodes": [],
                "evidence": [
                    _ev("accuracy", conv, None, detail="converged_at (digest)"),
                    _ev("accuracy", completed, acc.get("final"), detail="final at completed round"),
                    _ev("accuracy", None, total, detail="rounds_total (declared)"),
                ],
                "summary": (
                    f"over-provisioned: converged at r{conv}, already at r{completed} "
                    f"({completed - conv} extra rounds)"
                ),
            })

    return findings
