"""Arreglo del login de admin: 401 limpio y la password no se pierde al reiniciar.

Criterios:
- Credenciales incorrectas devuelven 401 (no 500 con "Error verifying user: 401").
- El arranque crea el admin si no existe, pero no pisa una password ya cambiada.
- Un cambio pedido explícitamente (``NEBULA_ADMIN_PASSWORD_PENDING``) sí se aplica.
- El ``.env`` es la fuente de verdad y se actualiza sin duplicar claves.
"""
import os
import pathlib
import re
import sqlite3
import tempfile

from fastapi.testclient import TestClient

from nebula.controller import database
from nebula.utils import EnvUtils


def _make_users_db(path, user, password, role="admin"):
    """Crea un ``users.db`` mínimo con un usuario."""
    ph = database.PasswordHasher()
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS users (user TEXT PRIMARY KEY, password TEXT, role TEXT)")
    conn.execute("INSERT INTO users VALUES (?, ?, ?)", (user.upper(), ph.hash(password), role))
    conn.commit()
    conn.close()


def _write_env(path, user, password, pending=None):
    """Escribe un ``.env`` con las credenciales configuradas."""
    lines = [f"NEBULA_DEFAULT_USER={user}", f"NEBULA_DEFAULT_PASSWORD={password}", "SECRET_KEY=abc"]
    if pending is not None:
        lines.append(f"NEBULA_ADMIN_PASSWORD_PENDING={pending}")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def test_env_file_is_read_and_rewritten_without_duplicates():
    with tempfile.TemporaryDirectory() as tmp:
        env_path = os.path.join(tmp, ".env")
        with open(env_path, "w") as f:
            f.write("# comentario\nNEBULA_DEFAULT_USER=admin\nNEBULA_DEFAULT_PASSWORD='vieja'\nOTRA=1\n")

        values = EnvUtils.read_env_file(env_path)
        assert values["NEBULA_DEFAULT_USER"] == "admin"
        assert values["NEBULA_DEFAULT_PASSWORD"] == "vieja"  # sin comillas
        assert values["OTRA"] == "1"

        assert EnvUtils.persist_credentials(env_path, "admin", "nueva")
        content = open(env_path).read()
        assert content.count("NEBULA_DEFAULT_PASSWORD=") == 1
        assert "NEBULA_DEFAULT_PASSWORD=nueva" in content
        assert "OTRA=1" in content  # no se pierde lo demás


def test_env_file_extra_keys_are_written():
    with tempfile.TemporaryDirectory() as tmp:
        env_path = os.path.join(tmp, ".env")
        _write_env(env_path, "admin", "clave")
        assert EnvUtils.persist_credentials(env_path, "admin", "clave", extra={"NEBULA_ADMIN_PASSWORD_PENDING": "1"})
        values = EnvUtils.read_env_file(env_path)
        assert values["NEBULA_ADMIN_PASSWORD_PENDING"] == "1"


def test_sync_creates_admin_when_missing():
    with tempfile.TemporaryDirectory() as tmp:
        env_path = os.path.join(tmp, ".env")
        db_path = os.path.join(tmp, "users.db")
        _write_env(env_path, "admin", "clave-inicial")

        database.user_db_file_location = db_path
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS users (user TEXT PRIMARY KEY, password TEXT, role TEXT)")
        conn.commit()
        conn.close()

        os.environ["NEBULA_ENV_PATH"] = env_path
        os.environ.pop("NEBULA_ADMIN_PASSWORD_PENDING", None)
        database._sync_configured_credentials()

        assert database.verify("ADMIN", "clave-inicial") is True


def test_sync_does_not_overwrite_existing_password():
    """La password cambiada en la UI debe sobrevivir a un reinicio."""
    with tempfile.TemporaryDirectory() as tmp:
        env_path = os.path.join(tmp, ".env")
        db_path = os.path.join(tmp, "users.db")
        # El .env quedó con la password antigua (la de la UI aún no se reflejó).
        _write_env(env_path, "admin", "password-vieja")
        _make_users_db(db_path, "ADMIN", "password-nueva-de-la-ui")

        database.user_db_file_location = db_path
        os.environ["NEBULA_ENV_PATH"] = env_path
        os.environ.pop("NEBULA_ADMIN_PASSWORD_PENDING", None)
        database._sync_configured_credentials()

        assert database.verify("ADMIN", "password-nueva-de-la-ui") is True
        assert database.verify("ADMIN", "password-vieja") is False


