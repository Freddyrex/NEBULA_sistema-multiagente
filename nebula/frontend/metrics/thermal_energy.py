"""Compat: re-exporta el modelo canónico de ``nebula.core.utils.thermal_energy``.

Se mantiene este módulo para no romper imports existentes
(``nebula.frontend.metrics.thermal_energy``).
"""

from nebula.core.utils.thermal_energy import (  # noqa: F401
    C_BASE_W,
    CPU_TEMP_EST_TAG,
    DEFAULT_ARX_CPU,
    DEFAULT_ARX_GPU,
    DEFAULT_ENERGY_W,
    ENERGY_EST_TAG,
    ENERGY_TOTAL_TAG,
    GPU_TEMP_EST_TAG,
    JOULES_PER_KWH,
    P_CPU_MAX_W,
    TEMP_MAX,
    TEMP_MIN,
    TEMP_SEED,
    clamp_temp,
    cpu_power_from_util,
    energy_mse,
    estimate_energy_physical,
    estimate_energy_total,
    gpu_temp_est_tag,
    predict_cpu_temp_arx,
    predict_gpu_temp_arx,
)
