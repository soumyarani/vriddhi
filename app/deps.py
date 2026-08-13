from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Agent, User
from app.security import TokenError, decode_access_token

security_scheme = HTTPBearer(auto_error=False)


def _parse_token_payload(credentials: HTTPAuthorizationCredentials | None) -> dict:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing auth token")
    try:
        return decode_access_token(credentials.credentials)
    except TokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid auth token") from None


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
    db: Session = Depends(get_db),
) -> User:
    payload = _parse_token_payload(credentials)
    if payload.get("actor") not in (None, "user"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User token required")
    try:
        user_id = int(payload["sub"])
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid auth token") from None

    user = db.get(User, user_id)
    if not user or user.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    request.state.current_user_id = user.id
    return user


def get_current_agent(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
    db: Session = Depends(get_db),
) -> Agent:
    payload = _parse_token_payload(credentials)
    if payload.get("actor") != "agent":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Agent token required")
    try:
        agent_id = int(payload["sub"])
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid auth token") from None

    agent = db.get(Agent, agent_id)
    if not agent or not agent.active or agent.deactivated_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Agent not active")

    request.state.current_agent_id = agent.id
    return agent


def require_admin(agent: Agent = Depends(get_current_agent)) -> Agent:
    if agent.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return agent
