from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nebula.controller.api.catalog import (
    DATASET_MODELS,
    Aggregator,
    ExperimentStatus,
    Federation,
    Topology,
)


class ExperimentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, examples=["MNIST ring FedAvg"])
    description: str | None = Field(default=None, max_length=2000)
    federation: Federation = "DFL"
    topology: Topology = "Fully"
    dataset: str = Field(examples=["MNIST"])
    model: str = Field(examples=["MLP"])
    agg_algorithm: Aggregator = "FedAvg"
    n_nodes: int = Field(default=4, ge=2, le=50)
    rounds: int = Field(default=3, ge=1, le=1000)
    epochs: int = Field(default=1, ge=1, le=100)
    status: ExperimentStatus = "draft"

    @model_validator(mode="after")
    def check_dataset_and_model(self):
        if self.dataset not in DATASET_MODELS:
            raise ValueError(f"dataset inválido. Opciones: {sorted(DATASET_MODELS)}")
        if self.model not in DATASET_MODELS[self.dataset]:
            raise ValueError(f"model inválido para {self.dataset}. Opciones: {DATASET_MODELS[self.dataset]}")
        return self


class ExperimentPatch(BaseModel):
    """Actualización parcial: solo se modifican los campos enviados."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    federation: Federation | None = None
    topology: Topology | None = None
    dataset: str | None = None
    model: str | None = None
    agg_algorithm: Aggregator | None = None
    n_nodes: int | None = Field(default=None, ge=2, le=50)
    rounds: int | None = Field(default=None, ge=1, le=1000)
    epochs: int | None = Field(default=None, ge=1, le=100)
    status: ExperimentStatus | None = None


class ExperimentResponse(ExperimentCreate):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    created_at: datetime
    updated_at: datetime
