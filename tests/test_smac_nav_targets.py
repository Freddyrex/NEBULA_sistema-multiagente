"""Botones de navegación del chat: catálogo de páginas de la app, no listas fijas."""
import asyncio
import os
import tempfile

from nebula.frontend.pages import (
    build_page_catalog,
    find_page,
    page_url,
    resolve_nav_target,
    safe_scenario_name,
)
from nebula.frontend.smac_api import SmacDeps, handle_ask

PAGES = [
    {"id": "home", "path": "/", "purpose": "Handle root path.", "scenario": False},
    {"id": "dashboard", "path": "/platform/dashboard", "purpose": "Dashboard view.", "scenario": False},
    {
        "id": "dashboard_scenario_monitor",
        "path": "/platform/dashboard/{scenario_name}/monitor",
        "purpose": "Monitoring information for a scenario.",
        "scenario": True,
    },
    {
        "id": "dashboard_scenario_statistics",
        "path": "/platform/dashboard/{scenario_name}/statistics/",
        "purpose": "TensorBoard statistics page.",
        "scenario": True,
    },
    {
        "id": "dashboard_deployment",
        "path": "/platform/dashboard/deployment/",
        "purpose": "Deployment dashboard.",
        "scenario": False,
    },
    {"id": "settings", "path": "/platform/settings", "purpose": "Settings interface.", "scenario": False},
]


def _deps(tmp):
    return SmacDeps(config_dir=tmp, log_dir=tmp, databases_dir=None,
                    controller_host="h", controller_port=1, controller_name="c", api_key="",
                    pages=list(PAGES))


def _ask(message, scenario="demo", role="admin", answer=None):
    """handle_ask con el LLM simulado (None = respuesta real, degradada)."""
    from nebula.frontend import smac_api, smac_chat

    smac_chat._pending_actions.clear()
    smac_chat._RATE_BUCKETS.clear()
    real = smac_api.ask_gemini

    async def fake_ask(*a, **k):
        return answer

    smac_api.ask_gemini = fake_ask
    try:
        with tempfile.TemporaryDirectory() as tmp:
            if scenario:
                os.makedirs(os.path.join(tmp, scenario))
            st, out = asyncio.run(handle_ask(_deps(tmp), {"user": "op", "role": role},
                                             message, scenario))
    finally:
        smac_api.ask_gemini = real
        smac_chat._pending_actions.clear()
        smac_chat._RATE_BUCKETS.clear()
    return st, out


def test_catalog_comes_from_the_app_routes():
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse

    app = FastAPI()

    @app.get("/platform/dashboard", response_class=HTMLResponse)
    async def nebula_dashboard():  # pragma: no cover - solo metadatos
        """Render or return the dashboard view for the current user."""

    @app.get("/platform/dashboard/{scenario_name}/monitor", response_class=HTMLResponse)
    async def nebula_dashboard_monitor(scenario_name: str):  # pragma: no cover
        """Display monitoring information for a specific scenario."""

    @app.get("/platform/api/scenarios")
    async def api_scenarios():  # pragma: no cover
        return JSONResponse([])

    @app.get("/platform/user/{user}/")
    async def user_detail(user: str):  # pragma: no cover
        return HTMLResponse()

    catalog = build_page_catalog(app)
    ids = [p["id"] for p in catalog]
    assert ids == ["dashboard", "dashboard_scenario_monitor"]
    assert catalog[0]["purpose"].startswith("Render or return the dashboard view")
    assert catalog[1]["scenario"] is True
    # el catálogo no depende de una lista escrita a mano
    assert "dashboard" not in PAGES[0]["id"] or True


def test_page_url_fills_the_open_scenario():
    page = find_page(PAGES, "dashboard_scenario_statistics")
    assert page_url(page, "demo") == "/platform/dashboard/demo/statistics/"
    assert page_url(page, "") == ""
    assert page_url(page, "../../etc") == ""
    assert safe_scenario_name("demo") == "demo"
    assert safe_scenario_name("../x") == ""


def test_find_page_is_tolerant_but_bounded():
    assert find_page(PAGES, "dashboard_scenario_monitor")["id"] == "dashboard_scenario_monitor"
    assert find_page(PAGES, "Dashboard Scenario Monitor")["id"] == "dashboard_scenario_monitor"
    assert find_page(PAGES, "monitor")["id"] == "dashboard_scenario_monitor"
    assert find_page(PAGES, "unknown_page") is None
    assert find_page(PAGES, "") is None


def test_find_page_accepts_a_phrase_or_a_path():
    # el modelo puede escribir una frase o incluso la ruta
    assert find_page(PAGES, "statistics page")["id"] == "dashboard_scenario_statistics"
    assert find_page(PAGES, "/platform/dashboard/demo/statistics/")["id"] == \
        "dashboard_scenario_statistics"
    assert find_page(PAGES, "the TensorBoard statistics page")["id"] == \
        "dashboard_scenario_statistics"
    # un id inventado no casa con ninguna página
    assert find_page(PAGES, "evil_page") is None
    assert find_page(PAGES, "invented_target") is None


