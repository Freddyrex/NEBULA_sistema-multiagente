"""Defaults centralizados de NEBULA (single source of truth).

Regla: ningún literal de entorno/red/credencial/umbral vive en el código que
lo usa; todo se importa de aquí (con override por variable de entorno donde
tenga sentido). Los pesos físicos (térmicos/energía) siguen en
``nebula.core.utils.thermal_energy`` y los tags de métricas en
``nebula.frontend.metrics.providers`` (se referencian, no se duplican).
"""

from __future__ import annotations

import json
import os

try:  # python-dotenv es dependencia del proyecto; el CI mínimo puede omitirla.
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - entorno mínimo
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()  # carga .env sin sobrescribir variables ya exportadas


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


# --- Red / puertos -----------------------------------------------------------
CONTROLLER_PORT: int = _env_int("NEBULA_CONTROLLER_PORT", 5050)
FRONTEND_PORT: int = _env_int("NEBULA_FRONTEND_PORT", 8080)
STATISTICS_PORT: int = _env_int("NEBULA_STATISTICS_PORT", 8081)
PHYSICAL_NODE_PORT: int = _env_int("PHYSICAL_NODE_PORT", 8000)
WAF_PORT: int = _env_int("NEBULA_WAF_PORT", 6000)
GRAFANA_PORT: int = _env_int("NEBULA_GRAFANA_PORT", 6040)
GRAFANA_INTERNAL_PORT: int = _env_int("GRAFANA_INTERNAL_PORT", 3000)
LOKI_PORT: int = _env_int("LOKI_PORT", 3100)
BIND_HOST: str = _env_str("NEBULA_BIND_HOST", "0.0.0.0")
TENSORBOARD_HOST: str = _env_str("NEBULA_TENSORBOARD_HOST", "127.0.0.1")
TENSORBOARD_PORT: int = _env_int("NEBULA_TENSORBOARD_PORT", 8080)
CONTROLLER_SCHEME: str = _env_str("NEBULA_CONTROLLER_SCHEME", "http")
# Endpoint público del controller para nodos físicos (sin default: en
# docker/physical se resuelve por env o fallback local; nunca IP personal).
CONTROLLER_PUBLIC_ENDPOINT: str = _env_str("NEBULA_CONTROLLER_PUBLIC_ENDPOINT", "")
CONTROLLER_HOST_FALLBACK: str = _env_str("NEBULA_CONTROLLER_HOST_FALLBACK", "127.0.0.1")
# Escenarios docker/physical (§2.3)
SCENARIO_IP_START: int = _env_int("NEBULA_SCENARIO_IP_START", 2)
SCENARIO_START_DELAY_S: int = _env_int("NEBULA_START_DELAY_S", 10)
LEGACY_SUBNET_PREFIX: str = _env_str("NEBULA_LEGACY_SUBNET", "192.168.50.")
# Imágenes Docker con registry/tag (§3.3). Pinnear Grafana, nunca `latest`.
IMAGE_FRONTEND: str = _env_str("NEBULA_IMAGE_FRONTEND", "nebula-frontend:latest")
IMAGE_CONTROLLER: str = _env_str("NEBULA_IMAGE_CONTROLLER", "nebula-controller:latest")
IMAGE_CORE: str = _env_str("NEBULA_IMAGE_CORE", "nebula-core:latest")
IMAGE_WAF: str = _env_str("NEBULA_IMAGE_WAF", "nebula-waf:latest")
IMAGE_GRAFANA: str = _env_str("NEBULA_IMAGE_GRAFANA", "grafana/grafana:11.3.2")
IMAGE_LOKI: str = _env_str("NEBULA_IMAGE_LOKI", "grafana/loki:3.2.0")
IMAGE_PROMTAIL: str = _env_str("NEBULA_IMAGE_PROMTAIL", "grafana/promtail:3.2.0")
# IPs intra-red (§3.4). Vacío = dejar IPAM/DNS de Docker (preferible).
IP_FRONTEND_SUFFIX: str = _env_str("NEBULA_IP_FRONTEND", ".100")
IP_CONTROLLER_SUFFIX: str = _env_str("NEBULA_IP_CONTROLLER", ".150")
IP_WAF_SUFFIX: str = _env_str("NEBULA_IP_WAF", ".200")
IP_GRAFANA_SUFFIX: str = _env_str("NEBULA_IP_GRAFANA", ".201")
IP_LOKI_SUFFIX: str = _env_str("NEBULA_IP_LOKI", ".202")
IP_PROMTAIL_SUFFIX: str = _env_str("NEBULA_IP_PROMTAIL", ".203")
# JSON opcional {"host.docker.internal": "host-gateway", ...} (podman no
# soporta `host-gateway`; vacío = sin extra_hosts).
EXTRA_HOSTS_JSON: str = _env_str("NEBULA_EXTRA_HOSTS", "")
# Timezone (§3.5)
TZ: str = _env_str("NEBULA_TZ", "Etc/UTC")
# Nodos / entrypoint físico (§2.2)
NODE_ENTRYPOINT: str = _env_str("NEBULA_NODE_ENTRYPOINT", "/nebula/nebula/core/node.py")

