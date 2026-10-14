"""Backfill de métricas derivadas a event files de TensorBoard.

Las corridas viejas no tienen tags ``W-TEMP/*`` ni ``E-Energy/*`` (el Reporter
los loguea desde esta versión), así que no aparecen en Real-time metrics
(TensorBoard). Este script calcula esas series con el mismo modelo ARX y las
anexa a los event files existentes para que salgan como las demás.

Idempotente: solo escribe rondas sin tags crudos (no duplica). Uso::

    PYTHONPATH=/nebula .venv/bin/python -m nebula.frontend.metrics.backfill \\
        /nebula/app/logs <scenario> [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import re

from nebula.core.utils.thermal_energy import (
    CPU_TEMP_EST_TAG,
    ENERGY_EST_TAG,
    ENERGY_TOTAL_TAG,
    GPU_TEMP_EST_TAG,
)

from .providers import (
    GPU_TEMP_EST_RE,
    TAG_MAP,
    TensorBoardProvider,
)

DERIVED_METRICS = ("cpu_temp_est", "gpu_temp_est", "energy_total", "energy_estimated")
DERIVED_TAG = {
    "cpu_temp_est": CPU_TEMP_EST_TAG,
    "gpu_temp_est": GPU_TEMP_EST_TAG,
    "energy_total": ENERGY_TOTAL_TAG,
    "energy_estimated": ENERGY_EST_TAG,
}


def _raw_covered(provider: TensorBoardProvider, acc, tags: list[str]) -> set[tuple[str, int]]:
    """Rondas ya cubiertas por tags crudos W-TEMP/E-Energy (no reescribir)."""
    covered: set[tuple[str, int]] = set()
    boundaries = provider._around_boundaries(acc)
    for tag in tags:
        mapped = TAG_MAP.get(tag)
        metric: str | None = None
        strategy = "around_map"
        if mapped and mapped[0] in DERIVED_METRICS:
            metric, strategy = mapped[0], mapped[1]
        elif re.fullmatch(GPU_TEMP_EST_RE, tag):
            metric = "gpu_temp_est"
        if metric is None:
            continue
        try:
            points = acc.Scalars(tag)
        except Exception:
            continue
        for p in points:
            rnd = provider._resolve_round(strategy, p.step, boundaries)
            if rnd is not None:
                covered.add((metric, int(rnd)))
    return covered


def _round_steps(provider: TensorBoardProvider, acc) -> dict[int, int]:
    """Primer step (segundos) de cada ronda según A-Round.

    Los puntos derivados deben loguearse con steps en escala de segundos para
    que ``around_map`` los atribuya a su ronda; usar el nº de ronda como step
    los deja fuera de toda ventana (se descartan).
    """
    bounds = provider._around_boundaries(acc)
    steps: dict[int, int] = {}
    for bstep, rnd in sorted(bounds):
        steps.setdefault(int(rnd), int(bstep))
    return steps


def _parse_ts(ts: str) -> float:
    try:
        return datetime.datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return datetime.datetime.now(tz=datetime.UTC).timestamp()


def backfill_scenario(logs_root: str, scenario: str, dry_run: bool = False) -> dict:
    """Anexa series derivadas faltantes. Devuelve resumen por participante."""
    from tensorboardX import SummaryWriter

    provider = TensorBoardProvider(logs_root)
    if not provider.exists(scenario):
        raise FileNotFoundError(f"No metrics directory for scenario '{scenario}'")
    res = provider.load(scenario)
    summary: dict[str, dict[str, int]] = {}
    for node_id in provider.participants(scenario):
        path = os.path.join(provider.scenario_dir(scenario), f"participant_{node_id}")
        from tensorboard.backend.event_processing import event_accumulator as ea_mod
        from tensorboard.backend.event_processing.event_accumulator import EventAccumulator

        acc = EventAccumulator(
            path,
            size_guidance={
                ea_mod.IMAGES: 0,
                ea_mod.AUDIO: 0,
                ea_mod.HISTOGRAMS: 0,
                ea_mod.COMPRESSED_HISTOGRAMS: 0,
                ea_mod.TENSORS: 0,
            },
        )
        acc.Reload()
        try:
            tags = acc.Tags()["scalars"]
        except Exception as e:
            summary[str(node_id)] = {"written": 0, "skipped": 0, "error": str(e)}
            continue
        covered = _raw_covered(provider, acc, tags)
        missing = [
            row
            for row in res.rows
            if row["node_id"] == node_id
            and row["metric"] in DERIVED_METRICS
            and (row["metric"], int(row["round"])) not in covered
        ]
        if not missing:
            summary[str(node_id)] = {"written": 0, "skipped": len(covered)}
            continue
        if dry_run:
            summary[str(node_id)] = {"written": 0, "dry_run_pending": len(missing)}
            continue
        # tensorboardX exige ruta absoluta para el FileWriter.
        writer = SummaryWriter(log_dir=os.path.abspath(path))
        try:
            rsteps = _round_steps(provider, acc)
            for row in sorted(missing, key=lambda r: (r["round"], r["metric"])):
                step = rsteps.get(int(row["round"]), int(row["step"]))
                writer.add_scalar(
                    DERIVED_TAG[row["metric"]],
                    float(row["value"]),
                    global_step=step,
                    walltime=_parse_ts(row["ts"]),
                )
        finally:
            writer.close()
        summary[str(node_id)] = {"written": len(missing), "skipped": len(covered)}
        logging.info(f"backfill | participant_{node_id}: wrote {len(missing)} points")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill W-TEMP/E-Energy a event files viejos")
    parser.add_argument("logs_root", help="Ej: /nebula/app/logs")
    parser.add_argument("scenario", help="Ej: nebula_DFL_2026_09_30_01_20_39")
    parser.add_argument("--dry-run", action="store_true", help="Solo reporta lo que escribiría")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    summary = backfill_scenario(args.logs_root, args.scenario, dry_run=args.dry_run)
    for node, info in summary.items():
        logging.info(f"participant_{node}: {info}")


if __name__ == "__main__":
    main()
