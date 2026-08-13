import httpx
from fastapi import HTTPException, status

from app.config import settings


async def fetch_google_user(code: str, redirect_uri: str | None = None) -> dict:
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth is not configured",
        )

    token_payload = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": redirect_uri or settings.google_redirect_uri,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=10) as client:
        token_res = await client.post("https://oauth2.googleapis.com/token", data=token_payload)
        if token_res.status_code >= 400:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Google authorization code")
        access_token = token_res.json().get("access_token")
        if not access_token:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google token exchange failed")
        user_res = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": "Bearer " + access_token},
        )
        if user_res.status_code >= 400:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unable to fetch Google user profile")
        user = user_res.json()
    if not user.get("id") or not user.get("email"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Google profile missing required fields")
    return user
