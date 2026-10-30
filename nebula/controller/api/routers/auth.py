from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nebula.controller.api.database import get_db
from nebula.controller.api.models.refresh_token import RefreshToken
from nebula.controller.api.models.user import User, utcnow
from nebula.controller.api.schemas.auth import LoginRequest, LogoutRequest, RefreshRequest, TokenResponse
from nebula.controller.api.schemas.user import UserRegister, UserResponse
from nebula.controller.api.security.deps import get_current_user
from nebula.controller.api.security.jwt import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_token,
)
from nebula.controller.api.security.passwords import hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["Authentication"])


def issue_tokens(db: Session, user: User, remember_me: bool) -> TokenResponse:
    """Crea un access token y un refresh token (este último queda registrado en la BD)."""
    access_token, expires_in = create_access_token(user.id)
    refresh_token, jti, expires_at = create_refresh_token(user.id, remember_me)
    db.add(
        RefreshToken(
            user_id=user.id,
            jti=jti,
            token_hash=hash_token(refresh_token),
            remember_me=remember_me,
            expires_at=expires_at,
        )
    )
    user.last_login = utcnow()
    db.commit()
    return TokenResponse(access_token=access_token, refresh_token=refresh_token, expires_in=expires_in)


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user",
    description="Creates a local account. The password is stored only as an Argon2id hash.",
    responses={409: {"description": "Username or email already registered"}},
)
def register(payload: UserRegister, db: Session = Depends(get_db)):
    email = payload.email.lower()
    exists = db.scalar(select(User.id).where(or_(User.email == email, User.username == payload.username)))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "Username or email already registered")

    user = User(
        username=payload.username,
        full_name=payload.full_name,
        email=email,
        password_hash=hash_password(payload.password),
        auth_provider="local",
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:  # carrera entre dos registros simultáneos
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Username or email already registered")
    db.refresh(user)
    return user


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Log in with email and password",
    description=(
        "Returns an access token (short-lived) and a refresh token. "
        "With `remember_me=true` the refresh token lasts longer; the access token lifetime does not change."
    ),
    responses={401: {"description": "Invalid credentials"}, 403: {"description": "Inactive account"}},
)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    # Mismo mensaje y mismo coste de cómputo si el usuario no existe, es social o la clave falla.
    password_ok = verify_password(payload.password, user.password_hash if user else None)
    if user is None or not password_ok:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is disabled")
    return issue_tokens(db, user, payload.remember_me)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Obtain a new access token",
    description=(
        "Validates the refresh token (signature, expiry, type, not revoked, active user) and applies "
        "**refresh-token rotation**: the used token is revoked and a new pair is issued."
    ),
    responses={401: {"description": "Invalid, expired or revoked refresh token"}},
)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)):
    invalid = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Invalid refresh token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        claims = decode_token(payload.refresh_token, expected_type="refresh")
    except TokenError:
        raise invalid

    record = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(payload.refresh_token)))
    if record is None:
        raise invalid

    if record.revoked_at is not None:
        # Reutilización de un token ya rotado/cerrado: probable robo. Se cierran todas las sesiones.
        db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == record.user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
        db.commit()
        raise invalid

    user = db.get(User, int(claims["sub"]))
    if record.expires_at <= utcnow() or user is None or not user.is_active:
        raise invalid

    record.revoked_at = utcnow()  # rotación: este refresh token ya no sirve
    return issue_tokens(db, user, record.remember_me)  # conserva la duración elegida en el login


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Log out (revoke the refresh token)",
    description="Revokes the given refresh token/session. Idempotent: always answers 204.",
)
def logout(payload: LogoutRequest, db: Session = Depends(get_db)):
    record = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(payload.refresh_token)))
    if record is not None and record.revoked_at is None:
        record.revoked_at = utcnow()
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Log out from every device",
    description="Revokes all active refresh tokens of the authenticated user.",
    responses={401: {"description": "Missing or invalid access token"}},
)
def logout_all(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == current_user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
