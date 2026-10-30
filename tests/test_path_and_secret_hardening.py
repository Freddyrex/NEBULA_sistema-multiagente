"""Security regression tests for path containment, command injection and secret leakage."""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from nebula.config.mender import Mender
from nebula.core.situationalawareness.awareness.cognitive.gemini_reasoning_agent import resolve_agent_api_key
from nebula.utils import FileUtils


def _physical_api() -> Any:
    """Return the physical API module, skipping the test when Flask is unavailable."""
    try:
        from nebula.physical import api
    except ImportError as exc:  # pragma: no cover - depends on the installed extras
        pytest.skip(f"Flask is required to exercise the physical API: {exc}")
    return api


@pytest.mark.parametrize(
    "relative_path",
    [
        "/app/logs_secret/x",
        "/app/logsX",
        "../logs_secret/x",
        "../logsX",
    ],
)
def test_should_reject_path_when_relative_path_escapes_the_base_directory(relative_path: str) -> None:
    with pytest.raises(Exception, match="Not allowed"):
        FileUtils.check_path("/app/logs", relative_path)


def test_should_accept_path_when_relative_path_stays_inside_the_base_directory(tmp_path: Path) -> None:
    base = tmp_path / "logs"
    base.mkdir()

    result = FileUtils.check_path(str(base), "x")

    assert result == os.path.join(str(base), "x")
    assert os.path.commonpath([os.path.realpath(result), os.path.realpath(str(base))]) == os.path.realpath(str(base))


def test_should_build_argv_list_when_generating_a_mender_artifact(tmp_path: Path) -> None:
    image = str(tmp_path / "image.img")
    command = Mender.build_artifact_command("rootfs", "artifact", "x; touch /tmp/pwn; #", image)

    assert isinstance(command, list)
    assert command == [
        "mender-artifact",
        "write",
        "module-image",
        "-T",
        "rootfs",
        "-n",
        "artifact",
        "-t",
        "x; touch /tmp/pwn; #",
        "-o",
        "artifact.mender",
        "-f",
        image,
    ]


def test_should_not_execute_shell_metacharacters_when_device_type_is_injected(tmp_path: Path, monkeypatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_binary = bin_dir / "mender-artifact"
    fake_binary.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$MENDER_ARGS_FILE"\n')
    fake_binary.chmod(0o755)

    args_file = tmp_path / "args.txt"
    marker = tmp_path / "pwn"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("MENDER_ARGS_FILE", str(args_file))

    Mender.generate_artifact("rootfs", "artifact", f"x; touch {marker}; #", str(tmp_path / "image.img"))

    assert not marker.exists()
    assert f"x; touch {marker}; #" in args_file.read_text().splitlines()


def test_should_raise_value_error_when_config_carries_an_inline_api_key() -> None:
    with pytest.raises(ValueError, match="api_key_env_var"):
        resolve_agent_api_key({"api_key": "inline-secret"})


def test_should_resolve_api_key_from_the_environment_when_env_var_is_configured(monkeypatch) -> None:
    monkeypatch.setenv("NEBULA_TEST_GEMINI_KEY", "env-secret")

    assert resolve_agent_api_key({"api_key_env_var": "NEBULA_TEST_GEMINI_KEY"}) == "env-secret"


def test_should_reject_upload_when_filename_contains_path_traversal(tmp_path: Path, monkeypatch) -> None:
    api = _physical_api()
    monkeypatch.delenv("NEBULA_PHYSICAL_TOKEN", raising=False)
    config_dir = tmp_path / "config"
    (config_dir / "scenario_1").mkdir(parents=True)
    monkeypatch.setattr(api, "CONFIG_FOLDER", str(config_dir))
    monkeypatch.setattr(api, "CERTS_FOLDER", str(tmp_path / "certs"))
    client = api.app.test_client()

    response = client.put(
        "/config/?path=scenario_1",
        data={"config": (io.BytesIO(b"{}"), "../../evil.json")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert not (tmp_path / "evil.json").exists()
    assert not (tmp_path.parent / "evil.json").exists()


def test_should_return_401_when_bearer_token_is_missing_and_a_token_is_configured(tmp_path: Path, monkeypatch) -> None:
    api = _physical_api()
    monkeypatch.setenv("NEBULA_PHYSICAL_TOKEN", "s3cr3t-token")
    monkeypatch.setattr(api, "CONFIG_FOLDER", str(tmp_path))
    client = api.app.test_client()

    response = client.get("/config/?path=scenario_1")

    assert response.status_code == 401
    assert "s3cr3t-token" not in response.get_data(as_text=True)


def test_should_accept_request_when_bearer_token_matches_the_configured_token(tmp_path: Path, monkeypatch) -> None:
    api = _physical_api()
    monkeypatch.setenv("NEBULA_PHYSICAL_TOKEN", "s3cr3t-token")
    monkeypatch.setattr(api, "CONFIG_FOLDER", str(tmp_path))
    client = api.app.test_client()

    response = client.get("/config/?path=scenario_1", headers={"Authorization": "Bearer s3cr3t-token"})

    assert response.status_code != 401


def test_should_allow_request_when_no_bearer_token_is_configured(tmp_path: Path, monkeypatch) -> None:
    api = _physical_api()
    monkeypatch.delenv("NEBULA_PHYSICAL_TOKEN", raising=False)
    monkeypatch.setattr(api, "CONFIG_FOLDER", str(tmp_path))
    client = api.app.test_client()

    response = client.get("/config/?path=scenario_1")

    assert response.status_code != 401


def test_should_log_a_warning_once_when_no_bearer_token_is_configured(tmp_path: Path, monkeypatch, caplog) -> None:
    api = _physical_api()
    monkeypatch.delenv("NEBULA_PHYSICAL_TOKEN", raising=False)
    monkeypatch.setattr(api, "AUTH_WARNING_EMITTED", False)
    monkeypatch.setattr(api, "CONFIG_FOLDER", str(tmp_path))
    client = api.app.test_client()

    with caplog.at_level(logging.WARNING):
        client.get("/config/?path=scenario_1")
        client.get("/config/?path=scenario_1")

    warnings = [record.getMessage() for record in caplog.records if "NEBULA_PHYSICAL_TOKEN" in record.getMessage()]
    assert len(warnings) == 1


def test_should_not_log_the_bearer_token_when_the_request_is_rejected(tmp_path: Path, monkeypatch, caplog) -> None:
    api = _physical_api()
    monkeypatch.setenv("NEBULA_PHYSICAL_TOKEN", "s3cr3t-token")
    monkeypatch.setattr(api, "CONFIG_FOLDER", str(tmp_path))
    client = api.app.test_client()

    with caplog.at_level(logging.DEBUG):
        response = client.get("/config/?path=scenario_1", headers={"Authorization": "Bearer wrong-token"})

    assert response.status_code == 401
    assert "s3cr3t-token" not in caplog.text
    assert "wrong-token" not in caplog.text
