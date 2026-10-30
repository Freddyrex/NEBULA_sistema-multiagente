"""Modelos analíticos de temperatura estimada y consumo energético.

Ubicación compartida (sin dependencias de frontend/addons) para que tanto el
``Reporter`` (logueo en vivo a TensorBoard) como el ``TensorBoardProvider``
(backfill de corridas viejas + API/SMAC) usen las mismas ecuaciones y tags.

1. ARX térmico CPU/GPU (un paso ``k -> k+1`` por tick/ronda)::

    T_cpu[k+1] = a1*T_cpu[k] + b1*U_cpu[k] + b2*U_ram[k] + b3*U_disk[k] + b4*N_io[k]
    T_gpu[k+1] = a2*T_gpu[k] + g1*U_gpu[k] + g2*P_gpu[k] + g3*f_gpu[k] - g4*v_fan[k]

2. Consumo global por ronda federada (lineal multivariante, kWh)::

    E_total = w0 + w1*dt + w2*S + w3*M + w4*Ucpu_bar + w5*Ugpu_bar + w6*P_bar + w7*B_net

3. Validación física por integración discreta (kWh)::

    E_est = sum_k (P_gpu[k] + f(U_cpu[k]) + C_base) * d_tau / 3.6e6
    f(U_cpu) = P_cpu_max * U_cpu/100
    L = (E_total - E_est)^2

Unidades: T en °C, U en %, RAM/Disk en %, N_io en MB, P en W, f_gpu en MHz,
v_fan en %, dt en s, S en muestras, M en MB, B_net en MB, E en kWh.

Sin telemetría GPU el dispositivo usa fallback CPU: la ARX de GPU se alimenta
con U_cpu y P_cpu = f(U_cpu); la energía ya es CPU-only (P_gpu = 0).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# --- Tags de TensorBoard (visibles en Scalars / Time Series del dashboard) ---
# Nombres solo ASCII [A-Za-z0-9_/.-]: tensorboardX sanea (°), espacios y
# paréntesis, así que los tags deben ser estables ante ese saneado para que
# Reporter (writer nativo), backfill (tensorboardX) y provider coincidan.
CPU_TEMP_EST_TAG = "W-TEMP/CPU_estimated_C"
#: Fallback sin GPU (sin índice).
GPU_TEMP_EST_TAG = "W-TEMP/GPU_estimated_C"
ENERGY_TOTAL_TAG = "E-Energy/Total_kWh"
ENERGY_EST_TAG = "E-Energy/Estimated_kWh"


def gpu_temp_est_tag(idx: int) -> str:
    """Tag por GPU: ``W-TEMP/GPU{i}_estimated_C``."""
    return f"W-TEMP/GPU{int(idx)}_estimated_C"


# --- Parámetros por defecto (recalibrables) ---------------------------------
# T_cpu[k+1] = a1*T + b1*Ucpu + b2*Uram + b3*Udisk + b4*Nio
DEFAULT_ARX_CPU: dict[str, float] = {
    "a1": 0.88,
    "b1": 0.10,  # U_cpu global (%)
    "b2": 0.02,  # U_ram (%)
    "b3": 0.01,  # U_disk (%)
    "b4": 0.05,  # N_io (MB)
}

# T_gpu[k+1] = a2*T + g1*Ugpu + g2*P + g3*f - g4*vfan
DEFAULT_ARX_GPU: dict[str, float] = {
    "a2": 0.85,
    "g1": 0.08,  # U_gpu (%)
    "g2": 0.05,  # P_gpu (W)
    "g3": 0.002,  # f_gpu (MHz)
    "g4": 0.03,  # v_fan (%)
}

# E_total = w0 + w1*dt + w2*S + w3*M + w4*Ucpu + w5*Ugpu + w6*P + w7*Bnet (kWh)
DEFAULT_ENERGY_W: dict[str, float] = {
    "w0": 0.0005,  # base por ronda (kWh)
    "w1": 4.2e-05,  # ~150 W medios -> kWh por segundo
    "w2": 1.0e-08,  # por muestra
    "w3": 1.0e-06,  # por MB de modelo
    "w4": 2.0e-05,  # por punto de U_cpu (%)
    "w5": 3.0e-05,  # por punto de U_gpu (%)
    "w6": 1.0e-05,  # por W medio GPU
    "w7": 1.0e-07,  # por MB de red
}

# Integración física (§6.2: `device_args` > env > default; 95W solo x86).
# En ARM/RPi calibrar `cpu_tdp_w` (95W sobreestima 6-19×).
P_CPU_MAX_W = _env_float("NEBULA_CPU_TDP_W", 95.0)  # para f(U_cpu) = P_CPU_MAX * U_cpu/100
C_BASE_W = _env_float("NEBULA_BASE_POWER_W", 15.0)  # RAM + disco + fuga
JOULES_PER_KWH = 3_600_000.0
TEMP_MIN, TEMP_MAX = _env_float("NEBULA_TEMP_MIN_C", 20.0), _env_float("NEBULA_TEMP_MAX_C", 105.0)
TEMP_SEED = _env_float("NEBULA_TEMP_SEED_C", 45.0)  # ancla inicial cuando no hay lectura real todavía
TEMP_LIMITS_C = (TEMP_MIN, TEMP_MAX)


def validate_arx_params(params: dict[str, float], kind: str = "cpu") -> None:
    """Valida `0 < a1/a2 < 1` (§6.1); falla alto ante pesos no físicos."""
    key = "a1" if kind == "cpu" else "a2"
    try:
        a = float(params[key])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"ARX {kind}: falta/inválido {key}: {e}") from e
    if not 0.0 < a < 1.0:
        raise ValueError(f"ARX {kind}: {key}={a} fuera de (0,1) — recalibrar")


def clamp_temp(value: float) -> float:
    """Recorta a un rango físico plausible de silicio (con warning, §6.2)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return TEMP_MIN
    clamped = max(TEMP_MIN, min(TEMP_MAX, v))
    if clamped != v:
        logger.warning("Temperatura %.2f°C fuera de [%.1f, %.1f]: clamp a %.2f", v, TEMP_MIN, TEMP_MAX, clamped)
    return clamped