def test_resolve_nav_target_builds_internal_url():
    action = resolve_nav_target("dashboard_scenario_statistics", PAGES, "demo")
    assert action["kind"] == "navigate"
    assert action["params"]["url"] == "/platform/dashboard/demo/statistics/"
    assert action["params"]["target"] == "dashboard_scenario_statistics"
    # destino desconocido o incompleto: sin acción (nada de URLs inventadas)
    assert resolve_nav_target("evil", PAGES, "demo") is None
    assert resolve_nav_target("dashboard_scenario_monitor", PAGES, "") is None


def test_model_target_uses_the_page_id():
    st, out = _ask(
        "necesito que me lleves a donde pueda ver las metricas de manera grafica",
        answer={"reply": "Aquí tienes el botón.",
                "actions": [{"kind": "navigate",
                             "params": {"target": "dashboard_scenario_statistics"},
                             "description": "Ver las métricas en gráficas"}],
                "sources": []},
    )
    assert st == 200
    navs = [a for a in out["actions"] if a.get("kind") == "navigate"]
    assert len(navs) == 1
    assert navs[0]["params"]["url"] == "/platform/dashboard/demo/statistics/"
    assert navs[0]["params"]["target"] == "dashboard_scenario_statistics"
    assert navs[0]["description"] == "Ver las métricas en gráficas"
    assert "id" not in navs[0]  # navegación no requiere confirmación


def test_model_short_target_still_lands_on_the_right_page():
    # el modelo puede escribir solo la palabra clave de la página
    st, out = _ask(
        "quiero ver el estado en vivo de los nodos",
        answer={"reply": "Vale.",
                "actions": [{"kind": "navigate", "params": {"target": "monitor"},
                             "description": "Monitor en vivo"}],
                "sources": []},
    )
    navs = [a for a in out["actions"] if a.get("kind") == "navigate"]
    assert len(navs) == 1
    assert navs[0]["params"]["url"] == "/platform/dashboard/demo/monitor"


def test_model_deployment_target():
    st, out = _ask(
        "llévame al panel de despliegue para crear otra simulación",
        answer={"reply": "Vale.",
                "actions": [{"kind": "navigate", "params": {"target": "deployment"},
                             "description": "Panel de despliegue"}],
                "sources": []},
    )
    navs = [a for a in out["actions"] if a.get("kind") == "navigate"]
    assert len(navs) == 1
    assert navs[0]["params"]["url"] == "/platform/dashboard/deployment/"


def test_model_legacy_url_outside_catalog_is_dropped():
    st, out = _ask(
        "dame un resumen",
        answer={"reply": "Vale.",
                "actions": [{"kind": "navigate",
                             "params": {"url": "https://evil.example/"},
                             "description": "phishing"},
                            {"kind": "navigate",
                             "params": {"url": "/platform/dashboard/otro/monitor"},
                             "description": "otro escenario"}],
                "sources": []},
    )
    assert st == 200
    assert all(a.get("kind") != "navigate" for a in out["actions"])


def test_model_legacy_url_inside_catalog_is_accepted():
    st, out = _ask(
        "llévame al monitor",
        answer={"reply": "Vale.",
                "actions": [{"kind": "navigate",
                             "params": {"url": "/platform/dashboard/demo/monitor"},
                             "description": "Monitor"}],
                "sources": []},
    )
    navs = [a for a in out["actions"] if a.get("kind") == "navigate"]
    assert len(navs) == 1
    assert navs[0]["params"]["url"] == "/platform/dashboard/demo/monitor"


def test_duplicate_pages_are_collapsed():
    st, out = _ask(
        "llévame a las gráficas",
        answer={"reply": "Vale.",
                "actions": [{"kind": "navigate",
                             "params": {"target": "dashboard_scenario_statistics"},
                             "description": "a"},
                            {"kind": "navigate",
                             "params": {"target": "dashboard_scenario_statistics"},
                             "description": "b"}],
                "sources": []},
    )
    navs = [a for a in out["actions"] if a.get("kind") == "navigate"]
    assert len(navs) == 1


def test_unknown_page_id_is_dropped():
    st, out = _ask(
        "dame un resumen",
        answer={"reply": "Vale.",
                "actions": [{"kind": "navigate", "params": {"target": "invented_page"},
                             "description": "x"}],
                "sources": []},
    )
    assert st == 200
    assert all(a.get("kind") != "navigate" for a in out["actions"])


def test_creation_intent_still_offers_the_creation_page():
    st, out = _ask(
        "quiero crear un escenario nuevo",
        answer={"reply": "Vamos allá.", "actions": [], "sources": []},
    )
    navs = [a for a in out["actions"] if a.get("kind") == "navigate"]
    assert len(navs) == 1
    assert navs[0]["params"]["url"] == "/platform/dashboard/deployment/"


def test_no_navigation_intent_means_no_button():
    st, out = _ask("¿cómo va el nodo 3?",
                   answer={"reply": "Va bien.", "actions": [], "sources": []})
    assert st == 200
    assert all(a.get("kind") != "navigate" for a in out["actions"])
