"""Security hardening tests for the FastAPI frontend authentication surface.

Covers:
- ``/platform/firebase_auth`` trusts only a server-verified Firebase ID token.
- ``/platform/register`` refuses usernames that can carry markup (stored XSS).
- Dashboard/API routes that used to answer anonymously now need a session (and a role).
- A scenario name can never be used to read outside the configuration directory.

The frontend module resolves ``templates/`` and ``static/`` relative to the
frontend directory, so it is imported with that directory as the working
directory; the environment is prepared before the import to keep the real
``.env``, the real users database and the real log file untouched.
"""
import asyncio
import logging
import os
import pathlib
import sqlite3
import sys
import tempfile

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FRONTEND_DIR = REPO_ROOT / "nebula" / "frontend"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_TMP_DIR = tempfile.mkdtemp(prefix="nebula-frontend-auth-")
_ENV_KEYS = ("SECRET_KEY", "NEBULA_FRONTEND_LOG", "NEBULA_ENV_PATH")
_PREVIOUS_ENV = {key: os.environ.get(key) for key in _ENV_KEYS}

os.environ.update({"SECRET_KEY": "frontend-auth-security-tests"})
os.environ["NEBULA_FRONTEND_LOG"] = os.path.join(_TMP_DIR, "frontend.log")
os.environ["NEBULA_ENV_PATH"] = os.path.join(_TMP_DIR, "frontend.env")
os.environ.setdefault("NEBULA_CONTROLLER_HOST", "127.0.0.1")

_PREVIOUS_CWD = os.getcwd()
os.chdir(FRONTEND_DIR)
try:
    from nebula.frontend import app as frontend_app
finally:
    os.chdir(_PREVIOUS_CWD)
    for key, value in _PREVIOUS_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture()
def users_db(tmp_path, monkeypatch):
    """Point the users database at a throwaway file seeded with one administrator."""
    from nebula.controller import database

    db_path = tmp_path / "users.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE users (user TEXT PRIMARY KEY, password TEXT, role TEXT, created_at TEXT)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(database, "user_db_file_location", str(db_path))
    database.add_user("seed-admin", "seed-password", "admin")
    return database


@pytest.fixture()
def frontend_users(monkeypatch, users_db):
    """Route the frontend user helpers to the throwaway users database."""
    database = users_db

    async def fake_list_users(allinfo=True):
        if allinfo:
            return [dict(row) for row in database.list_users(all_info=True)]
        return database.list_users(all_info=False)

    async def fake_get_users_roles():
        return {
            "users": database.list_user_roles(),
            "platform_admin": "SEED-ADMIN",
            "first_registered": "SEED-ADMIN",
        }

    async def fake_add_user(user, password, role):
        database.add_user(user, password, role)

    monkeypatch.setattr(frontend_app, "list_users", fake_list_users)
    monkeypatch.setattr(frontend_app, "get_users_roles", fake_get_users_roles)
    monkeypatch.setattr(frontend_app, "add_user", fake_add_user)
    return database


def _new_client():
    """Build a HTTP client that asks for JSON and never follows redirects."""
    return TestClient(
        frontend_app.app,
        headers={"accept": "application/json"},
        follow_redirects=False,
    )


@pytest.fixture()
def client():
    """HTTP client that asks for JSON and never follows redirects."""
    return _new_client()


def _fake_verify_user(user, role):
    """Build a controller verification stub that always accepts the given identity."""

    async def verify_user(username, password):
        return {"user": user, "role": role}

    return verify_user


def _login(client, monkeypatch, role):
    """Open an authenticated session with the given role."""
    monkeypatch.setattr(frontend_app, "verify_user", _fake_verify_user("SEED-ADMIN", role))
    response = client.post("/platform/login", data={"user": "SEED-ADMIN", "password": "seed-password"})
    assert response.status_code == 200
    return client


@pytest.fixture()
def logged_in(client, monkeypatch):
    """Open an authenticated administrator session on the client."""
    return _login(client, monkeypatch, "admin")


