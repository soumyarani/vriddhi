from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import ORMModel


class GoogleAuthRequest(BaseModel):
    code: str = Field(min_length=1, max_length=2048)
    redirect_uri: str | None = Field(default=None, max_length=2048)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=512)


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=1, max_length=512)
    all_devices: bool = False


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserProfile(ORMModel):
    id: int
    name: str | None = None
    email: EmailStr | None = None
    phone: str | None = None
    picture_url: str | None = None
    auth_provider: str
    whatsapp_opt_in: bool
    gstin: str | None = None
    created_at: datetime


class AgentProfile(ORMModel):
    id: int
    name: str
    email: EmailStr
    role: str
    active: bool
    picture_url: str | None = None


class AuthResponse(BaseModel):
    tokens: TokenPair
    user: UserProfile
    is_new_user: bool = False
    accounts_merged: bool = False


class AgentAuthResponse(BaseModel):
    tokens: TokenPair
    agent: AgentProfile
