"""Configuración de la API de autenticación. Todo se lee del entorno / .env."""
import os

from dotenv import load_dotenv

load_dotenv()  # lee .env si existe (en Docker las variables ya vienen en el entorno)


def _default_database_url() -> str:
    databases_dir = os.environ.get("NEBULA_DATABASES_DIR", "./app/databases")
    os.makedirs(databases_dir, exist_ok=True)
    return f"sqlite:///{os.path.join(databases_dir, 'api.db')}"


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Falta la variable de entorno {name}. Defínela en tu .env")
    if str(value).startswith("change-me"):
        raise RuntimeError(f"La variable de entorno {name} tiene un valor de ejemplo: debe generarse")
    return value


# --- Base de datos -----------------------------------------------------------
DATABASE_URL: str = os.getenv("DATABASE_URL") or _default_database_url()

# --- JWT ---------------------------------------------------------------------
JWT_SECRET: str = _required("JWT_SECRET")
JWT_ALGORITHM: str = "HS256"
ACCESS_TOKEN_MINUTES: int = int(os.getenv("ACCESS_TOKEN_MINUTES", "15"))
REFRESH_TOKEN_DAYS: int = int(os.getenv("REFRESH_TOKEN_DAYS", "7"))
REMEMBER_REFRESH_TOKEN_DAYS: int = int(os.getenv("REMEMBER_REFRESH_TOKEN_DAYS", "30"))

# --- Sesión (solo la usa Authlib para guardar el `state` de OAuth) -----------
SESSION_SECRET: str = os.getenv("SESSION_SECRET") or JWT_SECRET
HTTPS_ONLY: bool = os.getenv("NEBULA_PRODUCTION", "False") == "True"

# --- Google OAuth 2.0 / OpenID Connect ---------------------------------------
GOOGLE_CLIENT_ID: str | None = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET: str | None = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI: str | None = os.getenv("GOOGLE_REDIRECT_URI")