def test_should_return_401_when_the_authorization_header_is_missing(client):
    """Without a bearer token there is no identity to trust, so the login is refused."""
    response = client.post(
        "/platform/firebase_auth",
        data={"user": "ana", "email": "ana@example.com"},
    )

    assert response.status_code == 401
    assert "Login successful" not in response.text


def test_should_return_401_when_the_token_verification_fails(client, monkeypatch):
    """A token that the server cannot verify never opens a session."""
    monkeypatch.setattr(frontend_app, "_verify_firebase_id_token", lambda token: None)

    response = client.post(
        "/platform/firebase_auth",
        headers={"Authorization": "Bearer forged-token"},
        data={"user": "ana", "email": "ana@example.com"},
    )

    assert response.status_code == 401
    assert "Login successful" not in response.text


def test_should_return_401_when_the_authorization_header_is_malformed(client, monkeypatch):
    """Only a well formed ``Bearer <token>`` header is accepted."""
    monkeypatch.setattr(
        frontend_app,
        "_verify_firebase_id_token",
        lambda token: {"email": "ana@example.com", "localId": "firebase-uid-42"},
    )

    for header in ("Basic YW5hOnNlY3JldA==", "Bearer", "Bearer one two", "some-token"):
        response = client.post("/platform/firebase_auth", headers={"Authorization": header})
        assert response.status_code == 401
        assert "Login successful" not in response.text


def test_should_verify_the_token_against_the_identity_toolkit(monkeypatch, caplog):
    """The token is checked server-side and it never reaches the log."""
    calls = []

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload

        def json(self):
            return self.payload

    class FakeRequests:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload

        def post(self, url, json=None, timeout=None):
            calls.append({"url": url, "body": json, "timeout": timeout})
            return FakeResponse(self.status_code, self.payload)

    monkeypatch.setattr(frontend_app._frontend_defaults, "FIREBASE_API_KEY", "test-api-key")
    monkeypatch.setattr(
        frontend_app,
        "requests",
        FakeRequests(200, {"users": [{"email": "ana@example.com", "localId": "firebase-uid-42"}]}),
    )

    with caplog.at_level(logging.INFO):
        claims = frontend_app._verify_firebase_id_token("SECRET-TOKEN")

    assert claims == {"email": "ana@example.com", "localId": "firebase-uid-42"}
    assert calls[0]["body"] == {"idToken": "SECRET-TOKEN"}
    assert calls[0]["timeout"] == 10
    assert calls[0]["url"].startswith("https://identitytoolkit.googleapis.com/v1/accounts:lookup?key=")
    assert "SECRET-TOKEN" not in caplog.text


def test_should_reject_the_token_when_the_identity_toolkit_does_not_confirm_it(monkeypatch):
    """A non-200 answer or an empty user list means the token is not valid."""

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload

        def json(self):
            return self.payload

    class FakeRequests:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload

        def post(self, url, json=None, timeout=None):
            return FakeResponse(self.status_code, self.payload)

    monkeypatch.setattr(frontend_app._frontend_defaults, "FIREBASE_API_KEY", "test-api-key")

    monkeypatch.setattr(frontend_app, "requests", FakeRequests(400, {"error": "INVALID_ID_TOKEN"}))
    assert frontend_app._verify_firebase_id_token("SECRET-TOKEN") is None

    monkeypatch.setattr(frontend_app, "requests", FakeRequests(200, {"users": []}))
    assert frontend_app._verify_firebase_id_token("SECRET-TOKEN") is None


def test_should_fail_closed_when_no_firebase_api_key_is_configured(client, monkeypatch):
    """Without an API key the token cannot be verified, so the login is refused (503)."""
    monkeypatch.setattr(frontend_app._frontend_defaults, "FIREBASE_API_KEY", "")

    response = client.post(
        "/platform/firebase_auth",
        headers={"Authorization": "Bearer some-token"},
    )

    assert response.status_code == 503
    assert "Login successful" not in response.text


