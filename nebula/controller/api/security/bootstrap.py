"""Bootstrap de credenciales admin sin defaults hardcodeados (§1.2).

Fail-closed en producción, aleatorio en dev (solo se loguea una vez).

El **nombre** del administrador nunca está fijado en el código: se toma de
``NEBULA_DEFAULT_USER`` (configuración) y, en el arranque en desarrollo, se
genera uno único. Tras el arranque, la base de datos es la fuente de verdad
de la identidad (ver ``nebula.controller.database.get_platform_admin``).
"""

from __future__ import annotations

import logging
import os
import secrets

logger = logging.getLogger(__name__)

_DEV_USERNAME_PREFIX = "nebula-"


def generated_username() -> str:
    """Genera un nombre de usuario único para el arranque en desarrollo.

    Returns:
        str: Nombre generado, sin ninguna identidad fija en el código.
    """
    return f"{_DEV_USERNAME_PREFIX}{secrets.token_hex(3)}"


def require_admin_credentials() -> tuple[str, str]:
    """Devuelve (user, password) desde env o genera en dev.

    - Si NEBULA_PRODUCTION=True y faltan env -> RuntimeError.
    - En dev: genera password aleatorio si falta y un nombre único si no hay
      ninguno configurado (no existe ningun usuario por defecto en el código).

    Returns:
        tuple[str, str]: Usuario y password de bootstrap.
    """
    username = os.environ.get("NEBULA_DEFAULT_USER", "").strip()
    password = os.environ.get("NEBULA_DEFAULT_PASSWORD", "").strip()
    production = os.environ.get("NEBULA_PRODUCTION", "False") == "True"
    if production and (not username or not password):
        raise RuntimeError(
            "NEBULA_DEFAULT_USER y NEBULA_DEFAULT_PASSWORD deben definirse "
            "cuando NEBULA_PRODUCTION=True (sin credenciales por defecto)"
        )
    if not username or not password:
        if not username:
            username = generated_username()
        if not password:
            password = secrets.token_urlsafe(16)
            logger.warning("Password admin generado (solo se muestra una vez): %s", password)
        logger.warning(
            "Credenciales admin generadas para desarrollo (no usar en producción). "
            f"Usuario: {username} — define NEBULA_DEFAULT_USER/PASSWORD para fijarlas."
        )
    return username, password


def grafana_admin_password() -> str:
    """Password de Grafana desde la misma env (nunca literal `admin`)."""
    pwd = os.environ.get("GF_SECURITY_ADMIN_PASSWORD", "").strip()
    if not pwd:
        # Reutiliza el password admin de NEBULA si existe, si no genera.
        pwd = os.environ.get("NEBULA_DEFAULT_PASSWORD", "").strip()
    if not pwd:
        if os.environ.get("NEBULA_PRODUCTION", "False") == "True":
            raise RuntimeError(
                "GF_SECURITY_ADMIN_PASSWORD (o NEBULA_DEFAULT_PASSWORD) debe "
                "definirse cuando NEBULA_PRODUCTION=True"
            )
        pwd = secrets.token_urlsafe(16)
        logger.warning("GF_SECURITY_ADMIN_PASSWORD generado para desarrollo (solo log una vez).")
    return pwd
