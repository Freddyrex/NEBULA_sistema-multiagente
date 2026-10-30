"""Seguridad del servicio controller: token de servicio, roles y rutas seguras.

Criterios:
- Con ``NEBULA_CONTROLLER_TOKEN`` configurado, toda ruta del controller exige
  el token (401 si falta o no coincide).
- El token se acepta como ``Authorization: Bearer <token>`` y como
  ``X-NEBULA-Token``.
- Un rol fuera del conjunto permitido se rechaza con 400.
- Una ``federation`` con metacaracteres de shell o con traversal se rechaza y
  no escribe nada fuera del directorio de configuracion.
- Un escenario valido sigue usando la ``federation`` saneada en el nombre del
  directorio y crea ficheros con permisos restrictivos.
"""
import importlib
import os
import stat

import pytest
from fastapi.testclient import TestClient

SERVICE_KEY = "test-controller-key"


def _scenario_payload(federation: str = "DFL") -> dict:
    """Construye un payload de escenario minimo pero valido.

    Args:
        federation (str): Valor de ``federation`` que se quiere probar.

    Returns:
        dict: Escenario con los campos que exige ``Scenario``.
    """
    nodes = {
        "0": {
            "id": "0",
            "ip": "10.0.0.1",
            "port": 8000,
            "role": "aggregator",
            "malicious": False,
            "proxy": False,
            "start": True,
            "neighbors": "",
            "links": [],
        }
    }
    return {
        "scenario_title": "Security test",
        "scenario_description": "Escenario de prueba de seguridad",
        "deployment": "docker",
        "federation": federation,
        "topology": "FullMesh",
        "nodes": nodes,
        "nodes_graph": nodes,
        "n_nodes": 1,
        "matrix": None,
        "dataset": "MNIST",
        "iid": False,
        "partition_selection": "dirichlet",
        "partition_parameter": 0.5,
        "model": "MLP",
        "agg_algorithm": "FedAvg",
        "rounds": 1,
        "logginglevel": False,
        "report_status_data_queue": True,
        "accelerator": "cpu",
        "gpu_id": None,
        "network_subnet": "10.0.0.0/24",
        "network_gateway": "10.0.0.1",
        "epochs": 1,
        "attack_params": {"attacks": "No Attack"},
        "reputation": {"enabled": False},
        "random_geo": False,
        "latitude": 0.0,
        "longitude": 0.0,
        "mobility": False,
        "network_simulation": False,
        "mobility_type": "topology",
        "radius_federation": 1000,
        "scheme_mobility": "random",
        "round_frequency": 1,
        "mobile_participants_percent": 0,
        "additional_participants": None,
        "schema_additional_participants": None,
        "with_trustworthiness": False,
        "robustness_pillar": {},
        "resilience_to_attacks": {},
        "algorithm_robustness": {},
        "client_reliability": {},
        "privacy_pillar": {},
        "technique": {},
        "uncertainty": {},
        "indistinguishability": {},
        "fairness_pillar": {},
        "selection_fairness": {},
        "performance_fairness": {},
        "class_distribution": {},
        "outcome_fairness": {},
        "explainability_pillar": {},
        "interpretability": {},
        "post_hoc_methods": {},
        "accountability_pillar": {},
        "factsheet_completeness": {},
        "monitoring": {},
        "architectural_soundness_pillar": {},
        "client_management": {},
        "optimization": {},
        "federation_management": {},
        "sustainability_pillar": {},
        "energy_source": {},
        "hardware_efficiency": {},
        "federation_complexity": {},
        "random_topology_probability": 0.0,
        "with_sa": False,
        "strict_topology": False,
        "sad_candidate_selector": "random",
        "sad_model_handler": "default",
        "sar_arbitration_policy": "static",
        "sar_neighbor_policy": "static",
        "sar_training": False,
        "sar_training_policy": "default",
    }


@pytest.fixture
def secured_app(monkeypatch):
    """Recarga el controller con un token de servicio configurado.

    Yields:
        FastAPI: Aplicacion del controller con el token activo.
    """
    monkeypatch.setenv("NEBULA_CONTROLLER_TOKEN", SERVICE_KEY)
    monkeypatch.setenv("NEBULA_PRODUCTION", "False")
    import nebula.controller.controller as controller

    importlib.reload(controller)
    yield controller.app


