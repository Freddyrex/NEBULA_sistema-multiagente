from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nebula.controller.api.database import Base
from nebula.controller.api.models.user import utcnow


class Experiment(Base):
    """Configuración de un experimento de aprendizaje federado, propiedad de un usuario."""

    __tablename__ = "experiments"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    federation: Mapped[str] = mapped_column(String(10))
    topology: Mapped[str] = mapped_column(String(20))
    dataset: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(30))
    agg_algorithm: Mapped[str] = mapped_column(String(30))
    n_nodes: Mapped[int] = mapped_column(Integer)
    rounds: Mapped[int] = mapped_column(Integer)
    epochs: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    owner = relationship("User", back_populates="experiments")
