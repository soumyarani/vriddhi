"""JWT issuance, refresh-token rotation and agent domain gating."""

from __future__ import annotations

import pytest

from app.auth import (
    create_access_token,
    decode_access_token,
    is_agent_domain_allowed,
    issue_refresh_token,
    resolve_refresh_token,
    revoke_refresh_token,
)
from app.errors import AuthError


async def test_access_token_round_trips(user):
    claims = decode_access_token(create_access_token(user.id, "user"))
    assert claims["sub"] == str(user.id)
    assert claims["typ"] == "user"


async def test_tampered_token_is_rejected(user):
    token = create_access_token(user.id, "user")
    with pytest.raises(AuthError):
        decode_access_token(token[:-3] + "aaa")


async def test_refresh_token_is_not_stored_in_the_clear(db, user):
    from sqlalchemy import select

    from app.models.user import RefreshToken

    raw = await issue_refresh_token(db, user_id=user.id)
    stored = (await db.execute(select(RefreshToken))).scalars().all()
    assert len(stored) == 1
    assert stored[0].token_hash != raw
    assert raw not in stored[0].token_hash


async def test_rotation_revokes_the_previous_token(db, user):
    first = await issue_refresh_token(db, user_id=user.id)
    record = await resolve_refresh_token(db, first)
    await issue_refresh_token(db, user_id=user.id, replaces=record)

    assert record.revoked_at is not None
    with pytest.raises(AuthError):
        await resolve_refresh_token(db, first)


async def test_reuse_of_a_rotated_token_kills_the_family(db, user, monkeypatch):
    """Presenting a revoked token must log every session out.

    The revocation is written in its own transaction because the caller raises
    straight after, which rolls the request session back. That separate session
    is redirected onto this test's connection so the assertion can see it.
    """
    from contextlib import asynccontextmanager

    import app.auth as auth_mod

    @asynccontextmanager
    async def _scope():
        yield db

    monkeypatch.setattr("app.database.session_scope", _scope)

    old = await issue_refresh_token(db, user_id=user.id)
    record = await resolve_refresh_token(db, old)
    live = await issue_refresh_token(db, user_id=user.id, replaces=record)

    with pytest.raises(AuthError):
        await resolve_refresh_token(db, old)

    # The still-valid token issued during rotation must now be dead too.
    with pytest.raises(AuthError):
        await resolve_refresh_token(db, live)


async def test_logout_revokes_only_once(db, user):
    raw = await issue_refresh_token(db, user_id=user.id)
    assert await revoke_refresh_token(db, raw) is True
    assert await revoke_refresh_token(db, raw) is False


async def test_unknown_refresh_token_is_rejected(db):
    with pytest.raises(AuthError):
        await resolve_refresh_token(db, "not-a-real-token")


def test_agent_domain_allowlist():
    assert is_agent_domain_allowed("someone@shop.test") is True
    assert is_agent_domain_allowed("attacker@evil.example") is False


async def test_me_requires_a_token(client):
    assert (await client.get("/api/auth/me")).status_code in (401, 403)


async def test_me_returns_the_caller(client, auth_headers, user):
    resp = await client.get("/api/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["email"] == user.email


async def test_agent_token_cannot_use_customer_routes(client, agent_headers):
    """An agent JWT is not a customer JWT — the `typ` claim must be enforced."""
    resp = await client.get("/api/cart", headers=agent_headers)
    assert resp.status_code in (401, 403)
