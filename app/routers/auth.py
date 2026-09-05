"""Authentication endpoints.

Two separate Google sign-in flows share one token format:

* ``/api/auth/google`` — customers. An account is created on first sign-in.
* ``/api/auth/agent/google`` — staff. The email domain must be allow-listed and
  a matching ``Agent`` row must already exist; staff accounts are never
  auto-provisioned from a login.

Every mutation commits explicitly: ``get_db`` only rolls back on error, it does
not commit for us.
"""

# NOTE: `from __future__ import annotations` is deliberately absent. The slowapi
# limiter decorator wraps endpoints with functools.wraps, which keeps slowapi's
# module globals on the wrapper, so FastAPI cannot resolve string annotations
# back to these schema classes.
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    create_access_token,
    decode_access_token,
    exchange_google_code,
    get_agent_by_id,
    get_user_by_id,
    is_agent_domain_allowed,
    issue_refresh_token,
    lookup_hash,
    resolve_refresh_token,
    revoke_all_sessions,
    revoke_refresh_token,
)
from app.config import settings
from app.dependencies import Credentials, DbSession
from app.errors import AuthError, PermissionError_
from app.models.enums import AuthProvider
from app.models.user import Agent, RefreshToken, User
from app.rate_limit import limiter
from app.schemas.auth import (
    AgentAuthResponse,
    AgentProfile,
    AuthResponse,
    GoogleAuthRequest,
    LogoutRequest,
    RefreshRequest,
    TokenPair,
    UserProfile,
)
from app.schemas.common import AUTH_RESPONSES, MessageResponse
from app.services import user as user_service
from app.services.account_merge import link_or_merge_on_google_login
from logging_config import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"], responses=AUTH_RESPONSES)

AUTH_LIMIT = settings.rate_limit_auth


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _device_info(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _token_pair(access_token: str, refresh_token: str) -> TokenPair:
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.access_token_ttl_minutes * 60,
    )


def _verified_google_phone(profile: dict[str, Any]) -> str | None:
    """Pull a phone number out of the Google profile when it is verified.

    Google only returns this for workspace/People-scoped tokens, so it is
    usually absent — the merge path below is a no-op in that case.
    """
    phone = profile.get("phone_number") or profile.get("phone")
    if not phone:
        return None
    verified = profile.get("phone_number_verified")
    if verified is False:
        return None
    return str(phone)


async def _google_profile(body: GoogleAuthRequest) -> dict[str, Any]:
    profile = await exchange_google_code(body.code, body.redirect_uri)
    email = (profile.get("email") or "").strip().lower()
    if not email:
        raise AuthError("Google did not return an email address")
    if not profile.get("email_verified", True):
        raise AuthError("Your Google email address is not verified")
    profile["email"] = email
    return profile


async def _find_refresh_token(db: AsyncSession, raw_token: str) -> RefreshToken | None:
    stmt = select(RefreshToken).where(RefreshToken.lookup_hash == lookup_hash(raw_token))
    return (await db.execute(stmt)).scalar_one_or_none()


# --------------------------------------------------------------------------
# Customer login
# --------------------------------------------------------------------------
@router.post("/google", response_model=AuthResponse, summary="Customer Google sign-in")
@limiter.limit(AUTH_LIMIT)
async def google_login(
    request: Request,
    response: Response,
    body: GoogleAuthRequest,
    db: DbSession,
) -> AuthResponse:
    profile = await _google_profile(body)
    google_id = profile.get("google_id")

    user: User | None = None
    if google_id:
        user = await user_service.get_by_google_id(db, google_id)
    if user is None:
        user = await user_service.get_by_email(db, profile["email"])

    is_new_user = user is None
    if user is None:
        user = User(
            email=profile["email"],
            name=profile.get("name"),
            google_id=google_id,
            picture_url=profile.get("picture"),
            auth_provider=AuthProvider.GOOGLE,
        )
        db.add(user)
        await db.flush()
    else:
        # An account that started life on WhatsApp is upgraded in place.
        if google_id and not user.google_id:
            user.google_id = google_id
            user.auth_provider = AuthProvider.GOOGLE
        if not user.email:
            user.email = profile["email"]
        if not user.name and profile.get("name"):
            user.name = profile["name"]
        if profile.get("picture"):
            user.picture_url = profile["picture"]

    user.last_login_at = _now()
    await db.flush()

    merged = False
    phone = _verified_google_phone(profile)
    if phone:
        result = await link_or_merge_on_google_login(db, user, phone)
        merged = bool(result.get("merged"))

    access_token = create_access_token(user.id, "user")
    refresh_token = await issue_refresh_token(
        db, user_id=user.id, device_info=_device_info(request)
    )
    await db.commit()

    log.info("customer_login", user_id=user.id, is_new_user=is_new_user, merged=merged)
    return AuthResponse(
        tokens=_token_pair(access_token, refresh_token),
        user=UserProfile.model_validate(user_service.serialize_user(user)),
        is_new_user=is_new_user,
        accounts_merged=merged,
    )


