from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from nebula.controller.api.database import get_db
from nebula.controller.api.models.user import User
from nebula.controller.api.security.jwt import TokenError, decode_token

# Declarar el esquema hace que Swagger UI muestre el botón "Authorize".
# auto_error=False para responder siempre 401 (HTTPBearer por defecto responde 403).
bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Pega aquí el `access_token` devuelto por /auth/login (sin la palabra Bearer).",
)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Autenticación: ¿quién es este usuario? (la autorización se valida en cada router)."""
    if credentials is None:
        raise _unauthorized("Not authenticated")
    try:
        payload = decode_token(credentials.credentials, expected_type="access")
        user_id = int(payload["sub"])
    except (TokenError, ValueError):
        raise _unauthorized("Invalid or expired token")

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise _unauthorized("User not found or inactive")
    return user
