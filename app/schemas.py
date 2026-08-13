from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class GoogleAuthRequest(BaseModel):
    code: str = Field(min_length=1)
    redirect_uri: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    phone: str | None
    email: EmailStr | None
    name: str | None
    picture_url: str | None
    auth_provider: str
    created_at: datetime


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    image_url: str | None
    sort_order: int
