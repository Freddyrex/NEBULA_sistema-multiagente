"""Security regression tests for SMAC resume/draft identifiers and exec-interpreter scenarios."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from nebula.frontend import smac_chat
from nebula.frontend.exec_interpreter import adaptive_flags, read_audit_tail


def test_should_reject_resume_path_with_traversal_when_identifier_contains_slashes() -> None:
    """A resume identifier with path separators is rejected before any file is opened."""
    with tempfile.TemporaryDirectory() as tmp:
        with pytest.raises(ValueError):
            smac_chat._validate_resume(tmp, {"parent": "../../../etc/passwd", "checkpoint_round": 1})
        with pytest.raises(ValueError):
            smac_chat._drafts_dir(tmp, "../../../etc/passwd")
        ok, reason, _child = smac_chat._validate_resume(tmp, {"parent": "abc-123", "checkpoint_round": 1})
        assert ok is False and "no encontrado" in reason


def test_should_reject_scenario_with_path_separators_when_building_paths() -> None:
    """A scenario identifier with path separators resolves to the safe empty value."""
    with tempfile.TemporaryDirectory() as tmp:
        off = {
            "exec_interpreter": False,
            "aggregator_suggestions": False,
            "architecture_suggestions": False,
            "pause_on_suggestion": True,
            "aggregator_auto": False,
        }
        assert adaptive_flags(tmp, "a/b") == off
        assert read_audit_tail(tmp, "a/b") == []
        assert adaptive_flags(tmp, "..") == off
        assert read_audit_tail(tmp, "../..") == []


def test_should_accept_valid_scenario_when_name_is_safe() -> None:
    """A plain scenario name keeps resolving flags and audit tail as before."""
    with tempfile.TemporaryDirectory() as tmp:
        scenario_dir = os.path.join(tmp, "demo")
        os.makedirs(scenario_dir)
        with open(os.path.join(scenario_dir, "scenario.json"), "w") as f:
            json.dump({"adaptive_agent": {"exec_interpreter": True}}, f)
        assert adaptive_flags(tmp, "demo")["exec_interpreter"] is True
        with open(os.path.join(tmp, "demo", "smac_audit.jsonl"), "w") as f:
            f.write('{"event": "action_executed", "payload": {}}\n')
        records = read_audit_tail(tmp, "demo")
        assert len(records) == 1 and records[0]["event"] == "action_executed"
