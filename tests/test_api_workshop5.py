"""Pruebas del Workshop 5 (matriz de la sección 26) con pytest + TestClient."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

# La configuración se fija ANTES de importar la API.
_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test_api.db"
os.environ["JWT_SECRET"] = "test-secret-" + "x" * 40
os.environ.pop("GOOGLE_CLIENT_ID", None)

from fastapi.testclient import TestClient  # noqa: E402
from jose import jwt  # noqa: E402

from nebula.controller.api import settings  # noqa: E402
from nebula.controller.api.database import SessionLocal  # noqa: E402
from nebula.controller.api.routers.social import find_or_create_social_user  # noqa: E402
from nebula.controller.api.standalone import app  # noqa: E402

USER = {"username": "student01", "full_name": "Ana Torres", "email": "ana@example.com", "password": "MySecurePassword123!"}
OTHER = {"username": "student02", "full_name": "Luis Pérez", "email": "luis@example.com", "password": "AnotherPassword456!"}
EXPERIMENT = {"name": "MNIST ring", "dataset": "MNIST", "model": "MLP", "topology": "Ring", "n_nodes": 4}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:  # el lifespan crea las tablas
        yield c


def register_and_login(client, user, remember_me=False):
    client.post("/auth/register", json=user)
    r = client.post("/auth/login", json={"email": user["email"], "password": user["password"], "remember_me": remember_me})
    assert r.status_code == 200, r.text
    return r.json()


def auth(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def lifetime_days(token):
    claims = jwt.get_unverified_claims(token)
    return round((claims["exp"] - claims["iat"]) / 86400)


def test_public_endpoint_without_jwt(client):
    r = client.get("/public/info")
    assert r.status_code == 200 and r.json()["authentication_required"] is False
    assert client.get("/public/datasets").status_code == 200


def test_registration_valid_and_hash_not_exposed(client):
    r = client.post("/auth/register", json=USER)
    assert r.status_code == 201
    assert "password" not in r.text and "hash" not in r.text


def test_duplicate_email_and_username_rejected(client):
    assert client.post("/auth/register", json=USER).status_code == 409
    assert client.post("/auth/register", json={**USER, "email": "otra@example.com"}).status_code == 409


def test_invalid_email_rejected(client):
    assert client.post("/auth/register", json={**USER, "email": "not-an-email"}).status_code == 422


def test_login_correct_and_incorrect(client):
    r = client.post("/auth/login", json={"email": USER["email"], "password": USER["password"]})
    body = r.json()
    assert r.status_code == 200 and body["token_type"] == "bearer" and body["expires_in"] == 900
    bad = client.post("/auth/login", json={"email": USER["email"], "password": "wrong-password"})
    assert bad.status_code == 401
    unknown = client.post("/auth/login", json={"email": "nadie@example.com", "password": "whatever123"})
    assert unknown.status_code == 401 and unknown.json() == bad.json()  # no revela si la cuenta existe


def test_users_me(client):
    assert client.get("/users/me").status_code == 401
    tokens = register_and_login(client, USER)
    r = client.get("/users/me", headers=auth(tokens))
    assert r.status_code == 200 and r.json()["email"] == USER["email"] and "password_hash" not in r.json()


def test_invalid_and_expired_tokens_rejected(client):
    assert client.get("/users/me", headers={"Authorization": "Bearer garbage"}).status_code == 401
    expired = jwt.encode(
        {"sub": "1", "type": "access", "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
        settings.JWT_SECRET,
        algorithm="HS256",
    )
    assert client.get("/users/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    forged = jwt.encode({"sub": "1", "type": "access"}, "otra-clave", algorithm="HS256")
    assert client.get("/users/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_refresh_token_cannot_be_used_as_access_and_vice_versa(client):
    tokens = register_and_login(client, USER)
    assert client.get("/users/me", headers={"Authorization": f"Bearer {tokens['refresh_token']}"}).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": tokens["access_token"]}).status_code == 401


def test_crud_requires_jwt_and_works_with_jwt(client):
    assert client.get("/experiments").status_code == 401
    assert client.post("/experiments", json=EXPERIMENT).status_code == 401
    h = auth(register_and_login(client, USER))

    created = client.post("/experiments", json={**EXPERIMENT, "user_id": 999}, headers=h)
    assert created.status_code == 201
    exp = created.json()
    assert exp["user_id"] != 999  # el dueño sale del JWT, no del cuerpo

    assert client.get("/experiments", headers=h).status_code == 200
    assert client.get(f"/experiments/{exp['id']}", headers=h).json()["name"] == "MNIST ring"

    put = client.put(f"/experiments/{exp['id']}", json={**EXPERIMENT, "name": "Renombrado"}, headers=h)
    assert put.status_code == 200 and put.json()["name"] == "Renombrado"

    patch = client.patch(f"/experiments/{exp['id']}", json={"status": "queued"}, headers=h)
    assert patch.status_code == 200 and patch.json()["status"] == "queued" and patch.json()["name"] == "Renombrado"
    assert client.patch(f"/experiments/{exp['id']}", json={"dataset": "CIFAR100"}, headers=h).status_code == 422

    assert client.delete(f"/experiments/{exp['id']}", headers=h).status_code == 204
    assert client.get(f"/experiments/{exp['id']}", headers=h).status_code == 404


def test_validation_errors(client):
    h = auth(register_and_login(client, USER))
    assert client.post("/experiments", json={**EXPERIMENT, "model": "ResNet"}, headers=h).status_code == 422
    assert client.post("/experiments", json={**EXPERIMENT, "n_nodes": 1}, headers=h).status_code == 422
    assert client.post("/experiments", json={**EXPERIMENT, "dataset": "Nope"}, headers=h).status_code == 422


def test_cannot_access_another_users_resource(client):
    h1 = auth(register_and_login(client, USER))
    h2 = auth(register_and_login(client, OTHER))
    exp_id = client.post("/experiments", json=EXPERIMENT, headers=h1).json()["id"]
    assert client.get(f"/experiments/{exp_id}", headers=h2).status_code == 403
    assert client.put(f"/experiments/{exp_id}", json=EXPERIMENT, headers=h2).status_code == 403
    assert client.patch(f"/experiments/{exp_id}", json={"status": "running"}, headers=h2).status_code == 403
    assert client.delete(f"/experiments/{exp_id}", headers=h2).status_code == 403
    assert all(e["id"] != exp_id for e in client.get("/experiments", headers=h2).json())


def test_refresh_valid_rotates_and_old_token_is_rejected(client):
    tokens = register_and_login(client, USER)
    r = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200
    new = r.json()
    assert new["refresh_token"] != tokens["refresh_token"]
    assert client.get("/users/me", headers=auth(new)).status_code == 200
    assert client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401


def test_reuse_of_rotated_token_revokes_all_sessions(client):
    tokens = register_and_login(client, USER)
    new = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).json()
    client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})  # reutilización
    assert client.post("/auth/refresh", json={"refresh_token": new["refresh_token"]}).status_code == 401


def test_invalid_refresh_rejected(client):
    assert client.post("/auth/refresh", json={"refresh_token": "nope"}).status_code == 401
    forged = jwt.encode({"sub": "1", "type": "refresh", "exp": datetime.now(timezone.utc) + timedelta(days=1)}, settings.JWT_SECRET, algorithm="HS256")
    assert client.post("/auth/refresh", json={"refresh_token": forged}).status_code == 401


def test_logout_revokes_refresh_token(client):
    tokens = register_and_login(client, USER)
    assert client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]}).status_code == 204
    assert client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401
    assert client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]}).status_code == 204


def test_logout_all(client):
    t1 = register_and_login(client, USER)
    t2 = register_and_login(client, USER)
    assert client.post("/auth/logout-all", headers=auth(t1)).status_code == 204
    assert client.post("/auth/refresh", json={"refresh_token": t1["refresh_token"]}).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": t2["refresh_token"]}).status_code == 401
    assert client.post("/auth/logout-all").status_code == 401


def test_remember_me_changes_only_refresh_lifetime(client):
    normal = register_and_login(client, USER, remember_me=False)
    remembered = register_and_login(client, USER, remember_me=True)
    assert lifetime_days(normal["refresh_token"]) == settings.REFRESH_TOKEN_DAYS == 7
    assert lifetime_days(remembered["refresh_token"]) == settings.REMEMBER_REFRESH_TOKEN_DAYS == 30
    assert jwt.get_unverified_claims(remembered["access_token"])["exp"] - jwt.get_unverified_claims(remembered["access_token"])["iat"] == 900
    rotated = client.post("/auth/refresh", json={"refresh_token": remembered["refresh_token"]}).json()
    assert lifetime_days(rotated["refresh_token"]) == 30


def test_inactive_user_cannot_use_tokens(client):
    from sqlalchemy import select
    from nebula.controller.api.models.user import User

    tokens = register_and_login(client, OTHER)
    with SessionLocal() as db:
        db.scalar(select(User).where(User.email == OTHER["email"])).is_active = False
        db.commit()
    assert client.get("/users/me", headers=auth(tokens)).status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401
    assert client.post("/auth/login", json={"email": OTHER["email"], "password": OTHER["password"]}).status_code == 403


def test_social_login_not_configured_returns_503(client):
    assert client.get("/auth/google/login", follow_redirects=False).status_code == 503


def test_social_user_creation_and_linking_rules():
    with SessionLocal() as db:
        u1 = find_or_create_social_user(db, "google", "sub-123", "social@gmail.com", "Social User")
        assert u1.password_hash is None and u1.auth_provider == "google"
        u2 = find_or_create_social_user(db, "google", "sub-123", "social@gmail.com", "Social User")
        assert u1.id == u2.id
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            find_or_create_social_user(db, "google", "sub-999", USER["email"], "Impostor")
        assert exc.value.status_code == 409


def test_social_only_account_cannot_login_with_password(client):
    r = client.post("/auth/login", json={"email": "social@gmail.com", "password": "anything123"})
    assert r.status_code == 401


def test_openapi_exposes_bearer_scheme_and_tags(client):
    spec = client.get("/openapi.json").json()
    assert "HTTPBearer" in spec["components"]["securitySchemes"]
    assert {t["name"] for t in spec["tags"]} >= {"Public", "Authentication", "Users", "Experiments"}
    assert "/experiments/{experiment_id}" in spec["paths"]
