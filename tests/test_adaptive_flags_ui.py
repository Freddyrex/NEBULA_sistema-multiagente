"""Fase 1 — interruptor visible del agente adaptativo (UI de creación).

Cubre: serialización/restauración de ``adaptive_agent`` (contrato con el JS
``AdaptiveAgentManager``), default off, ``pause_on_suggestion`` (default
contextual para compatibilidad), y que un escenario viejo sin la clave no se
auto-pause. El JSON que produce la UI es la fuente de verdad: estos tests
emulan ese payload leyéndolo con ``adaptive_flags``.
"""
import json
import os
import tempfile

from nebula.frontend.exec_interpreter import adaptive_flags, interpreter_enabled
from nebula.frontend.smac_api import SmacDeps, maybe_auto_pause


def _write_scenario(tmp, scenario, payload):
    folder = os.path.join(tmp, scenario)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "scenario.json"), "w") as f:
        json.dump(payload, f)


def _deps(tmp):
    return SmacDeps(config_dir=tmp, log_dir=tmp, databases_dir=None,
                    controller_host="h", controller_port=1, controller_name="c", api_key="")


# --- Serialización (contrato con adaptive-agent.js) -------------------------

def test_master_off_collapses_all_flags():
    """Con el interruptor maestro OFF, el JS manda todo false."""
    with tempfile.TemporaryDirectory() as tmp:
        _write_scenario(tmp, "demo", {"adaptive_agent": {
            "exec_interpreter": False, "aggregator_suggestions": False,
            "architecture_suggestions": False, "pause_on_suggestion": False,
            "aggregator_auto": False}})
        flags = adaptive_flags(tmp, "demo")
        assert flags == {"exec_interpreter": False, "aggregator_suggestions": False,
                         "architecture_suggestions": False, "pause_on_suggestion": False,
                         "aggregator_auto": False}


def test_master_on_round_trips_every_subflag():
    """El JS escribió explícitamente cada sub-bandera; se leen tal cual."""
    payload = {"exec_interpreter": True, "aggregator_suggestions": True,
               "architecture_suggestions": True, "pause_on_suggestion": True,
               "aggregator_auto": False}
    with tempfile.TemporaryDirectory() as tmp:
        _write_scenario(tmp, "demo", {"adaptive_agent": payload})
        assert adaptive_flags(tmp, "demo") == payload
        assert interpreter_enabled(tmp, "demo") is True


def test_backward_compat_missing_file_keeps_legacy_autopause():
    """Sin scenario.json (despliegue Sprint 7 que nunca tocó la UI): la
    auto-pausa legada sigue activa para no romper el comportamiento previo."""
    with tempfile.TemporaryDirectory() as tmp:
        assert adaptive_flags(tmp, "demo")["pause_on_suggestion"] is True


def test_hand_edited_block_without_pause_key_defaults_off():
    """Bloque adaptive_agent editado a mano sin la clave nueva → off (opt-in)."""
    with tempfile.TemporaryDirectory() as tmp:
        _write_scenario(tmp, "demo", {"adaptive_agent": {"exec_interpreter": True}})
        flags = adaptive_flags(tmp, "demo")
        assert flags["pause_on_suggestion"] is False
        assert flags["exec_interpreter"] is True


def test_old_scenario_without_key_never_autopauses():
    """Escenario viejo con adaptive_agent no vacío pero sin pause_on_suggestion:
    no debe pausarse a sí mismo (compatibilidad hacia atrás Fase 1)."""
    with tempfile.TemporaryDirectory() as tmp:
        _write_scenario(tmp, "demo", {"adaptive_agent": {"exec_interpreter": True}})
        deps = _deps(tmp)
        state = {"scenario": "demo", "status": "running",
                 "nodes": [{"idx": "0", "state": "running"}],
                 "suggestions": [{"id": "agg-Krum-x", "kind": "select_aggregator",
                                  "title": "Cambiar FedAvg → Krum"}],
                 "architecture": {"suggestions": []}}
        assert maybe_auto_pause(deps, "demo", state) is False
        assert not os.path.isdir(os.path.join(tmp, "demo", "operator_commands")) or \
            os.listdir(os.path.join(tmp, "demo", "operator_commands")) == []


def test_pause_on_suggestion_true_still_pauses():
    with tempfile.TemporaryDirectory() as tmp:
        _write_scenario(tmp, "demo", {"adaptive_agent": {
            "exec_interpreter": True, "aggregator_suggestions": True,
            "architecture_suggestions": True, "pause_on_suggestion": True}})
        deps = _deps(tmp)
        state = {"scenario": "demo", "status": "running",
                 "nodes": [{"idx": "0", "state": "running"}],
                 "suggestions": [{"id": "agg-Krum-x", "kind": "select_aggregator",
                                  "title": "Cambiar FedAvg → Krum"}],
                 "architecture": {"suggestions": []}}
        assert maybe_auto_pause(deps, "demo", state) is True


def test_pause_key_explicit_false_disables_legacy_autopause():
    """Un escenario nuevo con pause_on_suggestion=false no se pausa aunque
    haya sugerencias."""
    with tempfile.TemporaryDirectory() as tmp:
        _write_scenario(tmp, "demo", {"adaptive_agent": {
            "exec_interpreter": True, "pause_on_suggestion": False}})
        deps = _deps(tmp)
        state = {"scenario": "demo", "status": "running",
                 "nodes": [{"idx": "0", "state": "running"}],
                 "suggestions": [{"title": "Cambiar FedAvg → Krum"}],
                 "architecture": {"suggestions": []}}
        assert maybe_auto_pause(deps, "demo", state) is False
