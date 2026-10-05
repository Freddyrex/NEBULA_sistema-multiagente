from typing import get_args

from fastapi import APIRouter

from nebula.controller.api.catalog import DATASET_MODELS, Aggregator, Federation, Topology

router = APIRouter(prefix="/public", tags=["Public"])


@router.get(
    "/info",
    summary="Obtain public API information",
    description="Returns general information about the API without requiring authentication.",
)
def public_info():
    return {"api": "NEBULA API", "version": "1.0", "authentication_required": False}


@router.get(
    "/datasets",
    summary="List the datasets and models supported by NEBULA",
    description="Public catalog: each dataset with the models that can be trained on it.",
)
def public_datasets():
    return [{"dataset": name, "models": models} for name, models in DATASET_MODELS.items()]


@router.get(
    "/aggregators",
    summary="List the aggregation algorithms",
    description="Aggregation algorithms available for a federation.",
)
def public_aggregators():
    return list(get_args(Aggregator))


@router.get(
    "/topologies",
    summary="List the federation types and network topologies",
    description="Federation types (DFL, SDFL, CFL) and topologies accepted when describing an experiment.",
)
def public_topologies():
    return {"federations": list(get_args(Federation)), "topologies": list(get_args(Topology))}
