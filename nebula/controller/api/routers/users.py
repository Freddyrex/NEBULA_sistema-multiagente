from fastapi import APIRouter, Depends

from nebula.controller.api.models.user import User
from nebula.controller.api.schemas.user import UserResponse
from nebula.controller.api.security.deps import get_current_user

router = APIRouter(prefix="/users", tags=["Users"])


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Get the authenticated user",
    description="Returns the profile of the user identified by the Bearer access token. Never includes the password hash.",
    responses={401: {"description": "Missing, invalid or expired access token"}},
)
def read_me(current_user: User = Depends(get_current_user)):
    return current_user