def test_sync_applies_pending_password_and_clears_the_flag():
    """``NEBULA password`` sin acceso a la BD deja el cambio pendiente para el arranque."""
    with tempfile.TemporaryDirectory() as tmp:
        env_path = os.path.join(tmp, ".env")
        db_path = os.path.join(tmp, "users.db")
        _write_env(env_path, "admin", "password-pendiente", pending="1")
        _make_users_db(db_path, "ADMIN", "password-anterior")

        database.user_db_file_location = db_path
        os.environ["NEBULA_ENV_PATH"] = env_path
        database._sync_configured_credentials()

        assert database.verify("ADMIN", "password-pendiente") is True
        assert EnvUtils.read_env_file(env_path)["NEBULA_ADMIN_PASSWORD_PENDING"] == "0"


def test_verify_endpoint_returns_401_for_wrong_password():
    """Una contraseña incorrecta debe ser 401, no un 500 con 'Error verifying user'."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "users.db")
        _make_users_db(db_path, "ADMIN", "clave-buena")
        database.user_db_file_location = db_path

        from nebula.controller.controller import app

        client = TestClient(app)
        ok = client.post("/user/verify", json={"user": "admin", "password": "clave-buena"})
        assert ok.status_code == 200
        assert ok.json()["user"] == "ADMIN"

        ko = client.post("/user/verify", json={"user": "admin", "password": "clave-mala"})
        assert ko.status_code == 401
        assert "Error verifying user" not in ko.text

        unknown = client.post("/user/verify", json={"user": "nadie", "password": "loquesea"})
        assert unknown.status_code == 401


def test_first_registered_user_becomes_the_administrator():
    """El primer usuario registrado en la plataforma es el administrador."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "users.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS users (user TEXT PRIMARY KEY, password TEXT, role TEXT, created_at TEXT)")
        conn.commit()
        conn.close()
        database.user_db_file_location = db_path

        database.add_user("ana", "clave-ana", "user")
        assert database.get_user_info("ANA")["role"] == "admin"

        # Los siguientes usuarios NO son administradores.
        database.add_user("bob", "clave-bob", "user")
        assert database.get_user_info("BOB")["role"] == "user"


def test_platform_admin_is_resolved_from_the_database():
    """Quien administra la plataforma se lee de la base de datos, no de un nombre fijo."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "users.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS users (user TEXT PRIMARY KEY, password TEXT, role TEXT, created_at TEXT)")
        conn.commit()
        conn.close()
        database.user_db_file_location = db_path

        database.add_user("ana", "clave-ana", "user")
        database.add_user("bob", "clave-bob", "user")

        assert database.get_first_registered_user()["user"] == "ANA"
        assert database.get_platform_admin()["user"] == "ANA"
        assert database.is_platform_admin("ana") is True
        assert database.is_platform_admin("bob") is False
        # El listado de roles no filtra hashes de password.
        roles = database.list_user_roles()
        assert [r["user"] for r in roles] == ["ANA", "BOB"]
        assert "password" not in roles[0]


def test_sync_ignores_the_env_username_and_uses_the_database_admin():
    """El usuario del .env no decide quien es el administrador: lo decide la base de datos."""
    with tempfile.TemporaryDirectory() as tmp:
        env_path = os.path.join(tmp, ".env")
        db_path = os.path.join(tmp, "users.db")
        # El .env nombra a un usuario que no existe en la base de datos.
        _write_env(env_path, "usuario-del-env", "password-del-env")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS users (user TEXT PRIMARY KEY, password TEXT, role TEXT, created_at TEXT)")
        conn.commit()
        conn.close()
        database.user_db_file_location = db_path
        database.add_user("ana", "clave-de-ana", "user")

        os.environ["NEBULA_ENV_PATH"] = env_path
        os.environ.pop("NEBULA_ADMIN_PASSWORD_PENDING", None)
        database._sync_configured_credentials()

        # No se crea el usuario del .env ni se pisa la password de la admin.
        assert database.get_user_info("USUARIO-DEL-ENV") is None
        assert database.verify("ANA", "clave-de-ana") is True
        assert database.get_platform_admin()["user"] == "ANA"


def test_roles_endpoint_reports_the_platform_admin():
    """El controller expone quien administra la plataforma segun la base de datos."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "users.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS users (user TEXT PRIMARY KEY, password TEXT, role TEXT, created_at TEXT)")
        conn.commit()
        conn.close()
        database.user_db_file_location = db_path
        database.add_user("ana", "clave-ana", "user")
        database.add_user("bob", "clave-bob", "user")

        from nebula.controller.controller import app

        client = TestClient(app)
        r = client.get("/users/roles")
        assert r.status_code == 200
        body = r.json()
        assert body["platform_admin"] == "ANA"
        assert body["first_registered"] == "ANA"
        assert [u["user"] for u in body["users"]] == ["ANA", "BOB"]
        assert "password" not in body["users"][0]