# --- Reintentos / timeouts ----------------------------------------------------
RETRY_MAX: int = _env_int("NEBULA_RETRY_MAX", 5)
RETRY_BASE_S: float = _env_float("NEBULA_RETRY_BASE_S", 1.0)
RETRY_FACTOR: float = _env_float("NEBULA_RETRY_FACTOR", 2.0)
RETRY_MAX_DELAY_S: float = _env_float("NEBULA_RETRY_MAX_DELAY_S", 30.0)
RETRY_JITTER: float = _env_float("NEBULA_RETRY_JITTER", 0.1)
HTTP_TIMEOUT_S: float = _env_float("NEBULA_HTTP_TIMEOUT_S", 15.0)
DB_TIMEOUT_S: float = _env_float("NEBULA_DB_TIMEOUT_S", 5.0)
S_TO_MS: int = 1000

# --- AFAS (dynamic aggregator failover) --------------------------------------
AFAS_ENABLED: bool = _env_str("NEBULA_AFAS_ENABLED", "true").lower() in ("1", "true", "yes", "on")
AFAS_PROBE_INTERVAL_S: float = _env_float("NEBULA_AFAS_PROBE_INTERVAL_S", 2.0)
AFAS_PROBE_TIMEOUT_S: float = _env_float("NEBULA_AFAS_PROBE_TIMEOUT_S", 1.0)
AFAS_RTT_THRESHOLD_MS: float = _env_float("NEBULA_AFAS_RTT_THRESHOLD_MS", 200.0)
AFAS_JITTER_THRESHOLD_MS: float = _env_float("NEBULA_AFAS_JITTER_THRESHOLD_MS", 50.0)
AFAS_LOSS_THRESHOLD_PCT: float = _env_float("NEBULA_AFAS_LOSS_THRESHOLD_PCT", 10.0)
AFAS_FAILED_PROBES_THRESHOLD: int = _env_int("NEBULA_AFAS_FAILED_PROBES_THRESHOLD", 3)
AFAS_HISTORY_SIZE: int = _env_int("NEBULA_AFAS_HISTORY_SIZE", 20)
AFAS_METRICS_TTL_S: float = _env_float("NEBULA_AFAS_METRICS_TTL_S", 30.0)
AFAS_ELECTION_COOLDOWN_S: float = _env_float("NEBULA_AFAS_ELECTION_COOLDOWN_S", 30.0)
AFAS_SWITCH_HOLD_ROUNDS: int = _env_int("NEBULA_AFAS_SWITCH_HOLD_ROUNDS", 3)
AFAS_MAX_BUFFER_TRANSFER_MB: int = _env_int("NEBULA_AFAS_MAX_BUFFER_TRANSFER_MB", 32)
AFAS_STARTUP_GRACE_S: float = _env_float("NEBULA_AFAS_STARTUP_GRACE_S", 30.0)
AFAS_DEGRADE_CONFIRM_S: float = _env_float("NEBULA_AFAS_DEGRADE_CONFIRM_S", 3.0)
AFAS_DIGEST_INTERVAL_S: float = _env_float("NEBULA_AFAS_DIGEST_INTERVAL_S", 5.0)