def cpu_power_from_util(u_cpu: float, p_max: float = P_CPU_MAX_W) -> float:
    """Interpola U_cpu (%) -> potencia empírica (W)."""
    try:
        u = max(0.0, min(100.0, float(u_cpu)))
    except (TypeError, ValueError):
        u = 0.0
    return float(p_max) * u / 100.0


def predict_cpu_temp_arx(
    t_prev: float,
    u_cpu: float = 0.0,
    u_ram: float = 0.0,
    u_disk: float = 0.0,
    n_io_mb: float = 0.0,
    params: dict[str, float] | None = None,
) -> float:
    """Un paso ARX de CPU. Sin sensor ambiente: la inercia ``a1*T[k]`` lo absorbe."""
    p = params or DEFAULT_ARX_CPU
    if "a1" in p:
        validate_arx_params(p, "cpu")
    nxt = (
        p["a1"] * float(t_prev or 0.0)
        + p["b1"] * float(u_cpu or 0.0)
        + p["b2"] * float(u_ram or 0.0)
        + p["b3"] * float(u_disk or 0.0)
        + p["b4"] * float(n_io_mb or 0.0)
    )
    return clamp_temp(nxt)


def predict_gpu_temp_arx(
    t_prev: float,
    u_gpu: float = 0.0,
    p_gpu: float = 0.0,
    f_gpu_mhz: float = 0.0,
    v_fan: float = 0.0,
    params: dict[str, float] | None = None,
) -> float:
    """Un paso ARX de GPU. El ventilador resta (disipación activa)."""
    p = params or DEFAULT_ARX_GPU
    if "a2" in p:
        validate_arx_params(p, "gpu")
    nxt = (
        p["a2"] * float(t_prev or 0.0)
        + p["g1"] * float(u_gpu or 0.0)
        + p["g2"] * float(p_gpu or 0.0)
        + p["g3"] * float(f_gpu_mhz or 0.0)
        - p["g4"] * float(v_fan or 0.0)
    )
    return clamp_temp(nxt)


def estimate_energy_total(
    dt_s: float = 0.0,
    sample_size: float = 0.0,
    model_mb: float = 0.0,
    u_cpu_mean: float = 0.0,
    u_gpu_mean: float = 0.0,
    p_gpu_mean: float = 0.0,
    b_net_mb: float = 0.0,
    weights: dict[str, float] | None = None,
) -> float:
    """E_total lineal (kWh). ``S`` y ``M`` a 0 si no hay trustworthiness."""
    w = weights or DEFAULT_ENERGY_W
    e = (
        w["w0"]
        + w["w1"] * float(dt_s or 0.0)
        + w["w2"] * float(sample_size or 0.0)
        + w["w3"] * float(model_mb or 0.0)
        + w["w4"] * float(u_cpu_mean or 0.0)
        + w["w5"] * float(u_gpu_mean or 0.0)
        + w["w6"] * float(p_gpu_mean or 0.0)
        + w["w7"] * float(b_net_mb or 0.0)
    )
    return max(0.0, float(e))


def estimate_energy_physical(
    p_gpu_w: float = 0.0,
    u_cpu: float = 0.0,
    dt_s: float = 0.0,
    c_base: float = C_BASE_W,
) -> float:
    """Un intervalo ``(P_gpu + f(U_cpu) + C_base) * dt`` convertido a kWh."""
    total_w = float(p_gpu_w or 0.0) + cpu_power_from_util(u_cpu) + float(c_base)
    return max(0.0, total_w * float(dt_s or 0.0) / JOULES_PER_KWH)


def energy_mse(e_total: float, e_estimado: float) -> float:
    """L = (E_total - E_estimado)^2 para entrenar/cibrar los pesos."""
    return float((float(e_total) - float(e_estimado)) ** 2)
