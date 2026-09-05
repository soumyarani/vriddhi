"""Google OAuth exchange, JWT issuance/verification, and refresh-token rotation."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.errors import AuthError, UpstreamError
from app.models.user import Agent, RefreshToken, User
from app.security import sha256_hex
from logging_config import get_logger

log = get_logger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}

# bcrypt caps input at 72 bytes; refresh tokens are pre-hashed to hex before hashing.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)

PrincipalType = Literal["user", "agent"]


# --------------------------------------------------------------------------
# Google OAuth
# --------------------------------------------------------------------------
async def exchange_google_code(code: str, redirect_uri: str | None = None) -> dict[str, Any]:
    """Trade an authorization code for Google tokens, then fetch the profile."""
    if not settings.google_client_id or not settings.google_client_secret:
        raise UpstreamError("Google OAuth is not configured")

    payload = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": redirect_uri or settings.google_redirect_uri,
        "grant_type": "authorization_code",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            token_res = await client.post(GOOGLE_TOKEN_URL, data=payload)
        except httpx.HTTPError as exc:
            raise UpstreamError("Could not reach Google") from exc

        if token_res.status_code != 200:
            log.warning("google_token_exchange_failed", status=token_res.status_code)
            raise AuthError("Invalid or expired Google authorization code")

        tokens = token_res.json()
        id_token = tokens.get("id_token")
        access_token = tokens.get("access_token")

        if id_token:
            profile = decode_google_id_token(id_token)
            if profile:
                return profile

        if not access_token:
            raise AuthError("Google did not return a usable token")

        info_res = await client.get(
            GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        if info_res.status_code != 200:
            raise AuthError("Could not fetch Google profile")
        return _normalize_google_profile(info_res.json())


def decode_google_id_token(id_token: str) -> dict[str, Any] | None:
    """Read claims from Google's ID token.

    Signature verification is skipped deliberately: the token came directly
    from Google's token endpoint over TLS using our client secret, so it is
    already authenticated. Issuer and audience are still checked.
    """
    try:
        claims = jwt.get_unverified_claims(id_token)
    except JWTError:
        return None

    if claims.get("iss") not in GOOGLE_ISSUERS:
        log.warning("google_id_token_bad_issuer")
        return None
    if claims.get("aud") != settings.google_client_id:
        log.warning("google_id_token_bad_audience")
        return None
    if not claims.get("email"):
        return None
    return _normalize_google_profile(claims)


def _normalize_google_profile(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "google_id": raw.get("sub"),
        "email": (raw.get("email") or "").lower() or None,
        "email_verified": bool(raw.get("email_verified", True)),
        "name": raw.get("name"),
        "picture": raw.get("picture"),
    }


def is_agent_domain_allowed(email: str) -> bool:
    allowed = settings.agent_domains
    if not allowed:
        # Fail closed: an unset allowlist must not grant staff access to anyone.
        log.error("agent_domains_not_configured")
        return False
    return email.lower().rsplit("@", 1)[-1] in allowed


# --------------------------------------------------------------------------
# JWT
# --------------------------------------------------------------------------
def _signing_key() -> tuple[str, str]:
    return settings.jwt_secret, settings.jwt_key_id


def _key_for_kid(kid: str | None) -> str | None:
    if kid == settings.jwt_key_id or kid is None:
        return settings.jwt_secret
    if settings.jwt_key_id_previous and kid == settings.jwt_key_id_previous:
        return settings.jwt_secret_previous
    return None


def create_access_token(
    subject: int,
    principal: PrincipalType,
    role: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    secret, kid = _signing_key()
    now = datetime.now(timezone.utc)
    claims: dict[str, Any] = {
        "sub": str(subject),
        "typ": principal,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_ttl_minutes)).timestamp()),
        "jti": secrets.token_urlsafe(12),
    }
    if role:
        claims["role"] = role
    if extra:
        claims.update(extra)
    return jwt.encode(claims, secret, algorithm=settings.jwt_algorithm, headers={"kid": kid})


def decode_access_token(token: str) -> dict[str, Any]:
    """Verify a JWT, honouring the previous key during rotation."""
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except JWTError as exc:
        raise AuthError("Malformed token") from exc

    secret = _key_for_kid(kid)
    if not secret:
        raise AuthError("Token signed with an unknown key")

    try:
        return jwt.decode(token, secret, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise AuthError("Invalid or expired token") from exc


# --------------------------------------------------------------------------
# Refresh tokens
# --------------------------------------------------------------------------
def generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    """bcrypt verifier. Input is pre-hashed to stay under bcrypt's 72-byte cap."""
    return pwd_context.hash(sha256_hex(token))