# --- SMAC / Gemini -------------------------------------------------------------
# gemini-3.5-flash: balance razonamiento/explicación con consumo moderado
# (nada de pro/4-argon ni modelos de imagen). El agente del nodo usa
# gemini-3.5-flash-lite por latencia.
SMAC_CHAT_MODEL: str = _env_str("SMAC_CHAT_MODEL", "gemini-3.5-flash")
SMAC_REASONING_EFFORT: str = _env_str("SMAC_CHAT_REASONING_EFFORT", "medium")
SMAC_TIMEOUT_S: float = _env_float("SMAC_TIMEOUT_S", 30.0)
SMAC_MAX_TOKENS: int = _env_int("SMAC_CHAT_MAX_TOKENS", 4096)
SMAC_TEMPERATURE: float = _env_float("SMAC_TEMPERATURE", 0.2)
SMAC_TOP_P: float = _env_float("SMAC_TOP_P", 1.0)
SMAC_SNAPSHOT_TTL_S: float = _env_float("SMAC_SNAPSHOT_TTL_S", 45.0)
# --- SMAC crew (optional multi-agent layer; off by default) -------------------
SMAC_MULTI_AGENT: bool = os.environ.get("SMAC_MULTI_AGENT", "0").lower() in ("1", "true", "yes", "on")
SMAC_CREW_STRATEGY: str = _env_str("SMAC_CREW_STRATEGY", "auto")
SMAC_CREW_NODE_ENABLED: bool = os.environ.get("SMAC_CREW_NODE_ENABLED", "0").lower() in ("1", "true", "yes", "on")
SMAC_CREW_USE_CREWAI: bool = os.environ.get("SMAC_CREW_USE_CREWAI", "0").lower() in ("1", "true", "yes", "on")
SMAC_CREW_DEBATE_ENABLED: bool = os.environ.get("SMAC_CREW_DEBATE_ENABLED", "0").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
SMAC_CREW_SPECIALIST_MODEL: str = _env_str("SMAC_CREW_SPECIALIST_MODEL", "gemini-3.5-flash-lite")
SMAC_CREW_COORDINATOR_MODEL: str = _env_str("SMAC_CREW_COORDINATOR_MODEL", "gemini-3.5-flash")
SMAC_CREW_AGENT_TIMEOUT_S: float = _env_float("SMAC_CREW_AGENT_TIMEOUT_S", 6.0)
SMAC_CREW_TOTAL_TIMEOUT_S: float = _env_float("SMAC_CREW_TOTAL_TIMEOUT_S", 8.0)
SMAC_CREW_CACHE_TTL_S: float = _env_float("SMAC_CREW_CACHE_TTL_S", 45.0)
SMAC_CREW_MAX_AGENTS: int = _env_int("SMAC_CREW_MAX_AGENTS", 4)
SMAC_CREW_DEBATE_MAX_TURNS: int = _env_int("SMAC_CREW_DEBATE_MAX_TURNS", 2)
SMAC_CREW_MAX_RPM: int = _env_int("SMAC_CREW_MAX_RPM", 30)
SMAC_CREW_MIN_CLIENTS: int = _env_int("SMAC_CREW_MIN_CLIENTS", 2)
SMAC_CREW_MAX_EXCLUSION_RATIO: float = _env_float("SMAC_CREW_MAX_EXCLUSION_RATIO", 0.4)
PENDING_ACTION_TTL_S: float = _env_float("SMAC_PENDING_TTL_S", 600.0)
OPERATOR_RESULT_WAIT_S: float = _env_float("SMAC_OPERATOR_WAIT_S", 12.0)
OPERATOR_POLL_S: float = _env_float("SMAC_OPERATOR_POLL_S", 1.0)
RATE_LIMIT_PER_MIN: int = _env_int("SMAC_RATE_PER_MIN", 20)
RATE_WINDOW_S: int = _env_int("SMAC_RATE_WINDOW_S", 60)
AUDIT_TRUNC_CHARS: int = _env_int("SMAC_AUDIT_TRUNC_CHARS", 500)
SSE_MAX_WAIT_S: float = _env_float("SMAC_SSE_MAX_WAIT_S", 90.0)
SSE_POLL_S: float = _env_float("SMAC_SSE_POLL_S", 4.0)
# Fase 2 (Copilot): watcher en servidor que pausa sin tener el panel abierto.
ADAPTIVE_WATCH_POLL_S: float = _env_float("SMAC_ADAPTIVE_WATCH_POLL_S", 10.0)
# Fase 4 (Copilot): TTL de los borradores de nueva simulación.
DRAFT_TTL_S: float = _env_float("SMAC_DRAFT_TTL_S", 3600.0)
DRAFT_ID_HEX: int = _env_int("SMAC_DRAFT_ID_HEX", 12)
ACTION_ID_HEX: int = _env_int("SMAC_ACTION_ID_HEX", 12)
LLM_RANKING_HEAD: int = _env_int("SMAC_LLM_RANKING_HEAD", 10)
LLM_OUTLIER_HEAD: int = _env_int("SMAC_LLM_OUTLIER_HEAD", 10)
DECISION_TAIL_LIMIT: int = _env_int("SMAC_DECISION_TAIL_LIMIT", 15)
ANALYZE_MAX_CHARS: int = _env_int("SMAC_ANALYZE_MAX_CHARS", 1200)
SCENARIO_DONE_STATES: frozenset[str] = frozenset({"completed", "finished"})

