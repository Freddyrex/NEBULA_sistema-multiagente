from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# Argon2id con los parámetros por defecto de argon2-cffi (la misma librería que ya usa Nebula).
_hasher = PasswordHasher()

# Hash "señuelo": se verifica contra él cuando el email no existe, para que el tiempo de
# respuesta no revele si una cuenta existe.
_DUMMY_HASH = _hasher.hash("nebula-dummy-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False