def verify_refresh_token(token: str, token_hash: str) -> bool:
    try:
        return pwd_context.verify(sha256_hex(token), token_hash)
    except Exception:
        return False


def lookup_hash(token: str) -> str:
    """Deterministic index key — bcrypt hashes are salted and unsearchable."""
    return sha256_hex(token)


async def issue_refresh_token(
    db: AsyncSession,
    *,
    user_id: int | None = None,
    agent_id: int | None = None,
    device_info: str | None = None,
    replaces: RefreshToken | None = None,
) -> str:
    raw = generate_refresh_token()
    record = RefreshToken(
        user_id=user_id,
        agent_id=agent_id,
        lookup_hash=lookup_hash(raw),
        token_hash=hash_refresh_token(raw),
        device_info=(device_info or "")[:255] or None,
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_ttl_days),
    )
    db.add(record)
    await db.flush()

    if replaces is not None:
        replaces.revoked_at = datetime.now(timezone.utc)
        replaces.replaced_by_id = record.id

    return raw


async def resolve_refresh_token(db: AsyncSession, raw_token: str) -> RefreshToken:
    """Look up and validate a refresh token, detecting reuse of a rotated one."""
    stmt = select(RefreshToken).where(RefreshToken.lookup_hash == lookup_hash(raw_token))
    record = (await db.execute(stmt)).scalar_one_or_none()

    if record is None or not verify_refresh_token(raw_token, record.token_hash):
        raise AuthError("Invalid refresh token")

    now = datetime.now(timezone.utc)
    if record.revoked_at is not None:
        # A revoked token being presented means it leaked after rotation.
        # Kill the whole family so the attacker and victim are both logged out.
        log.warning(
            "refresh_token_reuse_detected",
            user_id=record.user_id,
            agent_id=record.agent_id,
        )
        await _revoke_family_durably(user_id=record.user_id, agent_id=record.agent_id)
        raise AuthError("Refresh token has been revoked")

    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise AuthError("Refresh token has expired")

    return record


async def revoke_refresh_token(db: AsyncSession, raw_token: str) -> bool:
    stmt = select(RefreshToken).where(RefreshToken.lookup_hash == lookup_hash(raw_token))
    record = (await db.execute(stmt)).scalar_one_or_none()
    if record is None or record.revoked_at is not None:
        return False
    record.revoked_at = datetime.now(timezone.utc)
    return True


async def _revoke_family_durably(*, user_id: int | None, agent_id: int | None) -> None:
    """Revoke a token family in its own transaction.

    The caller raises `AuthError` immediately after this, which rolls back the
    request session — so revoking on that session would undo the very thing the
    reuse detection exists to do. This commits independently, and swallows its
    own failure so a bookkeeping error still surfaces as a clean 401.
    """
    from app.database import session_scope

    try:
        async with session_scope() as fresh:
            revoked = await revoke_all_sessions(fresh, user_id=user_id, agent_id=agent_id)
        log.warning("refresh_token_family_revoked", user_id=user_id, agent_id=agent_id, count=revoked)
    except Exception as exc:  # noqa: BLE001
        log.error("refresh_family_revoke_failed", user_id=user_id, agent_id=agent_id, error=str(exc))


async def revoke_all_sessions(
    db: AsyncSession, *, user_id: int | None = None, agent_id: int | None = None
) -> int:
    if user_id is None and agent_id is None:
        return 0
    stmt = select(RefreshToken).where(RefreshToken.revoked_at.is_(None))
    stmt = stmt.where(
        RefreshToken.user_id == user_id if user_id is not None else RefreshToken.agent_id == agent_id
    )
    records = (await db.execute(stmt)).scalars().all()
    now = datetime.now(timezone.utc)
    for record in records:
        record.revoked_at = now
    return len(records)


# --------------------------------------------------------------------------
# Principal lookup
# --------------------------------------------------------------------------
async def get_user_by_id(db: AsyncSession, user_id: int) -> User | None:
    stmt = select(User).where(User.id == user_id, User.deleted_at.is_(None))
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_agent_by_id(db: AsyncSession, agent_id: int) -> Agent | None:
    return (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