# --- Métricas / series ---------------------------------------------------------
SERIES_DEFAULT_POINTS: int = _env_int("NEBULA_SERIES_DEFAULT_POINTS", 500)
SERIES_HARD_CAP: int = _env_int("NEBULA_SERIES_HARD_CAP", 2000)
DIGEST_ROUND_ND: int = _env_int("NEBULA_DIGEST_ROUND_ND", 4)
CONVERGENCE_EPS: float = _env_float("NEBULA_CONVERGENCE_EPS", 0.005)
CONVERGENCE_K: int = _env_int("NEBULA_CONVERGENCE_K", 3)
OUTLIER_GAP: float = _env_float("NEBULA_OUTLIER_GAP", 0.05)
STRAGGLER_RATIO: float = _env_float("NEBULA_STRAGGLER_RATIO", 1.5)
NO_OUTLIER_METRICS: frozenset[str] = frozenset({"round_time_s"})
NODE_ONLINE_THRESHOLD_S: int = _env_int("NEBULA_NODE_ONLINE_S", 25)

# --- Reporter / telemetría -----------------------------------------------------
REPORTER_CONFIG_RELOAD_CYCLES: int = _env_int("NEBULA_REPORTER_RELOAD_CYCLES", 50)
REPORTER_RETRY_DELAY_S: float = _env_float("NEBULA_REPORTER_RETRY_DELAY_S", 5.0)
REPORTER_CPU_SAMPLE_INTERVAL_S: float = _env_float("NEBULA_REPORTER_CPU_INTERVAL_S", 1.0)
BYTES_PER_MB: int = 1024**2
NET_MB_PRECISION: int = 3
MW_TO_W: float = 1000.0
CPU_SENSOR_NAMES: tuple[str, ...] = ("coretemp", "k10temp", "cpu_thermal")
# Modelo térmico/energético por dispositivo (§6.2). Defaults conservadores
# x86; en ARM/RPi calibrar con `device_args` o env (95W sobreestima 6-19×).
CPU_TDP_W: float = _env_float("NEBULA_CPU_TDP_W", 95.0)
BASE_POWER_W: float = _env_float("NEBULA_BASE_POWER_W", 15.0)
TEMP_SEED_C: float = _env_float("NEBULA_TEMP_SEED_C", 45.0)
TEMP_MIN_C: float = _env_float("NEBULA_TEMP_MIN_C", 20.0)
TEMP_MAX_C: float = _env_float("NEBULA_TEMP_MAX_C", 105.0)
# Sleeps de control del frontend con nombre (§7, app.py).
RAM_POLL_S: float = _env_float("NEBULA_RAM_POLL_S", 1.0)
MONITOR_ERROR_S: float = _env_float("NEBULA_MONITOR_ERROR_S", 5.0)
MONITOR_INTERVAL_S: float = _env_float("NEBULA_MONITOR_INTERVAL_S", 20.0)
PHYSICAL_BUSY_S: float = _env_float("NEBULA_PHYSICAL_BUSY_S", 10.0)
FINISH_POLL_S: float = _env_float("NEBULA_FINISH_POLL_S", 1.0)
RESOURCE_POLL_S: float = _env_float("NEBULA_RESOURCE_POLL_S", 1.0)
CONTROLLER_RETRY_S: float = _env_float("NEBULA_CONTROLLER_RETRY_S", 5.0)
PHYSICAL_FINISH_S: float = _env_float("NEBULA_PHYSICAL_FINISH_S", 3.0)