def test_should_create_the_account_and_open_the_session_when_the_token_is_verified(
    client, monkeypatch, frontend_users
):
    """A verified token creates the account, ignores the client hints and never stores a literal password."""
    database = frontend_users
    monkeypatch.setattr(
        frontend_app,
        "_verify_firebase_id_token",
        lambda token: {"email": "ana@example.com", "localId": "firebase-uid-42"},
    )

    async def fake_get_scenarios(user, role):
        return {"user": user, "role": role}

    monkeypatch.setattr(frontend_app, "get_scenarios", fake_get_scenarios)

    response = client.post(
        "/platform/firebase_auth",
        headers={"Authorization": "Bearer good-token"},
        data={"user": "attacker", "email": "attacker@example.com"},
    )

    assert response.status_code == 200
    assert response.json() == {"message": "Login successful"}

    stored = database.get_user_info("ANA_EXAMPLE.COM")
    assert stored is not None
    assert stored["role"] == "user"
    legacy_literal = "firebase_auth"
    assert stored["password"] != legacy_literal
    assert database.verify("ANA_EXAMPLE.COM", legacy_literal) is False

    session_view = client.get("/platform/api/scenarios")
    assert session_view.status_code == 200
    assert session_view.json() == {"user": "ANA_EXAMPLE.COM", "role": "user"}


def test_should_fall_back_to_the_uid_when_the_email_is_not_usable(client, monkeypatch, frontend_users):
    """The verified uid names the account when the verified email cannot be used."""
    database = frontend_users
    monkeypatch.setattr(
        frontend_app,
        "_verify_firebase_id_token",
        lambda token: {"email": "@", "localId": "firebase-uid-42"},
    )

    response = client.post("/platform/firebase_auth", headers={"Authorization": "Bearer good-token"})

    assert response.status_code == 200
    assert database.get_user_info("FIREBASE-UID-42") is not None

    monkeypatch.setattr(
        frontend_app,
        "_verify_firebase_id_token",
        lambda token: {"email": "", "localId": ""},
    )

    unusable = client.post("/platform/firebase_auth", headers={"Authorization": "Bearer good-token"})
    assert unusable.status_code == 401


