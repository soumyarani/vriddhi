from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.google_oauth import fetch_google_user
from app.models import RefreshToken, User
from app.rate_limit import InMemoryRateLimiter
from app.schemas import GoogleAuthRequest, LogoutRequest, RefreshRequest, TokenPair, UserProfile
from app.security import create_access_token, create_refresh_token, hash_refresh_token

router = APIRouter(prefix="/api/auth", tags=["auth"])
rate_limiter = InMemoryRateLimiter(max_requests=10, window_seconds=60)


def _find_or_create_google_user(db: Session, google_user: dict) -> User:
    google_id = google_user["id"]
    email = google_user.get("email")

    user = db.scalar(select(User).where(User.google_id == google_id))
    if user:
        user.email = email or user.email
        user.name = google_user.get("name") or user.name
        user.picture_url = google_user.get("picture") or user.picture_url
        user.auth_provider = "google"
        db.flush()
        return user

    if email:
        user = db.scalar(select(User).where(User.email == email))
        if user:
            user.google_id = google_id
            user.name = google_user.get("name") or user.name
            user.picture_url = google_user.get("picture") or user.picture_url
            user.auth_provider = "google"
            db.flush()
            return user

    user = User(
        email=email,
        name=google_user.get("name"),
        google_id=google_id,
        picture_url=google_user.get("picture"),
        auth_provider="google",
        whatsapp_opt_in=True,
    )
    db.add(user)
    db.flush()
    return user


def _issue_tokens(db: Session, user: User, device_info: str | None) -> TokenPair:
    access_token = create_access_token(subject=str(user.id))
    refresh_token = create_refresh_token()

    refresh_record = RefreshToken(
        user_id=user.id,
        token_hash=hash_refresh_token(refresh_token),
        device_info=(device_info or "")[:255] or None,
        expires_at=datetime.utcnow() + timedelta(days=settings.refresh_token_expire_days),
    )
    db.add(refresh_record)
    db.flush()

    return TokenPair(access_token=access_token, refresh_token=refresh_token)


@router.post("/google", response_model=TokenPair)
async def google_auth(payload: GoogleAuthRequest, request: Request, db: Session = Depends(get_db)) -> TokenPair:
    client_ip = request.client.host if request.client else "unknown"
    if not rate_limiter.allow(client_ip):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")

    google_user = await fetch_google_user(payload.code, payload.redirect_uri)

    user = _find_or_create_google_user(db, google_user)
    token_pair = _issue_tokens(db, user, request.headers.get("user-agent"))
    db.commit()
    return token_pair


@router.post("/refresh", response_model=TokenPair)
def refresh_tokens(payload: RefreshRequest, request: Request, db: Session = Depends(get_db)) -> TokenPair:
    token_hash = hash_refresh_token(payload.refresh_token)
    token_row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    now = datetime.utcnow()

    if not token_row or token_row.revoked_at is not None or token_row.expires_at < now:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    token_row.revoked_at = now
    user = db.get(User, token_row.user_id)
    if not user or user.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    new_pair = _issue_tokens(db, user, request.headers.get("user-agent"))
    db.commit()
    return new_pair


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(payload: LogoutRequest, db: Session = Depends(get_db)) -> None:
    token_hash = hash_refresh_token(payload.refresh_token)
    token_row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    if token_row and token_row.revoked_at is None:
        token_row.revoked_at = datetime.utcnow()
        db.commit()


@router.get("/me", response_model=UserProfile)
def me(current_user: User = Depends(get_current_user)) -> UserProfile:
    return UserProfile.model_validate(current_user)
