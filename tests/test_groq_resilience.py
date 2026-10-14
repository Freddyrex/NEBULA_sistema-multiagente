"""Resiliencia Groq: clave por fichero + guard anti-413."""
import json
import os
import tempfile

from nebula.frontend.smac_chat import (
    MAX_SNAPSHOT_BYTES,
    resolve_groq_key,
    shrink_snapshot_for_llm,
)


def test_resolve_groq_key_precedence_and_file():
    assert resolve_groq_key("  gsk_x  ") == "gsk_x"
    old_env, old_file = os.environ.get("GROQ_API_KEY"), os.environ.get("GROQ_APIKEY_FILE")
    try:
        os.environ.pop("GROQ_API_KEY", None)
        os.environ.pop("GROQ_APIKEY_FILE", None)
        with tempfile.TemporaryDirectory() as empty:
            here = os.getcwd()
            os.chdir(empty)
            try:
                assert resolve_groq_key("") == ""
            finally:
                os.chdir(here)
        with tempfile.TemporaryDirectory() as tmp:
            keyfile = os.path.join(tmp, "mykey")
            with open(keyfile, "w") as f:
                f.write("  gsk_filekey  \n")
            os.environ["GROQ_APIKEY_FILE"] = keyfile
            from nebula.frontend.smac_chat import resolve_groq_key as r2

            assert r2("") == "gsk_filekey"
    finally:
        if old_env is not None:
            os.environ["GROQ_API_KEY"] = old_env
        else:
            os.environ.pop("GROQ_API_KEY", None)
        if old_file is not None:
            os.environ["GROQ_APIKEY_FILE"] = old_file
        else:
            os.environ.pop("GROQ_APIKEY_FILE", None)


def _fat_state():
    return {
        "scenario": "d", "status": "running",
        "metrics_summary": {
            "metrics": {
                "accuracy": {"first": 0.5, "final": 0.9, "delta": 0.4,
                             "best": {"round": 9, "value": 0.9},
                             "ranking_final": [{"node_id": i, "value": 0.9} for i in range(50)],
                             "outliers": [{"node_id": i} for i in range(50)]},
            },
            "series": {"accuracy": [[r, 0.5] for r in range(2000)]},
        },
        "runtime": {"metrics_summary": {"series": {"loss": [[r, 0.1] for r in range(2000)]}}},
        "recent_decisions": [{"d": "x" * 2000} for _ in range(15)],
        "nodes": [{"idx": str(i)} for i in range(30)],
        "containers": [{"n": i} for i in range(30)],
        "findings": [{"type": "accuracy_plateau", "summary": "plateau"}],
        "suggestions": [{"title": "Cambiar FedAvg → Krum"}],
    }


def test_shrink_keeps_value_drops_bulk():
    fat = _fat_state()
    assert len(json.dumps(fat)) > MAX_SNAPSHOT_BYTES
    slim = shrink_snapshot_for_llm(fat)
    assert len(json.dumps(slim)) <= MAX_SNAPSHOT_BYTES
    assert slim["metrics_summary"]["metrics"]["accuracy"]["final"] == 0.9
    assert slim["metrics_summary"]["metrics"]["accuracy"]["best"] == {"round": 9, "value": 0.9}
    assert "series" not in slim["metrics_summary"]
    assert len(slim["metrics_summary"]["metrics"]["accuracy"]["ranking_final"]) == 3
    assert len(slim["recent_decisions"]) == 5
    assert len(slim["nodes"]) == 20
    assert slim["findings"][0]["type"] == "accuracy_plateau"
    assert slim["suggestions"][0]["title"].startswith("Cambiar")
    # el original cacheado no se muta
    assert len(fat["metrics_summary"]["metrics"]["accuracy"]["ranking_final"]) == 50


def test_shrink_small_state_passthrough_and_invalid():
    small = {"scenario": "d", "metrics_summary": {"metrics": {}}}
    assert shrink_snapshot_for_llm(small)["scenario"] == "d"
    assert shrink_snapshot_for_llm(None) == {}
    assert shrink_snapshot_for_llm("x") == {}