# --- Guardia: ningun usuario administrador fijado en el codigo ----------------

# El ROL "admin" si es legitimo (comparar roles, add_user(..., "admin"), etc.);
# lo prohibido es un USUARIO concreto ("admin"/"ADMIN") usado como identidad.
FORBIDDEN_IDENTITY_PATTERNS = (
    (re.compile(r"\buser(?:name)?\s*=\s*user(?:name)?\s+or\s+['\"]admin['\"]"), "fallback de usuario admin"),
    (re.compile(r"\buser(?:name)?\s*=\s*['\"]admin['\"]"), "usuario admin asignado"),
    (re.compile(r"\buser(?:name)?\s*=\s*['\"]ADMIN['\"]"), "usuario ADMIN asignado"),
    (re.compile(r"['\"]ADMIN['\"]"), "literal ADMIN"),
    (re.compile(r"NEBULA_DEFAULT_USER\s*=\s*admin\b"), "NEBULA_DEFAULT_USER=admin"),
    (re.compile(r"\|\|\s*user\s*=\s*['\"]admin['\"]"), "fallback de shell user=admin"),
)


def _runtime_sources():
    """Ficheros de ejecucion donde no puede aparecer una identidad fija."""
    root = pathlib.Path(__file__).resolve().parents[1]
    files = [root / "scripts" / "NEBULA", root / ".env.example"]
    files += sorted((root / "nebula").rglob("*.py"))
    files += sorted((root / "app").glob("*.py"))
    return files


def test_no_hardcoded_admin_username_in_the_code():
    """El administrador se identifica con los datos de la base de datos, no con un literal."""
    offenders = []
    for path in _runtime_sources():
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern, label in FORBIDDEN_IDENTITY_PATTERNS:
            for match in pattern.finditer(content):
                line_no = content[: match.start()].count("\n") + 1
                offenders.append(f"{path.name}:{line_no} -> {label}")
    assert not offenders, "Identidad de administrador hardcodeada: " + "; ".join(offenders)


def test_bootstrap_does_not_invent_a_fixed_admin_name(monkeypatch):
    """Sin configuracion, el bootstrap genera un nombre unico (no "admin")."""
    from nebula.controller.api.security import bootstrap

    monkeypatch.delenv("NEBULA_DEFAULT_USER", raising=False)
    monkeypatch.delenv("NEBULA_DEFAULT_PASSWORD", raising=False)
    monkeypatch.setenv("NEBULA_PRODUCTION", "False")

    user, password = bootstrap.require_admin_credentials()
    assert user and password
    assert user.lower() != "admin"
    assert user.startswith("nebula-")
    # Dos llamadas nunca producen la misma identidad.
    other, _ = bootstrap.require_admin_credentials()
    assert other != user


def test_fresh_install_stores_the_admin_in_the_database(monkeypatch):
    """Instalación en limpio: el admin nace en la BD, no de un literal del código."""
    import asyncio

    monkeypatch.delenv("NEBULA_DEFAULT_USER", raising=False)
    monkeypatch.delenv("NEBULA_DEFAULT_PASSWORD", raising=False)
    monkeypatch.setenv("NEBULA_PRODUCTION", "False")

    with tempfile.TemporaryDirectory() as tmp:
        env_path = os.path.join(tmp, ".env")
        with open(env_path, "w") as f:
            f.write("SECRET_KEY=abc\n")
        monkeypatch.setenv("NEBULA_ENV_PATH", env_path)

        asyncio.run(database.initialize_databases(tmp))

        admin = database.get_platform_admin()
        assert admin is not None
        assert admin["role"] == "admin"
        assert admin["user"].lower() != "admin"
        # La identidad y la contraseña son las de la BD...
        values = EnvUtils.read_env_file(env_path)
        assert values["NEBULA_DEFAULT_USER"].upper() == admin["user"]
        assert database.verify(admin["user"], values["NEBULA_DEFAULT_PASSWORD"]) is True
        assert database.verify(admin["user"].lower(), values["NEBULA_DEFAULT_PASSWORD"]) is True
        assert database.verify(admin["user"], "otra-cosa") is False
        # ...y el siguiente usuario registrado NO es administrador.
        database.add_user("segundo", "clave-segundo", "user")
        assert database.get_user_info("SEGUNDO")["role"] == "user"
        assert database.get_platform_admin()["user"] == admin["user"]