def test_should_reject_the_registration_when_the_username_contains_markup(client, frontend_users):
    """A username with HTML/JS characters is refused and no account is created."""
    database = frontend_users

    for username in ("<img src=x onerror=alert(1)>", "<script>alert(1)</script>", "a\"b", "a'b"):
        response = client.post(
            "/platform/register",
            data={
                "user": username,
                "password": "seed-password",
                "confirm_password": "seed-password",
            },
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/platform/register"

    assert [row["user"] for row in database.list_user_roles()] == ["SEED-ADMIN"]


def test_should_reject_anonymous_clients_when_the_endpoint_requires_a_session(client):
    """The routes that used to answer anonymously now refuse a client without a session."""
    assert client.get("/platform/api/physical/state/1.2.3.4").status_code in (401, 403)
    assert client.get("/platform/dashboard/x/notes").status_code in (401, 403)
    assert client.get("/platform/dashboard/x/config").status_code in (401, 403)
    assert client.post("/platform/dashboard/x/node/update", json={}).status_code in (401, 403)
    assert client.post("/platform/dashboard/x/node/done", json={}).status_code in (401, 403)
    assert client.get("/experiment/foo").status_code in (401, 403)


def test_should_return_400_when_the_scenario_name_traverses_out_of_the_config_dir(
    logged_in, monkeypatch, tmp_path
):
    """A traversal scenario name is refused and no file outside the config dir is read."""
    config_dir = tmp_path / "config"
    (config_dir / "x").mkdir(parents=True)
    (config_dir / "x" / "scenario.json").write_text('{"inside": "config"}')
    (tmp_path / "scenario.json").write_text('{"inside": "TOP-SECRET"}')
    monkeypatch.setenv("NEBULA_CONFIG_DIR", str(config_dir))

    async def fake_check_scenario_with_role(role, scenario_name):
        return True

    monkeypatch.setattr(frontend_app, "check_scenario_with_role", fake_check_scenario_with_role)

    response = logged_in.get("/platform/dashboard/%2E%2E/config")

    assert response.status_code == 400
    assert "TOP-SECRET" not in response.text

    with pytest.raises(HTTPException) as excinfo:
        frontend_app._validate_scenario_name("../../etc")
    assert excinfo.value.status_code == 400


def test_should_return_400_when_the_scenario_name_is_not_safe_for_a_path(
    logged_in, monkeypatch, tmp_path
):
    """A name the role check accepts is still refused when it is not safe to use in a path."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("NEBULA_CONFIG_DIR", str(config_dir))

    async def fake_check_scenario_with_role(role, scenario_name):
        return True

    monkeypatch.setattr(frontend_app, "check_scenario_with_role", fake_check_scenario_with_role)

    response = logged_in.get("/platform/dashboard/a.b/config")

    assert response.status_code == 400


def test_should_return_403_when_the_role_cannot_access_the_scenario(
    logged_in, monkeypatch, tmp_path
):
    """A session that is not allowed on the scenario does not get its configuration."""
    config_dir = tmp_path / "config"
    (config_dir / "x").mkdir(parents=True)
    (config_dir / "x" / "scenario.json").write_text('{"inside": "config"}')
    monkeypatch.setenv("NEBULA_CONFIG_DIR", str(config_dir))

    async def fake_check_scenario_with_role(role, scenario_name):
        return False

    monkeypatch.setattr(frontend_app, "check_scenario_with_role", fake_check_scenario_with_role)

    response = logged_in.get("/platform/dashboard/x/config")

    assert response.status_code == 403


def test_should_return_403_when_the_role_is_not_allowed_to_use_the_endpoint(client, monkeypatch):
    """A viewer session is refused by the endpoints reserved to administrators and operators."""
    viewer = _login(client, monkeypatch, "viewer")

    assert viewer.get("/platform/api/physical/state/192.168.1.10").status_code == 403
    assert viewer.post("/platform/dashboard/x/node/update", json={}).status_code == 403
    assert viewer.post("/platform/dashboard/x/node/done", json={}).status_code == 403


def test_should_return_400_when_the_ip_address_is_not_private(logged_in, monkeypatch):
    """Only private addresses are proxied to the controller."""
    requested = []

    async def fake_controller_get(url):
        requested.append(url)
        return {"running": False}

    monkeypatch.setattr(frontend_app, "controller_get", fake_controller_get)

    public = logged_in.get("/platform/api/physical/state/8.8.8.8")
    assert public.status_code == 400
    assert requested == []

    invalid = logged_in.get("/platform/api/physical/state/not-an-ip")
    assert invalid.status_code == 400
    assert requested == []

    private = logged_in.get("/platform/api/physical/state/192.168.1.10")
    assert private.status_code == 200
    assert private.json() == {"running": False}
    assert len(requested) == 1


def test_should_return_the_scenario_config_when_the_session_and_the_name_are_valid(
    logged_in, monkeypatch, tmp_path
):
    """A safe scenario name still serves its configuration to an allowed session."""
    config_dir = tmp_path / "config"
    (config_dir / "x").mkdir(parents=True)
    (config_dir / "x" / "scenario.json").write_text('{"inside": "config"}')
    monkeypatch.setenv("NEBULA_CONFIG_DIR", str(config_dir))

    async def fake_check_scenario_with_role(role, scenario_name):
        return True

    monkeypatch.setattr(frontend_app, "check_scenario_with_role", fake_check_scenario_with_role)

    response = logged_in.get("/platform/dashboard/x/config")

    assert response.status_code == 200
    assert response.json()["config"] == {"inside": "config"}


def test_should_require_scenario_access_when_the_logs_are_downloaded(
    client, logged_in, monkeypatch, tmp_path
):
    """The log archive is only built for a session that is allowed on the scenario."""
    log_dir = tmp_path / "logs"
    config_dir = tmp_path / "config"
    (log_dir / "x").mkdir(parents=True)
    (config_dir / "x").mkdir(parents=True)
    (log_dir / "x" / "run.log").write_text("data")
    (config_dir / "x" / "scenario.json").write_text("{}")
    monkeypatch.setattr(frontend_app.settings, "log_dir", str(log_dir))
    monkeypatch.setattr(frontend_app.settings, "config_dir", str(config_dir))

    async def fake_check_scenario_with_role(role, scenario_name):
        return False

    monkeypatch.setattr(frontend_app, "check_scenario_with_role", fake_check_scenario_with_role)

    anonymous = _new_client().get("/platform/dashboard/x/download/logs")
    assert anonymous.status_code == 401

    refused = logged_in.get("/platform/dashboard/x/download/logs")
    assert refused.status_code == 403

    async def allowed_check_scenario_with_role(role, scenario_name):
        return True

    monkeypatch.setattr(
        frontend_app, "check_scenario_with_role", allowed_check_scenario_with_role
    )

    allowed = logged_in.get("/platform/dashboard/x/download/logs")
    assert allowed.status_code == 200
    assert allowed.headers["content-type"] == "application/zip"


def test_should_require_an_administrator_or_operator_when_the_deployment_dashboard_is_opened(
    client, monkeypatch
):
    """The deployment dashboard is closed to anonymous clients and to viewers."""
    anonymous = client.get("/platform/dashboard/deployment/")
    assert anonymous.status_code == 302

    viewer = _login(client, monkeypatch, "viewer")
    refused = viewer.get("/platform/dashboard/deployment/")
    assert refused.status_code == 403


def test_should_report_no_running_scenario_when_the_client_is_anonymous(client):
    """The running-scenario report answers an anonymous client instead of failing."""
    response = client.get("/platform/api/dashboard/runningscenario")

    assert response.status_code == 200
    assert response.json() == {"scenario_status": "not running"}


def test_should_not_log_the_controller_authentication_response(caplog, monkeypatch):
    """The controller answer is never written to the log: it can carry token material."""

    async def fake_controller_post(url, data=None):
        return {
            "user": "ANA",
            "role": "admin",
            "token": "SECRET-TOKEN",
            "password": "SECRET-PASSWORD",
        }

    monkeypatch.setattr(frontend_app, "controller_post", fake_controller_post)

    with caplog.at_level(logging.INFO):
        result = asyncio.run(frontend_app.verify_user("ANA", "SECRET-PASSWORD"))

    assert result["user"] == "ANA"
    assert "SECRET-TOKEN" not in caplog.text
    assert "SECRET-PASSWORD" not in caplog.text


def test_should_not_log_the_controller_body_when_the_answer_is_unexpected(caplog, monkeypatch):
    """An unexpected controller body is not written to the log either."""

    async def fake_controller_post(url, data=None):
        return "UNEXPECTED-BODY-WITH-SECRET"

    monkeypatch.setattr(frontend_app, "controller_post", fake_controller_post)

    with caplog.at_level(logging.INFO):
        result = asyncio.run(frontend_app.verify_user("ANA", "SECRET-PASSWORD"))

    assert result is False
    assert "UNEXPECTED-BODY-WITH-SECRET" not in caplog.text


def test_should_harden_the_session_cookie(client, monkeypatch):
    """The session cookie is HttpOnly, SameSite=Lax and Secure in production."""
    monkeypatch.setattr(
        frontend_app,
        "verify_user",
        _fake_verify_user("SEED-ADMIN", "admin"),
    )
    response = client.post("/platform/login", data={"user": "SEED-ADMIN", "password": "seed-password"})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie

    middleware = next(item for item in frontend_app.app.user_middleware if item.cls is SessionMiddleware)
    assert middleware.kwargs["same_site"] == "lax"
    assert middleware.kwargs["https_only"] is frontend_app.settings.production


def test_should_bind_to_the_loopback_address_by_default():
    """The frontend does not listen on every interface unless it is told to."""
    source = (REPO_ROOT / "nebula" / "frontend" / "app.py").read_text(encoding="utf-8")

    assert 'host="0.0.0.0"' not in source
    assert 'os.environ.get("NEBULA_BIND_HOST", "127.0.0.1")' in source
