"""Formato de la respuesta del chat SMAC: texto plano, no markdown estructurado."""

import asyncio

from nebula.core import smac_gemini
from nebula.frontend.smac_chat import _naturalize_reply, ask_gemini, deterministic_reply


def test_naturalize_reply_strips_markdown_to_plain_prose():
    raw = (
        "## **Riesgos**: accuracy baja\n"
        "\n"
        "- Accuracy cayó a 0.8 en la ronda 12\n"
        "\n"
        "1. Sugerencia: cambiar el agregador\n"
        "\n"
        "`node 3` offline\n"
    )
    assert _naturalize_reply(raw) == (
        "Riesgos: accuracy baja\n"
        "\n"
        "Accuracy cayó a 0.8 en la ronda 12\n"
        "\n"
        "Sugerencia: cambiar el agregador\n"
        "\n"
        "node 3 offline"
    )


def test_naturalize_reply_keeps_plain_prose_intact():
    assert _naturalize_reply("La accuracy va por 0.87 en la ronda 12.") == ("La accuracy va por 0.87 en la ronda 12.")


def test_deterministic_reply_is_plain_prose():
    state = {
        "scenario": "demo",
        "status": "running",
        "round": 6,
        "nodes": [
            {"idx": "0", "state": "running"},
            {"idx": "1", "state": "offline"},
        ],
        "config": {"aggregation": "FedAvg"},
        "metrics_summary": {"metrics": {}, "available": [], "warnings": []},
    }
    reply = deterministic_reply(state, "")["reply"]
    assert "**" not in reply
    assert "`" not in reply
    assert not any(line.lstrip().startswith(("- ", "1. ", "#")) for line in reply.splitlines())
    assert "offline" in reply.lower()


def test_ask_gemini_naturalizes_llm_reply(monkeypatch):
    async def fake_gemini_json_completion(**_kwargs):
        return {
            "reply": "## **Riesgos**: accuracy baja\n\n- Accuracy cayó a 0.8 en la ronda 12\n",
            "actions": [],
            "sources": [],
        }

    monkeypatch.setattr(smac_gemini, "gemini_json_completion", fake_gemini_json_completion)
    data = asyncio.run(ask_gemini("cómo va el escenario", {"scenario": "demo"}, "k"))
    assert data is not None
    assert data["reply"] == "Riesgos: accuracy baja\n\nAccuracy cayó a 0.8 en la ronda 12"
    assert data["actions"] == []