@pytest.fixture
def scenario_dirs(monkeypatch, tmp_path):
    """Apunta los directorios del controller a un arbol temporal.

    Args:
        monkeypatch (pytest.MonkeyPatch): Fixture de monkeypatch.
        tmp_path (pathlib.Path): Directorio temporal del test.

    Returns:
        pathlib.Path: Raiz temporal con config, logs, certs y root.
    """
    for name in ("config", "logs", "certs", "root"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("NEBULA_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("NEBULA_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("NEBULA_CERTS_DIR", str(tmp_path / "certs"))
    monkeypatch.setenv("NEBULA_ROOT_HOST", str(tmp_path / "root"))
    monkeypatch.setenv("NEBULA_HOST_PLATFORM", "linux")
    monkeypatch.setenv("NEBULA_CONTROLLER_NAME", "test-controller")
    monkeypatch.setenv("NEBULA_CONTROLLER_PORT", "5050")
    return tmp_path


def test_should_return_401_when_the_request_has_no_token_and_a_token_is_configured(secured_app):
    """Sin token, el controller rechaza la peticion."""
    client = TestClient(secured_app)
    response = client.get("/status")
    assert response.status_code == 401


def test_should_not_return_401_when_the_bearer_token_matches_the_configured_token(secured_app):
    """El token valido en ``Authorization: Bearer`` deja pasar la peticion."""
    client = TestClient(secured_app)
    response = client.get("/status", headers={"Authorization": f"Bearer {SERVICE_KEY}"})
    assert response.status_code != 401
    assert response.status_code == 200


def test_should_not_return_401_when_the_service_token_header_matches_the_configured_token(secured_app):
    """El token valido en ``X-NEBULA-Token`` deja pasar la peticion."""
    client = TestClient(secured_app)
    response = client.get("/status", headers={"X-NEBULA-Token": SERVICE_KEY})
    assert response.status_code != 401
    assert response.status_code == 200


def test_should_return_401_when_the_provided_token_does_not_match(secured_app):
    """Un token incorrecto se rechaza con 401."""
    client = TestClient(secured_app)
    response = client.get("/status", headers={"Authorization": "Bearer token-incorrecto"})
    assert response.status_code == 401


def test_should_return_400_when_the_role_is_not_allowed(secured_app):
    """Un rol fuera del conjunto permitido se rechaza con 400."""
    client = TestClient(secured_app)
    response = client.post(
        "/scenarios/run",
        json={"scenario_data": _scenario_payload(), "role": "superadmin", "user": "tester"},
        headers={"Authorization": f"Bearer {SERVICE_KEY}"},
    )
    assert response.status_code == 400


def test_should_return_400_when_the_federation_contains_shell_metacharacters(secured_app, scenario_dirs):
    """Una ``federation`` con metacaracteres de shell se rechaza con 400."""
    client = TestClient(secured_app)
    response = client.post(
        "/scenarios/run",
        json={
            "scenario_data": _scenario_payload("x; touch /tmp/pwn"),
            "role": "admin",
            "user": "tester",
        },
        headers={"Authorization": f"Bearer {SERVICE_KEY}"},
    )
    assert response.status_code == 400


def test_should_fail_to_start_when_production_has_no_service_token(monkeypatch):
    """En produccion el controller no arranca sin token de servicio."""
    monkeypatch.setenv("NEBULA_PRODUCTION", "True")
    monkeypatch.delenv("NEBULA_CONTROLLER_TOKEN", raising=False)
    import nebula.controller.controller as controller

    importlib.reload(controller)
    with pytest.raises(RuntimeError):
        with TestClient(controller.app):
            pass


def test_should_reject_a_federation_with_shell_metacharacters(scenario_dirs, tmp_path):
    """La creacion del escenario falla y no ejecuta nada en el shell."""
    from nebula.controller.scenarios import ScenarioManagement

    sentinel = tmp_path / "pwn"
    with pytest.raises(ValueError):
        ScenarioManagement(_scenario_payload(f"x; touch {sentinel}"), "tester")
    assert not sentinel.exists()


def test_should_reject_a_federation_with_a_path_traversal(scenario_dirs, tmp_path):
    """La creacion del escenario falla y no escribe fuera del directorio de config."""
    from nebula.controller.scenarios import ScenarioManagement

    outside = tmp_path / "outside"
    with pytest.raises(ValueError):
        ScenarioManagement(_scenario_payload("../../outside/pwn"), "tester")
    assert not outside.exists()
    assert list((tmp_path / "config").iterdir()) == []


def test_should_use_the_sanitised_federation_in_the_scenario_directory_name(scenario_dirs, tmp_path):
    """Con una ``federation`` valida, el directorio usa el valor saneado."""
    from nebula.controller.scenarios import ScenarioManagement

    management = ScenarioManagement(_scenario_payload("DFL"), "tester")
    assert management.scenario_name.startswith("nebula_DFL_")
    assert os.path.basename(management.config_dir) == management.scenario_name
    assert os.path.dirname(os.path.realpath(management.config_dir)) == os.path.realpath(
        str(tmp_path / "config")
    )
    assert os.path.isdir(management.config_dir)


def test_should_create_the_scenario_files_with_restrictive_permissions(scenario_dirs):
    """Los ficheros del escenario no son legibles por otros usuarios."""
    from nebula.controller.scenarios import ScenarioManagement

    management = ScenarioManagement(_scenario_payload("DFL"), "tester")
    assert stat.S_IMODE(os.stat(management.config_dir).st_mode) == 0o700
    scenario_file = os.path.join(management.config_dir, "scenario.json")
    assert stat.S_IMODE(os.stat(scenario_file).st_mode) == 0o600