# --------------------------------------------------------------------------
# Staff login
# --------------------------------------------------------------------------
@router.post(
    "/agent/google", response_model=AgentAuthResponse, summary="Staff Google sign-in"
)
@limiter.limit(AUTH_LIMIT)
async def agent_login(
    request: Request,
    response: Response,
    body: GoogleAuthRequest,
    db: DbSession,
) -> AgentAuthResponse:
    profile = await _google_profile(body)
    email = profile["email"]

    # Fails closed when AGENT_ALLOWED_DOMAINS is unset.
    if not is_agent_domain_allowed(email):
        log.warning("agent_login_domain_rejected", email=email)
        raise PermissionError_("This email domain is not permitted for staff access")

    agent = (
        await db.execute(select(Agent).where(func.lower(Agent.email) == email))
    ).scalar_one_or_none()

    # Staff are provisioned by an admin, never by signing in.
    if agent is None:
        log.warning("agent_login_unknown_email", email=email)
        raise AuthError("No staff account exists for this email address")

    if not agent.active or agent.deactivated_at is not None:
        raise PermissionError_("This agent account has been deactivated")

    google_id = profile.get("google_id")
    if google_id and not agent.google_id:
        agent.google_id = google_id
    if profile.get("picture"):
        agent.picture_url = profile["picture"]
    agent.last_login_at = _now()
    await db.flush()

    access_token = create_access_token(agent.id, "agent", role=agent.role)
    refresh_token = await issue_refresh_token(
        db, agent_id=agent.id, device_info=_device_info(request)
    )
    await db.commit()

    log.info("agent_login", agent_id=agent.id, role=agent.role)
    return AgentAuthResponse(
        tokens=_token_pair(access_token, refresh_token),
        agent=AgentProfile.model_validate(agent),
    )


# --------------------------------------------------------------------------
# Rotation
# --------------------------------------------------------------------------
@router.post("/refresh", response_model=TokenPair, summary="Rotate a refresh token")
@limiter.limit(AUTH_LIMIT)
async def refresh_tokens(
    request: Request,
    response: Response,
    body: RefreshRequest,
    db: DbSession,
) -> TokenPair:
    record = await resolve_refresh_token(db, body.refresh_token)

    if record.user_id is not None:
        principal = await get_user_by_id(db, record.user_id)
        if principal is None:
            raise AuthError("Account no longer exists")
        access_token = create_access_token(principal.id, "user")
    elif record.agent_id is not None:
        agent = await get_agent_by_id(db, record.agent_id)
        if agent is None:
            raise AuthError("Agent no longer exists")
        if not agent.active or agent.deactivated_at is not None:
            raise PermissionError_("This agent account has been deactivated")
        access_token = create_access_token(agent.id, "agent", role=agent.role)
    else:
        raise AuthError("Refresh token is not bound to an account")

    refresh_token = await issue_refresh_token(
        db,
        user_id=record.user_id,
        agent_id=record.agent_id,
        device_info=_device_info(request),
        replaces=record,
    )
    await db.commit()
    return _token_pair(access_token, refresh_token)


@router.post("/logout", response_model=MessageResponse, summary="Revoke a session")
@limiter.limit(AUTH_LIMIT)
async def logout(
    request: Request,
    response: Response,
    body: LogoutRequest,
    db: DbSession,
) -> MessageResponse:
    # Deliberately idempotent: an unknown or already-revoked token still
    # returns 200 so a client can always reach a signed-out state.
    if body.all_devices:
        record = await _find_refresh_token(db, body.refresh_token)
        if record is not None:
            await revoke_all_sessions(
                db, user_id=record.user_id, agent_id=record.agent_id
            )
    else:
        await revoke_refresh_token(db, body.refresh_token)

    await db.commit()
    return MessageResponse(detail="Signed out")


# --------------------------------------------------------------------------
# Whoami
# --------------------------------------------------------------------------
@router.get(
    "/me",
    response_model=UserProfile | AgentProfile,
    summary="Profile for the bearer token",
)
async def me(credentials: Credentials, db: DbSession) -> Any:
    if credentials is None or not credentials.credentials:
        raise AuthError("Missing bearer token")

    claims = decode_access_token(credentials.credentials)
    subject = claims.get("sub")
    if subject is None:
        raise AuthError("Malformed token")

    if claims.get("typ") == "agent":
        agent = await get_agent_by_id(db, int(subject))
        if agent is None:
            raise AuthError("Agent no longer exists")
        if not agent.active or agent.deactivated_at is not None:
            raise PermissionError_("This agent account has been deactivated")
        return AgentProfile.model_validate(agent)

    user = await get_user_by_id(db, int(subject))
    if user is None:
        raise AuthError("Account no longer exists")
    return UserProfile.model_validate(user_service.serialize_user(user))
