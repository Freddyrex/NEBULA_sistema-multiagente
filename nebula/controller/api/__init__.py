"""API REST con JWT, refresh tokens, Remember Me, login social y CRUD protegido (Workshop 5)."""
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from nebula.controller.api import settings

OPENAPI_TAGS = [
    {"name": "Public", "description": "Endpoints sin autenticación."},
    {"name": "Authentication", "description": "Registro, login, refresh, logout."},
    {"name": "Social authentication", "description": "Login con Google (OAuth 2.0 / OIDC)."},
    {"name": "Users", "description": "Usuario autenticado."},
    {"name": "Experiments", "description": "CRUD de experimentos protegido con JWT."},
]


def init_api() -> None:
    """Crea las tablas. Llamar una vez al arrancar (lifespan)."""
    from nebula.controller.api.database import init_db

    init_db()


def include_api(app: FastAPI) -> None:
    """Registra middleware y routers en una aplicación FastAPI existente."""
    from nebula.controller.api.routers import auth, experiments, public, social, users

    # Authlib guarda el `state` de OAuth en una cookie de sesión firmada.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.SESSION_SECRET,
        same_site="lax",
        https_only=settings.HTTPS_ONLY,
        session_cookie="nebula_api_oauth",
    )
    for module in (public, auth, social, users, experiments):
        app.include_router(module.router)