# --- Training ------------------------------------------------------------------
TRAIN_LOG_EVERY_N_STEPS: int = _env_int("NEBULA_TRAIN_LOG_EVERY_N_STEPS", 100)
DEFAULT_EPOCHS: int = _env_int("NEBULA_DEFAULT_EPOCHS", 1)
MODEL_SUMMARY_DEPTH: int = _env_int("NEBULA_MODEL_SUMMARY_DEPTH", 1)
GPU_ASSIGNMENT_STRATEGY: str = _env_str("NEBULA_GPU_ASSIGNMENT_STRATEGY", "round_robin")
# Privacidad diferencial (§7): sin defaults silenciosos en dp.py; exigir
# config explícita. Solo rangos de validación aquí.
DP_NOISE_MULTIPLIER: float = _env_float("NEBULA_DP_NOISE_MULTIPLIER", 1.0)
DP_MAX_GRAD_NORM: float = _env_float("NEBULA_DP_MAX_GRAD_NORM", 1.0)
DP_DELTA: float = _env_float("NEBULA_DP_DELTA", 1e-5)
DP_ACCOUNTANT: str = _env_str("NEBULA_DP_ACCOUNTANT", "prv")
# Métricas versionadas (§6.8). Sprint 1 (ROADMAP_AGENTE_ADAPTATIVO): 1.1 añade
# f1_macro/recall_macro/precision_macro + cpu_percent/ram_percent/cpu_temp.
METRICS_SCHEMA_VERSION: str = _env_str("NEBULA_METRICS_SCHEMA_VERSION", "1.1")
# Outliers por métrica (§6.7): override global + JSON opcional por métrica.
OUTLIER_GAP_BY_METRIC_JSON: str = _env_str("NEBULA_OUTLIER_GAP_BY_METRIC", "")
# Directorios físicos absolutos (§3.5)
PHYSICAL_CERTS_DIR: str = _env_str("NEBULA_PHYSICAL_CERTS_DIR", "/nebula/app/certs")
PHYSICAL_CONFIG_DIR: str = _env_str("NEBULA_PHYSICAL_CONFIG_DIR", "/nebula/app/config")
PHYSICAL_LOGS_DIR: str = _env_str("NEBULA_PHYSICAL_LOGS_DIR", "/nebula/app/logs")
PHYSICAL_METRICS_DIR: str = _env_str("NEBULA_PHYSICAL_METRICS_DIR", "/nebula/app/logs/metrics")

# --- Movilidad -------------------------------------------------------------------
MOBILITY_MAX_DIRECT_M: float = _env_float("NEBULA_MOBILITY_MAX_DIRECT_M", 150.0)
MOBILITY_MAX_STEP_M: float = _env_float("NEBULA_MOBILITY_MAX_STEP_M", 50.0)
MOBILITY_APPROX_MARGIN: float = _env_float("NEBULA_MOBILITY_APPROX_MARGIN", 1.2)
METERS_PER_DEGREE_LAT: float = 111000.0
MOBILITY_JITTER: float = _env_float("NEBULA_MOBILITY_JITTER", 0.2)

# --- Chat: historial por escenario -----------------------------------------------
HIST_MAX_MSG: int = _env_int("NEBULA_HIST_MAX_MSG", 60)
HIST_MAX_SC: int = _env_int("NEBULA_HIST_MAX_SC", 15)
HIST_MAX_LEN: int = _env_int("NEBULA_HIST_MAX_LEN", 2000)

