"""Catálogo de páginas de NEBULA construido desde las rutas reales de la app.

El chat puede llevar al operador a otra parte de la plataforma. Para no
mantener una lista fija de destinos, el catálogo se deriva de las rutas que la
propia app declara: solo las que renderizan HTML (las de API quedan fuera por
construcción) y de las que el backend sabe completar (los parámetros de
escenario se rellenan con el escenario abierto).

El LLM elige un **id** de página (nunca escribe una URL); el backend lo
resuelve contra este catálogo, de modo que nunca puede inventarse un destino
ni una URL externa.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi.responses import HTMLResponse

#: Un parámetro de ruta es un escenario cuando su nombre lo dice así.
_SCENARIO_PARAM_RE = re.compile(r"scenario", re.IGNORECASE)

#: Charset válido de un nombre de escenario (mismo que la API de métricas).
_SCENARIO_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

#: Caché por instancia de app (el catálogo solo cambia si cambian las rutas).
_CATALOGS: dict[int, list[dict]] = {}


def safe_scenario_name(scenario: str) -> str:
    """Nombre de escenario seguro para interpolar en una URL ('' si no sirve)."""
    name = str(scenario or "").strip()
    if not name or name in (".", "..") or not _SCENARIO_RE.fullmatch(name):
        return ""
    return name


def normalize_id(text: str) -> str:
    """Normaliza un id/etiqueta para comparar ('Dashboard Monitor' → 'dashboard_monitor')."""
    return re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")


def page_id(path: str) -> str:
    """Id legible y estable a partir de la ruta ('/platform/dashboard/...' → 'dashboard_...')."""
    parts: list[str] = []
    for segment in str(path or "").strip("/").split("/"):
        if not segment or segment == "platform":
            continue
        if segment.startswith("{"):
            name = segment.strip("{}")
            parts.append("scenario" if _SCENARIO_PARAM_RE.search(name) else "item")
        else:
            clean = re.sub(r"[^a-z0-9]+", "_", segment.lower()).strip("_")
            if clean:
                parts.append(clean)
    return "_".join(parts) or "home"


def page_purpose(endpoint: Any) -> str:
    """Primera frase del docstring de la ruta: para qué sirve la página."""
    doc = (getattr(endpoint, "__doc__", "") or "").strip()
    if not doc:
        return ""
    first = re.sub(r"\s+", " ", doc.splitlines()[0].strip())
    return first[:200]


def _is_html_route(route: Any) -> bool:
    """Whether the route declares an HTML response (API routes return JSON)."""
    response_class = getattr(route, "response_class", None)
    if response_class is None:
        return False
    if isinstance(response_class, type):
        return issubclass(response_class, HTMLResponse)
    return "html" in str(getattr(response_class, "__name__", "")).lower()


def build_page_catalog(app: Any) -> list[dict]:
    """Páginas HTML de la app como [{id, path, purpose, scenario}].

    Solo entran las rutas GET declaradas con ``response_class=HTMLResponse``
    (las de API devuelven JSON y quedan fuera por construcción) y las que el
    backend puede completar con el escenario abierto.
    """
    key = id(app)
    cached = _CATALOGS.get(key)
    if cached is not None:
        return cached
    pages: list[dict] = []
    seen: set[str] = set()
    try:
        routes = list(getattr(app, "routes", []) or [])
    except Exception:
        routes = []
    for route in routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        if "GET" not in (getattr(route, "methods", None) or set()):
            continue
        response_class = getattr(route, "response_class", None)
        if response_class is not None and not _is_html_route(route):
            continue
        path = str(getattr(route, "path", "") or "")
        if not (path == "/" or path.startswith("/platform")):
            continue
        params = re.findall(r"\{([^}]+)\}", path)
        if any(not _SCENARIO_PARAM_RE.search(p) for p in params):
            continue
        pid = page_id(path)
        if pid in seen:
            continue
        seen.add(pid)
        pages.append(
            {
                "id": pid,
                "path": path,
                "purpose": page_purpose(endpoint),
                "scenario": bool(params),
            }
        )
    _CATALOGS[key] = pages
    return pages


def page_url(page: dict, scenario: str = "") -> str:
    """URL concreta de una página del catálogo para el escenario abierto."""
    path = str((page or {}).get("path") or "")
    if not path:
        return ""
    if page.get("scenario"):
        name = safe_scenario_name(scenario)
        if not name:
            return ""
        path = re.sub(r"\{[^}]+\}", name, path)
    return path


def _tokens(text: str) -> set[str]:
    """Palabras significativas de un texto (para comparar de forma tolerante)."""
    return {t for t in re.split(r"[^a-z0-9]+", str(text or "").lower()) if len(t) > 2}


def _prefer(candidates: list[dict]) -> dict:
    """Ante varias coincidencias manda la página del escenario abierto."""
    specific = [p for p in candidates if p.get("scenario")]
    return (specific or candidates)[0]


def find_page(pages: list[dict], target: str) -> dict | None:
    """Página del catálogo que corresponde a lo que pidió el modelo.

    Primero exige coincidencia exacta (id o id normalizado); si no, acepta
    que el modelo escribiera una frase ("statistics page") o una ruta: basta
    con que alguna palabra coincida con el id de una página. Un id inventado
    ("evil_page") no casa con ninguna y se descarta.
    """
    wanted = normalize_id(target)
    if not wanted:
        return None
    exact = [p for p in (pages or []) if normalize_id(str(p.get("id") or "")) == wanted]
    if exact:
        return _prefer(exact)
    suffix = [p for p in (pages or []) if normalize_id(str(p.get("id") or "")).endswith("_" + wanted)]
    if suffix:
        return _prefer(suffix)
    tokens = _tokens(target)
    if not tokens:
        return None
    scored: list[tuple[int, dict]] = []
    for page in pages or []:
        overlap = len(tokens & _tokens(str(page.get("id") or "")))
        if overlap:
            scored.append((overlap, page))
    if not scored:
        return None
    best = max(overlap for overlap, _ in scored)
    return _prefer([page for overlap, page in scored if overlap == best])


def page_label(page: dict) -> str:
    """Etiqueta corta de una página (fallback cuando el modelo no la escribe)."""
    pid = normalize_id(str((page or {}).get("id") or ""))
    if not pid:
        return ""
    return " ".join(word.capitalize() for word in pid.split("_"))


def resolve_nav_target(target: str, pages: list[dict], scenario: str = "") -> dict | None:
    """Construye la acción 'navigate' a partir de un id de página del catálogo.

    El id lo elige el modelo; la URL la construye el backend. Un id
    desconocido o una página que no se puede completar (falta el escenario)
    devuelven None: nunca se navega a una URL inventada.
    """
    page = find_page(pages, target)
    if not page:
        return None
    url = page_url(page, scenario)
    if not url:
        return None
    return {
        "kind": "navigate",
        "params": {"url": url, "target": page.get("id")},
        "description": page_label(page),
    }
