from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserRegister(BaseModel):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_.-]+$", examples=["student01"])
    full_name: str = Field(min_length=1, max_length=150, examples=["Ana Torres"])
    email: EmailStr = Field(examples=["ana@example.com"])
    password: str = Field(min_length=8, max_length=128, examples=["MySecurePassword123!"])


class UserResponse(BaseModel):
    """Usuario público. Nunca incluye `password_hash`."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    full_name: str
    email: EmailStr
    role: str
    auth_provider: str
    created_at: datetime
