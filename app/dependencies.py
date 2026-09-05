from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import decode_access_token, get_agent_by_id, get_user_by_id
from app.config import settings
from app.database import get_db
from app.errors import AuthError, PermissionError_
from app.models.enums import AgentRole
from app.models.user import Agent, User

bearer = HTTPBearer(auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_db)]
Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]


def _claims(credentials: HTTPAuthorizationCredentials | None) -> dict:
    if credentials is None or not credentials.credentials:
        raise AuthError("Missing bearer token")
    return decode_access_token(credentials.credentials)


async def get_current_user(
    request: Request, credentials: Credentials, db: DbSession
) -> User:
    claims = _claims(credentials)
    if claims.get("typ") != "user":
        raise AuthError("This endpoint requires a customer token")

    user = await get_user_by_id(db, int(claims["sub"]))
    if user is None:
        raise AuthError("Account no longer exists")

    request.state.user_id = user.id
    request.state.rate_limit_key = f"user:{user.id}"
    return user


async def get_optional_user(
    request: Request, credentials: Credentials, db: DbSession
) -> User | None:
    if credentials is None:
        return None
    try:
        return await get_current_user(request, credentials, db)
    except AuthError:
        return None


async def get_current_agent(
    request: Request, credentials: Credentials, db: DbSession
) -> Agent:
    claims = _claims(credentials)
    if claims.get("typ") != "agent":
        raise AuthError("This endpoint requires a staff token")

    agent = await get_agent_by_id(db, int(claims["sub"]))
    if agent is None:
        raise AuthError("Agent no longer exists")

    # Checked on every request so deactivation takes effect immediately,
    # without waiting for the access token to expire.
    if not agent.active or agent.deactivated_at is not None:
        raise PermissionError_("This agent account has been deactivated")

    request.state.user_id = f"agent:{agent.id}"
    request.state.rate_limit_key = f"agent:{agent.id}"
    return agent


async def require_agent(agent: Annotated[Agent, Depends(get_current_agent)]) -> Agent:
    return agent


async def require_admin(agent: Annotated[Agent, Depends(get_current_agent)]) -> Agent:
    if agent.role != AgentRole.ADMIN:
        raise PermissionError_("This action requires an admin account")
    return agent


CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]
CurrentAgent = Annotated[Agent, Depends(require_agent)]
CurrentAdmin = Annotated[Agent, Depends(require_admin)]


def pagination_limit(limit: int | None = None) -> int:
    from app.pagination import clamp_limit

    return clamp_limit(limit)


PageLimit = Annotated[int, Depends(pagination_limit)]
