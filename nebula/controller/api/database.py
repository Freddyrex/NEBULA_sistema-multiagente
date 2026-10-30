from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from nebula.controller.api.settings import DATABASE_URL

# SQLite necesita check_same_thread=False porque FastAPI ejecuta los endpoints
# síncronos en un pool de hilos.
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=_connect_args, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """Dependencia de FastAPI: una sesión por petición."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Crea las tablas que no existan. (En producción usa migraciones con Alembic.)"""
    from nebula.controller.api import models  # noqa: F401  (registra los modelos en Base)

    Base.metadata.create_all(bind=engine)