# --- Frontend UI -----------------------------------------------------------------
FRONTEND_POLL_MS: int = _env_int("NEBULA_FRONTEND_POLL_MS", 5000)
COPY_RESET_MS: int = _env_int("NEBULA_COPY_RESET_MS", 1200)
STATIC_VERSION: str = _env_str("NEBULA_STATIC_VERSION", "20261021")
# Breakpoint dock/drawer compartido JS+CSS (§5.3). CSS usa
# `@media (max-width: BREAKPOINT_DOCK-1px)`; JS usa `matchMedia`.
BREAKPOINT_DOCK: int = _env_int("NEBULA_BREAKPOINT_DOCK", 1280)
CHAT_CHIPS_MAX: int = _env_int("NEBULA_CHAT_CHIPS_MAX", 3)
CHAT_READY_RESET_MS: int = _env_int("NEBULA_CHAT_READY_RESET_MS", 5000)
MONITOR_POLL_MS: int = _env_int("NEBULA_MONITOR_POLL_MS", 5000)
MONITOR_QUEUE_MS: int = _env_int("NEBULA_MONITOR_QUEUE_MS", 100)
MONITOR_STALE_BASE_MS: int = _env_int("NEBULA_MONITOR_STALE_BASE_MS", 20000)
MONITOR_STALE_PER_NODE_MS: int = _env_int("NEBULA_MONITOR_STALE_PER_NODE_MS", 500)
TOPO_HEIGHT_PX: int = _env_int("NEBULA_TOPO_HEIGHT_PX", 480)
# Firebase / analytics (layout.html §5: sin apiKey literal en el template).
FIREBASE_API_KEY: str = _env_str("NEBULA_FIREBASE_API_KEY", "")
FIREBASE_AUTH_DOMAIN: str = _env_str("NEBULA_FIREBASE_AUTH_DOMAIN", "")
FIREBASE_PROJECT_ID: str = _env_str("NEBULA_FIREBASE_PROJECT_ID", "")
GTAG_ID: str = _env_str("NEBULA_GTAG_ID", "")
# Google OAuth (sin host/puerto literal en .env).
GOOGLE_REDIRECT_URI: str = _env_str(
    "GOOGLE_REDIRECT_URI", "http://127.0.0.1:8000/auth/google/callback"
)

