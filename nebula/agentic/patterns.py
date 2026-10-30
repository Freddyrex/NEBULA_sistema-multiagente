"""Registry of architectural patterns and the real NEBULA levers behind them.

Only patterns that exist in this repository are ``available``. A pattern marked
``available=False`` can never be proposed by an agent (the guardrail rejects it);
a pattern marked ``hot=False`` becomes a linked new-simulation draft instead of a
hot change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PatternSpec:
    id: str
    label: str
    lever: str
    action_kind: str
    available: bool
    hot: bool
    allowed_values: tuple[str, ...] = ()
    description: str = ""
    min_clients: int = 2


PATTERNS: dict[str, PatternSpec] = {
    "aggregation_algorithm": PatternSpec(
        id="aggregation_algorithm",
        label="Algoritmo de agregación",
        lever="select_aggregator",
        action_kind="select_aggregator",
        available=True,
        hot=True,
        allowed_values=("fedavg", "krum", "median", "trimmedmean"),
        description="Selecciona el algoritmo de agregación del siguiente round.",
    ),
    "client_selection": PatternSpec(
        id="client_selection",
        label="Selección de clientes",
        lever="pause_node",
        action_kind="pause_node",
        available=True,
        hot=True,
        description="Excluye temporalmente nodos low-spec o stragglers.",
    ),
    "training_policy": PatternSpec(
        id="training_policy",
        label="Política de entrenamiento",
        lever="adjust_rounds/adjust_epochs",
        action_kind="propose_architecture_change",
        available=True,
        hot=True,
        description="Ajusta rondas totales o epochs locales.",
    ),
    "model": PatternSpec(
        id="model",
        label="Modelo global",
        lever="swap_model",
        action_kind="propose_architecture_change",
        available=True,
        hot=True,
        description="Cambia el modelo si es compatible.",
    ),
    "topology": PatternSpec(
        id="topology",
        label="Topología",
        lever="propose_disconnect",
        action_kind="propose_new_simulation",
        available=True,
        hot=False,
        description="Cambio estructural de topología.",
    ),
    "batch_size": PatternSpec(
        id="batch_size",
        label="Batch size",
        lever="nueva simulación",
        action_kind="propose_new_simulation",
        available=True,
        hot=False,
        description="Requiere rebuild del dataloader.",
    ),
    "message_compression": PatternSpec(
        id="message_compression",
        label="Compresión de mensajes",
        lever="compression_policy",
        action_kind="propose_architecture_change",
        available=False,
        hot=True,
        description="Futuro: exponer on/off de compresión por escenario.",
    ),
    "heterogeneous_data_handler": PatternSpec(
        id="heterogeneous_data_handler",
        label="Manejador de datos heterogéneos",
        lever="data_augmentation",
        action_kind="propose_architecture_change",
        available=False,
        hot=True,
        description="Futuro: GAN/JSD; no implementado en NEBULA.",
    ),
}


def get_pattern(pattern_id: str) -> PatternSpec | None:
    return PATTERNS.get(str(pattern_id or ""))


def available_patterns() -> dict[str, PatternSpec]:
    return {k: v for k, v in PATTERNS.items() if v.available}


def to_action_kind(pattern_id: str) -> str | None:
    spec = get_pattern(pattern_id)
    return spec.action_kind if spec else None


def is_allowed(pattern_id: str, params: dict | None = None) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for a pattern id and its params."""
    spec = get_pattern(pattern_id)
    if spec is None:
        return False, f"patrón desconocido: {pattern_id}"
    if not spec.available:
        return False, f"patrón no disponible en NEBULA: {pattern_id}"
    values = spec.allowed_values
    if values:
        raw = (params or {}).get("algorithm") or (params or {}).get("value")
        if raw is not None and str(raw).strip().lower() not in values:
            return False, f"valor no permitido para {pattern_id}: {raw}"
    return True, "ok"


def canonical_algorithm(value: str) -> str:
    """Normalize an aggregator name to the casing used by NEBULA."""
    mapping = {
        "fedavg": "FedAvg",
        "krum": "Krum",
        "median": "Median",
        "trimmedmean": "TrimmedMean",
    }
    return mapping.get(str(value or "").strip().lower(), str(value or ""))


def canonical_pattern_for_change(change: dict) -> str | None:
    """Map an exec-interpreter change payload to a registry pattern id."""
    kind = str((change or {}).get("kind") or "").strip().lower()
    return {
        "rounds": "training_policy",
        "epochs": "training_policy",
        "aggregator": "aggregation_algorithm",
        "model": "model",
        "topology": "topology",
        "batch": "batch_size",
        "batch_size": "batch_size",
        "federation": "topology",
    }.get(kind)


__all__ = [
    "PATTERNS",
    "PatternSpec",
    "available_patterns",
    "canonical_algorithm",
    "canonical_pattern_for_change",
    "get_pattern",
    "is_allowed",
    "to_action_kind",
]
