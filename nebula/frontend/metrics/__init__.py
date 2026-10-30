"""Capa de métricas SMAC (improvements §4): provider, servicio y digest."""

from .digest import build_digest
from .providers import METRIC_META, TensorBoardProvider
from .service import MetricsService, check_scenario_id
from .thermal_energy import (
    CPU_TEMP_EST_TAG,
    DEFAULT_ARX_CPU,
    DEFAULT_ARX_GPU,
    DEFAULT_ENERGY_W,
    ENERGY_EST_TAG,
    ENERGY_TOTAL_TAG,
    GPU_TEMP_EST_TAG,
    energy_mse,
    estimate_energy_physical,
    estimate_energy_total,
    gpu_temp_est_tag,
    predict_cpu_temp_arx,
    predict_gpu_temp_arx,
)

__all__ = [
    "CPU_TEMP_EST_TAG",
    "DEFAULT_ARX_CPU",
    "DEFAULT_ARX_GPU",
    "DEFAULT_ENERGY_W",
    "ENERGY_EST_TAG",
    "ENERGY_TOTAL_TAG",
    "GPU_TEMP_EST_TAG",
    "METRIC_META",
    "MetricsService",
    "TensorBoardProvider",
    "build_digest",
    "check_scenario_id",
    "energy_mse",
    "estimate_energy_physical",
    "estimate_energy_total",
    "gpu_temp_est_tag",
    "predict_cpu_temp_arx",
    "predict_gpu_temp_arx",
]