# --- Controller ------------------------------------------------------------------
GPU_BUSY_THRESHOLD: int = _env_int("NEBULA_GPU_BUSY_THRESHOLD", 50)
GPU_FREE_THRESHOLD: int = _env_int("NEBULA_GPU_FREE_THRESHOLD", 5)
PHYSICAL_TIMEOUT_S: int = _env_int("NEBULA_PHYSICAL_TIMEOUT_S", 3)
# --- Registro estático de nodos (reemplaza Tailscale) ------------------------
# NEBULA_NODES: JSON {"pi1": "192.168.1.11:8000", ...} o "pi1=ip:port,pi2=..."
# NEBULA_NODE_REGISTRY_FILE: ruta a un JSON con el mismo formato.
NODES_JSON: str = _env_str("NEBULA_NODES", "")
NODE_REGISTRY_FILE: str = _env_str("NEBULA_NODE_REGISTRY_FILE", "")
NODE_NAME: str = _env_str("NEBULA_NODE_NAME", "")
NODE_IP: str = _env_str("NEBULA_NODE_IP", "")
NODE_TCP_CHECK: bool = _env_str("NEBULA_NODE_TCP_CHECK", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
NODE_REACHABILITY_TIMEOUT_S: float = _env_float(
    "NEBULA_NODE_REACHABILITY_TIMEOUT_S", 2.0
)

# --- Simulación de red -----------------------------------------------------------
# Tabla distancia(m) -> {bandwidth, delay}. Override por escenario con
# network_args["distance_thresholds"] = [{"up_to_m": 100, "bandwidth": "5Gbps",
# "delay": "5ms"}, ...] (último tramo = partición).
NETWORK_DISTANCE_THRESHOLDS: tuple[dict, ...] = (
    {"up_to_m": 100, "bandwidth": "5Gbps", "delay": "5ms"},
    {"up_to_m": 200, "bandwidth": "2Gbps", "delay": "50ms"},
    {"up_to_m": 300, "bandwidth": "100Mbps", "delay": "200ms"},
    {"up_to_m": float("inf"), "bandwidth": "10Mbps", "delay": "100000ms"},
)
PARTITION_DELAY_MS: int = _env_int("NEBULA_PARTITION_DELAY_MS", 100000)
MULTICAST_IP: str = _env_str("NEBULA_MULTICAST_IP", "239.255.255.250")
NET_COND_PRECISION: int = 2
TC_DEFAULTS: dict[str, str] = {
    "interface": "eth0",
    "bandwidth": "5Gbps",
    "delay": "0ms",
    "delay_distro": "10ms",
    "delay_distribution": "normal",
    "loss": "0%",
    "duplicate": "0%",
    "corrupt": "0%",
    "reordering": "0%",
}


# --- Helpers single-source (§3.2) ------------------------------------------------
def controller_name() -> str:
    """Nombre base del controller sin KeyError (§3.2).

    Prioridad: NEBULA_CONTROLLER_NAME > USER > getpass.getuser() > "nebula".
    """

    return (
        os.getenv("NEBULA_CONTROLLER_NAME")
        or os.getenv("USER")
        or _safe_getuser()
        or "nebula"
    )


def _safe_getuser() -> str:
    import getpass

    try:
        return getpass.getuser()
    except Exception:
        return ""


def extra_hosts() -> dict[str, str]:
    """NEBULA_EXTRA_HOSTS como dict (JSON opcional, §3.4)."""
    import json

    raw = os.getenv("NEBULA_EXTRA_HOSTS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return dict(data) if isinstance(data, dict) else {}
    except Exception:
        return {}


def outlier_gap_for(metric: str) -> float:
    """Gap de outlier por métrica (§6.7): JSON > global OUTLIER_GAP."""
    import json

    raw = os.getenv("NEBULA_OUTLIER_GAP_BY_METRIC", "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and metric in data:
                return float(data[metric])
        except Exception:
            pass
    return float(OUTLIER_GAP)


def _normalise_nodes(data: object) -> dict[str, str]:
    """Normalise a registry payload to {name: host[:port]}."""
    if isinstance(data, dict):
        return {str(k): str(v).strip() for k, v in data.items() if str(v).strip()}
    if isinstance(data, list):
        result: dict[str, str] = {}
        for idx, item in enumerate(data):
            if isinstance(item, dict):
                name = item.get("name") or item.get("id") or str(idx)
                host = item.get("host") or item.get("ip") or item.get("address")
                if host:
                    result[str(name)] = str(host).strip()
            elif isinstance(item, str) and item.strip():
                result[str(idx)] = item.strip()
        return result
    return {}


def _parse_nodes(raw: str) -> dict[str, str]:
    """Parse NEBULA_NODES: JSON object/list or name=host[:port],... pairs."""
    raw = raw.strip()
    if not raw:
        return {}
    try:
        return _normalise_nodes(json.loads(raw))
    except json.JSONDecodeError:
        data: dict[str, str] = {}
        for item in raw.split(","):
            if "=" in item:
                name, host = item.split("=", 1)
                name, host = name.strip(), host.strip()
                if name and host:
                    data[name] = host
        return data


def load_node_registry() -> dict[str, str]:
    """Return {name: host[:port]} from NEBULA_NODES or NEBULA_NODE_REGISTRY_FILE.

    The environment is read on every call so a .env loaded after import is
    still honoured. Returns {} when nothing is configured or the file is
    unreadable.
    """
    raw = os.environ.get("NEBULA_NODES", "").strip()
    if raw:
        return _parse_nodes(raw)
    path = os.environ.get("NEBULA_NODE_REGISTRY_FILE", "").strip()
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return _normalise_nodes(json.load(fh))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def host_part(endpoint: str) -> str:
    """Strip scheme and :port from a registry endpoint (IPv4)."""
    value = endpoint.strip()
    if "://" in value:
        value = value.split("://", 1)[1]
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value


def node_ips() -> list[str]:
    """Return unique host parts from the registry, preserving order."""
    seen: set[str] = set()
    ips: list[str] = []
    for endpoint in load_node_registry().values():
        host = host_part(endpoint)
        if host and host not in seen:
            seen.add(host)
            ips.append(host)
    return ips
