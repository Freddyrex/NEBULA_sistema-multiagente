import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

from nebula.controller.api import settings


class TokenError(Exception):
    """Token ausente, mal firmado, expirado o de tipo incorrecto."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _encode(subject: int, token_type: str, lifetime: timedelta, jti: str | None = None) -> tuple[str, datetime, str]:
    now = _now()
    expires = now + lifetime
    jti = jti or str(uuid.uuid4())
    payload = {
        "sub": str(subject),
        "type": token_type,  # "access" | "refresh": evita usar uno en lugar del otro
        "jti": jti,
        "iat": now,
        "exp": expires,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM), expires, jti


def create_access_token(user_id: int) -> tuple[str, int]:
    """Devuelve (token, expires_in_segundos)."""
    lifetime = timedelta(minutes=settings.ACCESS_TOKEN_MINUTES)
    token, _, _ = _encode(user_id, "access", lifetime)
    return token, int(lifetime.total_seconds())


def create_refresh_token(user_id: int, remember_me: bool) -> tuple[str, str, datetime]:
    """Devuelve (token, jti, expires_at). `remember_me` solo cambia la duración del refresh."""
    days = settings.REMEMBER_REFRESH_TOKEN_DAYS if remember_me else settings.REFRESH_TOKEN_DAYS
    token, expires, jti = _encode(user_id, "refresh", timedelta(days=days))
    return token, jti, expires.astimezone(timezone.utc).replace(tzinfo=None)


def decode_token(token: str, expected_type: str) -> dict:
    """Valida firma, expiración y tipo. Lanza TokenError si algo falla."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as exc:  # incluye ExpiredSignatureError
        raise TokenError(str(exc)) from exc
    if payload.get("type") != expected_type:
        raise TokenError(f"Se esperaba un token de tipo '{expected_type}'")
    if not payload.get("sub"):
        raise TokenError("Token sin sujeto")
    return payload


def hash_token(token: str) -> str:
    """SHA-256 del refresh token: es lo único que se guarda en la base de datos."""
    return hashlib.sha256(token.encode()).hexdigest()
