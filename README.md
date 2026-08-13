# vriddhi

FastAPI backend foundation for a dual-channel commerce platform (WhatsApp + Web/App).

## Implemented scope
- SQLAlchemy schema covering users/auth, catalog, cart, orders/payments, coupons, reviews, conversations, and operations tables
- Google OAuth token exchange endpoint with account upsert/linking
- JWT access + refresh token issuance with refresh-token rotation and revocation
- Auth endpoints:
  - `POST /api/auth/google`
  - `POST /api/auth/refresh`
  - `POST /api/auth/logout`
  - `GET /api/auth/me`
- Customer catalog endpoint:
  - `GET /api/store/categories` (JWT-protected, active/non-deleted only, paginated)
- Request body limit middleware (1MB)
- In-memory rate limit for `POST /api/auth/google` (10 req/min per IP)
- Focused tests for auth flow, category visibility, and rate-limiting behavior

## Run locally
```bash
pip install -e '.[dev]'
uvicorn app.main:app --reload
pytest -q
```
