from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from nebula.controller.api.catalog import ExperimentStatus
from nebula.controller.api.database import get_db
from nebula.controller.api.models.experiment import Experiment
from nebula.controller.api.models.user import User
from nebula.controller.api.schemas.experiment import ExperimentCreate, ExperimentPatch, ExperimentResponse
from nebula.controller.api.security.deps import get_current_user

router = APIRouter(
    prefix="/experiments",
    tags=["Experiments"],
    responses={401: {"description": "Missing, invalid or expired access token"}},
)


def get_owned_experiment(experiment_id: int, db: Session, user: User) -> Experiment:
    """Autorización: 404 si no existe, 403 si existe pero pertenece a otro usuario."""
    experiment = db.get(Experiment, experiment_id)
    if experiment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Experiment not found")
    if experiment.user_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not own this experiment")
    return experiment


@router.post(
    "",
    response_model=ExperimentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an experiment",
    description="The owner is always the authenticated user; a `user_id` in the body is ignored.",
)
def create_experiment(
    payload: ExperimentCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    experiment = Experiment(**payload.model_dump(), user_id=current_user.id)
    db.add(experiment)
    db.commit()
    db.refresh(experiment)
    return experiment


@router.get(
    "",
    response_model=list[ExperimentResponse],
    summary="List my experiments",
    description="Only the experiments of the authenticated user, newest first.",
)
def list_experiments(
    status_filter: ExperimentStatus | None = Query(default=None, alias="status"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = select(Experiment).where(Experiment.user_id == current_user.id)
    if status_filter:
        query = query.where(Experiment.status == status_filter)
    query = query.order_by(Experiment.created_at.desc(), Experiment.id.desc()).offset(skip).limit(limit)
    return db.scalars(query).all()


@router.get(
    "/{experiment_id}",
    response_model=ExperimentResponse,
    summary="Get an experiment",
    responses={403: {"description": "Belongs to another user"}, 404: {"description": "Not found"}},
)
def get_experiment(
    experiment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return get_owned_experiment(experiment_id, db, current_user)


@router.put(
    "/{experiment_id}",
    response_model=ExperimentResponse,
    summary="Replace an experiment",
    description="Full update: every field of the experiment must be sent.",
    responses={403: {"description": "Belongs to another user"}, 404: {"description": "Not found"}},
)
def replace_experiment(
    experiment_id: int,
    payload: ExperimentCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    experiment = get_owned_experiment(experiment_id, db, current_user)
    for field, value in payload.model_dump().items():
        setattr(experiment, field, value)
    db.commit()
    db.refresh(experiment)
    return experiment


@router.patch(
    "/{experiment_id}",
    response_model=ExperimentResponse,
    summary="Partially update an experiment",
    description="Only the fields sent are changed. The resulting experiment is validated as a whole.",
    responses={403: {"description": "Belongs to another user"}, 404: {"description": "Not found"}},
)
def patch_experiment(
    experiment_id: int,
    payload: ExperimentPatch,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    experiment = get_owned_experiment(experiment_id, db, current_user)
    current = ExperimentCreate.model_validate(experiment, from_attributes=True).model_dump()
    merged = {**current, **payload.model_dump(exclude_unset=True)}
    try:
        validated = ExperimentCreate(**merged)
    except ValueError as exc:  # p. ej. cambiar dataset sin cambiar model
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    for field, value in validated.model_dump().items():
        setattr(experiment, field, value)
    db.commit()
    db.refresh(experiment)
    return experiment


@router.delete(
    "/{experiment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an experiment",
    responses={403: {"description": "Belongs to another user"}, 404: {"description": "Not found"}},
)
def delete_experiment(
    experiment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    experiment = get_owned_experiment(experiment_id, db, current_user)
    db.delete(experiment)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
