from fastapi import FastAPI, HTTPException, Request, status

from app.config import settings
from app.database import init_db
from app.routers.auth import router as auth_router
from app.routers.store import router as store_router

app = FastAPI(title=settings.app_name)


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.middleware("http")
async def enforce_body_limit(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > settings.max_request_body_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Request body too large")
    return await call_next(request)


app.include_router(auth_router)
app.include_router(store_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
