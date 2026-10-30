from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr = Field(examples=["ana@example.com"])
    password: str = Field(min_length=1, max_length=128, examples=["MySecurePassword123!"])
    remember_me: bool = Field(default=False, description="Si es true, el refresh token dura más (30 días por defecto).")


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Segundos de vida del access token.", examples=[900])
