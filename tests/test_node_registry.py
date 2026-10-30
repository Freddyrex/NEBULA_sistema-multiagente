"""Tests for the static node registry that replaces Tailscale discovery."""
import json
from pathlib import Path

import pytest

from nebula.config.defaults import (
    _normalise_nodes,
    _parse_nodes,
    host_part,
    load_node_registry,
    node_ips,
)


@pytest.fixture(autouse=True)
def _clear_registry_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEBULA_NODES", raising=False)
    monkeypatch.delenv("NEBULA_NODE_REGISTRY_FILE", raising=False)


def test_parse_json_object() -> None:
    raw = json.dumps({"pi1": "192.168.1.11:8000", "pi2": "192.168.1.12:8000"})
    assert _parse_nodes(raw) == {
        "pi1": "192.168.1.11:8000",
        "pi2": "192.168.1.12:8000",
    }


def test_parse_csv_pairs() -> None:
    assert _parse_nodes("pi1=192.168.1.11:8000, pi2=192.168.1.12:8000") == {
        "pi1": "192.168.1.11:8000",
        "pi2": "192.168.1.12:8000",
    }


def test_parse_empty_or_invalid() -> None:
    assert _parse_nodes("") == {}
    assert _parse_nodes("   ") == {}
    assert _parse_nodes("not a registry") == {}


def test_normalise_list_payload() -> None:
    data = [
        {"name": "pi1", "host": "10.0.0.1"},
        {"name": "pi2", "ip": "10.0.0.2:8000"},
    ]
    assert _normalise_nodes(data) == {"pi1": "10.0.0.1", "pi2": "10.0.0.2:8000"}


def test_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEBULA_NODES", '{"pi1": "10.0.0.1:8000"}')
    assert load_node_registry() == {"pi1": "10.0.0.1:8000"}
    assert node_ips() == ["10.0.0.1"]


def test_load_from_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = tmp_path / "nodes.json"
    registry.write_text(json.dumps({"pi1": "10.0.0.2:8000"}), encoding="utf-8")
    monkeypatch.setenv("NEBULA_NODE_REGISTRY_FILE", str(registry))
    assert load_node_registry() == {"pi1": "10.0.0.2:8000"}
    assert node_ips() == ["10.0.0.2"]


def test_missing_file_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEBULA_NODE_REGISTRY_FILE", str(tmp_path / "missing.json"))
    assert load_node_registry() == {}


def test_env_takes_precedence_over_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = tmp_path / "nodes.json"
    registry.write_text(json.dumps({"file": "10.0.0.3"}), encoding="utf-8")
    monkeypatch.setenv("NEBULA_NODE_REGISTRY_FILE", str(registry))
    monkeypatch.setenv("NEBULA_NODES", '{"env": "10.0.0.4"}')
    assert load_node_registry() == {"env": "10.0.0.4"}


def test_node_ips_deduplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "NEBULA_NODES",
        '{"a": "10.0.0.1:8000", "b": "10.0.0.1:9000", "c": "10.0.0.2:8000"}',
    )
    assert node_ips() == ["10.0.0.1", "10.0.0.2"]


def test_host_part() -> None:
    assert host_part("http://192.168.1.11:8000") == "192.168.1.11"
    assert host_part("192.168.1.11:8000") == "192.168.1.11"
    assert host_part("192.168.1.11") == "192.168.1.11"
