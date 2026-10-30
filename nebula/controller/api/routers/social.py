"""Login social con Google (OAuth 2.0 + OpenID Connect) usando Authlib."""
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from nebula.controller.api import settings
from nebula.controller.api.database import SessionLocal
from nebula.controller.api.models.user import User
from nebula.controller.api.routers.auth import issue_tokens
from nebula.controller.api.schemas.auth import TokenResponse

router = APIRouter(prefix="/auth", tags=["Social authentication"])

oauth = OAuth()
if settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET:
    oauth.register(
        name="google",
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_id=settings.GOOGLE_CLIENT_ID,
        client_secret=settings.GOOGLE_CLIENT_SECRET,
        client_kwargs={"scope": "openid email profile"},
    )


def _require_google():
    if not hasattr(oauth, "google"):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Google login is not configured (set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)",
        )
    return oauth.google


def find_or_create_social_user(db: Session, provider: str, provider_user_id: str, email: str, full_name: str) -> User:
    user = db.scalar(
        select(User).where(User.auth_provider == provider, User.provider_user_id == provider_user_id)
    )
    if user:
        return user

    # El email ya existe con otro método de acceso: no se enlaza automáticamente (evita toma de cuentas).
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This email is already registered with another sign-in method",
        )

    user = User(
        username=email,
        email=email,
        full_name=full_name or email,
        password_hash=None,  # nunca se recibe ni se guarda la clave de Google
        auth_provider=provider,
        provider_user_id=provider_user_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.get(
    "/google/login",
    summary="Start Google login",
    description="Redirects the browser to Google. **Open it in a browser tab**, not from Swagger's *Try it out*.",
    responses={302: {"description": "Redirect to Google"}, 503: {"description": "Google login not configured"}},
)
async def google_login(request: Request):
    google = _require_google()
    redirect_uri = settings.GOOGLE_REDIRECT_URI or str(request.url_for("google_callback"))
    return await google.authorize_redirect(request, redirect_uri)


@router.get(
    "/google/callback",
    response_model=TokenResponse,
    name="google_callback",
    summary="Google login callback",
    description="Google redirects here. Verifies the response, finds or creates the local user and returns the application JWTs.",
    responses={401: {"description": "Google authentication failed"}, 409: {"description": "Email used by another sign-in method"}},
)
async def google_callback(request: Request):
    google = _require_google()
    try:
        token = await google.authorize_access_token(request)
    except OAuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Google authentication failed: {exc.error}")

    info = token.get("userinfo") or {}
    if not info.get("sub") or not info.get("email") or not info.get("email_verified"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Google did not return a verified email")

    def _work() -> TokenResponse:
        with SessionLocal() as db:
            user = find_or_create_social_user(
                db, "google", info["sub"], info["email"].lower(), info.get("name", "")
            )
            if not user.is_active:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is disabled")
            return issue_tokens(db, user, remember_me=False)

    return await run_in_threadpool(_work)
