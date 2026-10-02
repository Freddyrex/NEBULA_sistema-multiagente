"""NEBULA metrics layer (SMAC improvements §4): canonical schema + providers.

Canonical row (long format, one measurement per row)::

    {"metric": "accuracy", "scope": "node", "node_id": 1, "round": 3,
     "value": 0.81, "step": 3, "ts": "2026-09-28T01:02:49Z"}

Rules (§4.2):

- ``metric`` is lowercase snake_case: ``accuracy``, ``loss``, ``round_time_s``,
  ``bytes_sent``, ``bytes_received``.
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
    "accuracy": {"unit": "ratio", "higher_is_better": True},
    "loss": {"unit": "nats", "higher_is_better": False},
    "round_time_s": {"unit": "s", "higher_is_better": False},
    "bytes_sent": {"unit": "MB", "higher_is_better": False},
    "bytes_received": {"unit": "MB", "higher_is_better": False},
}

#: TensorBoard tag -> (canonical metric, round strategy).
#: ``step_is_round``: the tag is logged once per round with step == round
#: (e.g. ``Train/Accuracy`` steps 0..R-1). ``around_map``: batch-level points
#: whose round is resolved through the ``A-Round`` step->round boundaries.
TAG_MAP: dict[str, tuple[str, str]] = {
    "Train/Accuracy": ("accuracy", "step_is_round"),
    "Train/Loss": ("loss", "around_map"),
    "X-Network/Network (MB sent)": ("bytes_sent", "around_map"),
    "X-Network/Network (MB received)": ("bytes_received", "around_map"),
}

AROUND_TAG = "A-Round"


def _to_iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()


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
        from tensorboard.backend.event_processing.event_accumulator import EventAccumulator
        from tensorboard.backend.event_processing import event_accumulator as ea_mod

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
            boundaries = self._around_boundaries(acc)
            loss_times: dict[int, list[float]] = {}
            for tag in tags:
                mapped = TAG_MAP.get(tag)
                if not mapped:
                    continue
                metric, strategy = mapped
                try:
                    points = acc.Scalars(tag)
                except Exception:
                    continue
                for p in points:
                    rnd = self._resolve_round(strategy, p.step, boundaries)
                    if rnd is None:
                        continue
                    result.rows.append(
                        {
                            "metric": metric,
                            "scope": "node",
                            "node_id": node_id,
                            "round": rnd,
                            "value": float(p.value),
                            "step": int(p.step),
                            "ts": _to_iso(p.wall_time),
                        }
                    )
                    if metric == "loss":
                        loss_times.setdefault(rnd, []).append(p.wall_time)
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
        if not result.rows:
            result.warnings.append(
                f"Scenario '{scenario}' registered no mappable metrics "
                f"(mapped tags: {sorted(TAG_MAP)})"
            )
        return result

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
