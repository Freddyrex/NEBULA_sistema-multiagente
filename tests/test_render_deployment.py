"""Tests for the Render/PaaS deployment path.

Covers:
- ``controller_base_url`` builds HTTPS URLs from host/port, from an absolute
  public endpoint, and from the Docker Compose fallback.
- The frontend resolves ``templates/`` and ``static/`` relative to the module,
  not to the current working directory.
- The controller resolves its databases/log paths when the environment does not
  provide them and creates the parent directories.
- ``/available_gpus/`` always returns the expected shape on CPU-only hosts.
- A docker deployment is rejected with 501 when no Docker daemon is reachable.
"""
import os
import pathlib
import secrets
import sys
import tempfile

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONTEND_DIR = REPO_ROOT / "nebula" / "frontend"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="module")
def frontend():
    """Import the frontend module with an isolated log file."""
    tmp_dir = tempfile.mkdtemp(prefix="nebula-render-frontend-")
    os.environ["SECRET_KEY"] = secrets.token_hex(16)
    os.environ["NEBULA_FRONTEND_LOG"] = os.path.join(tmp_dir, "frontend.log")
    os.environ["NEBULA_ENV_PATH"] = os.path.join(tmp_dir, "frontend.env")
    import nebula.frontend.app as app_module

    return app_module


@pytest.fixture(scope="module")
def controller():
    """Import the controller module with an isolated database directory."""
    tmp_dir = tempfile.mkdtemp(prefix="nebula-render-controller-")
    os.environ["JWT_SECRET"] = secrets.token_hex(16)
    os.environ["NEBULA_DATABASES_DIR"] = os.path.join(tmp_dir, "databases")
    os.environ["NEBULA_CONTROLLER_LOG"] = os.path.join(tmp_dir, "logs", "controller.log")
    import nebula.controller.controller as controller_module

    return controller_module


@pytest.fixture
def clean_controller_env(monkeypatch):
    """Remove every controller URL variable before a test."""
    for key in (
        "NEBULA_CONTROLLER_PUBLIC_ENDPOINT",
        "NEBULA_CONTROLLER_SCHEME",
        "NEBULA_CONTROLLER_HOST",
        "NEBULA_CONTROLLER_PORT",
    ):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_controller_base_url_uses_public_endpoint(frontend, clean_controller_env):
    """An absolute public endpoint wins over host and port."""
    clean_controller_env.setenv("NEBULA_CONTROLLER_PUBLIC_ENDPOINT", "https://nebula-controller.onrender.com/")
    clean_controller_env.setenv("NEBULA_CONTROLLER_HOST", "ignored")

    assert frontend.controller_base_url() == "https://nebula-controller.onrender.com"


def test_controller_base_url_uses_https_without_default_port(frontend, clean_controller_env):
    """HTTPS on port 443 omits the port from the URL."""
    clean_controller_env.setenv("NEBULA_CONTROLLER_SCHEME", "https")
    clean_controller_env.setenv("NEBULA_CONTROLLER_HOST", "nebula-controller.onrender.com")
    clean_controller_env.setenv("NEBULA_CONTROLLER_PORT", "443")

    assert frontend.controller_base_url() == "https://nebula-controller.onrender.com"


def test_controller_base_url_keeps_explicit_port(frontend, clean_controller_env):
    """A non-default port is preserved."""
    clean_controller_env.setenv("NEBULA_CONTROLLER_SCHEME", "http")
    clean_controller_env.setenv("NEBULA_CONTROLLER_HOST", "127.0.0.1")
    clean_controller_env.setenv("NEBULA_CONTROLLER_PORT", "5050")

    assert frontend.controller_base_url() == "http://127.0.0.1:5050"


def test_frontend_assets_are_module_relative(frontend):
    """Static files and templates do not depend on the working directory."""
    assert frontend.BASE_DIR == FRONTEND_DIR
    assert pathlib.Path(frontend.settings.templates_dir) == FRONTEND_DIR / "templates"
    assert (FRONTEND_DIR / "static").is_dir()


def test_frontend_log_path_is_writable(frontend):
    """The resolved log file always points to an existing directory."""
    log_path = pathlib.Path(frontend.settings.frontend_log)
    assert log_path.parent.is_dir()


def test_controller_creates_databases_and_log_parents(controller, tmp_path, monkeypatch):
    """The controller creates the directories it needs at startup."""
    monkeypatch.delenv("NEBULA_DATABASES_DIR", raising=False)
    monkeypatch.delenv("NEBULA_CONTROLLER_LOG", raising=False)
    monkeypatch.chdir(tmp_path)

    import asyncio

    async def _run():
        async with controller.lifespan(controller.app):
            pass

    asyncio.run(_run())

    assert (tmp_path / "app" / "databases").is_dir()
    assert (tmp_path / "app" / "logs").is_dir()


def test_available_gpus_returns_expected_shape(controller):
    """CPU-only hosts still return a dict with the GPU list."""
    import asyncio

    result = asyncio.run(controller.get_available_gpu())

    assert isinstance(result, dict)
    assert isinstance(result["available_gpus"], list)


def test_docker_deployment_is_rejected_without_daemon(controller, monkeypatch):
    """A docker scenario on a host without Docker fails with 501."""
    monkeypatch.setattr(controller, "_docker_available", lambda: False)

    with pytest.raises(HTTPException) as excinfo:
        controller._assert_deployment_supported({"deployment": "docker"})

    assert excinfo.value.status_code == 501


def test_physical_deployment_is_allowed(controller, monkeypatch):
    """Physical deployments are not blocked by the Docker check."""
    monkeypatch.setattr(controller, "_docker_available", lambda: False)

    controller._assert_deployment_supported({"deployment": "physical", "physical_ips": ["10.0.0.1"]})


def test_frontend_base_url_supports_public_endpoint(controller, monkeypatch):
    """The controller reaches the frontend through the configured endpoint."""
    monkeypatch.setenv("NEBULA_FRONTEND_PUBLIC_ENDPOINT", "https://nebula-frontend.onrender.com")

    assert controller._frontend_base_url() == "https://nebula-frontend.onrender.com"


def test_frontend_base_url_supports_host_and_scheme(controller, monkeypatch):
    """Host and scheme are enough to build the frontend URL."""
    monkeypatch.delenv("NEBULA_FRONTEND_PUBLIC_ENDPOINT", raising=False)
    monkeypatch.setenv("NEBULA_FRONTEND_HOST", "nebula-frontend.onrender.com")
    monkeypatch.setenv("NEBULA_FRONTEND_SCHEME", "https")
    monkeypatch.setenv("NEBULA_FRONTEND_PORT", "443")

    assert controller._frontend_base_url() == "https://nebula-frontend.onrender.com"


def test_controller_token_is_required_in_production(controller, monkeypatch):
    """Production without a service token refuses to start."""
    monkeypatch.setenv("NEBULA_PRODUCTION", "True")
    monkeypatch.delenv("NEBULA_CONTROLLER_TOKEN", raising=False)

    with pytest.raises(RuntimeError):
        controller._assert_controller_token_configured()


def test_controller_status_endpoint_is_public_with_token(controller):
    """The controller answers /status when the service token is valid."""
    client = TestClient(controller.app)
    response = client.get("/status", headers={"Authorization": "Bearer render-test-token"})

    assert response.status_code == 200
    assert "running" in response.json()["status"]
