# WhatsApp Commerce Platform

A shopping backend with two customer-facing channels over one set of data:

- **WhatsApp** — customers are identified by phone number, arriving through the Meta Cloud API webhook. An AI assistant handles browsing, cart and checkout; a human agent confirms every order.
- **Web storefront** — customers sign in with Google. Same products, same cart, same orders.

One FastAPI service, PostgreSQL as the store of record, Redis for cache/rate limiting/queue, and an arq worker for everything that must not happen inside a request.

---

## Quick start

### With Docker (everything included)

```bash
cp .env.example .env          # then fill in the CHANGE_ME_ values
docker compose up -d
docker compose exec api alembic upgrade head
docker compose exec api python seed_data.py
```

API on <http://localhost:8000>, interactive docs on <http://localhost:8000/docs>.

## Documentation

| Document | Contents |
| --- | --- |
| [docs/openapi.json](docs/openapi.json) / [.yaml](docs/openapi.yaml) | Swagger/OpenAPI 3.1 spec — 78 paths, 94 operations. Import into Postman, Insomnia or a client generator |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Managing the database, the chatbot, and the Meta/WhatsApp configuration, plus a troubleshooting index |
| `/docs`, `/redoc` | The same spec, browsable, when the app is running |

Regenerate the spec after changing any route:

```bash
.venv/bin/python export_openapi.py            # writes docs/openapi.{json,yaml}
.venv/bin/python export_openapi.py --check    # CI: fails if out of date
```

### Locally

Requires Python 3.11+, plus PostgreSQL 16 and Redis 7 reachable at the URLs in your `.env`.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env          # then fill in the CHANGE_ME_ values
.venv/bin/alembic upgrade head
.venv/bin/python seed_data.py

.venv/bin/uvicorn app.main:app --reload
```

The background worker is a **separate process** and the app is not fully functional without it — webhooks ACK immediately and do their real work on the queue, so with no worker running, inbound WhatsApp messages are accepted and never answered:

```bash
.venv/bin/arq app.workers.settings.WorkerSettings
```

`seed_data.py` is idempotent (every row is looked up by natural key first), so re-running it is safe.

---

## Configuration

Every variable lives in `.env`; each one maps 1:1 to a field on `Settings` in [app/config.py](app/config.py), and `.env.example` documents all of them inline with their real defaults. The ones with no working default:

| Variable | Why it's required |
| --- | --- |
| `JWT_SECRET` | Signs access and refresh tokens. Generate with `python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `DATABASE_URL` | Must use the `postgresql+asyncpg://` driver — the sync driver will not work |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Web sign-in; without them only the WhatsApp channel works |
| `AGENT_ALLOWED_DOMAINS` | Empty means **nobody** can sign in as an agent, so the admin console is unreachable |
| `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN` | Sending and verifying Meta webhooks |
| `CASHFREE_APP_ID`, `CASHFREE_SECRET_KEY`, `CASHFREE_WEBHOOK_SECRET` | Payment links and settlement callbacks |
| `OPENAI_API_KEY` | Optional. Blank is a supported mode: the bot degrades to the scripted flow rather than failing |

Shipping rates, zones and serviceable pincodes are deliberately **not** environment variables — they are rows in `shipping_config` and `serviceable_pincodes`, editable from the admin API at runtime.

### Production is guarded at boot

With `ENVIRONMENT=production` the app refuses to start on a development
placeholder, reporting every problem at once rather than one per deploy attempt:

```
Refusing to start in production:
  - JWT_SECRET is still the .env.example placeholder
  - CORS_ALLOWED_ORIGINS must not be '*' in production
  - WHATSAPP_APP_SECRET must be set (webhook signatures depend on it)
```

It checks `DEBUG`, `JWT_SECRET` (placeholder or under 32 chars), wildcard/empty
CORS, and the three webhook secrets. These all work in staging and are a breach
in production — exactly the combination that reaches deploy day unnoticed.
Development and the test suite are untouched.

### External setup, not in .env

Three things must be configured outside this repo before the app is usable:

1. **Google OAuth redirect URI** — `GOOGLE_REDIRECT_URI` must match an
   *Authorised redirect URI* on the Google credential exactly, including scheme,
   port and trailing slash.
2. **Meta webhook subscription** — callback URL and verify token are entered in
   the Meta App Dashboard, and the app must be reachable over public HTTPS.
   See [docs/OPERATIONS.md](docs/OPERATIONS.md#32-webhook-subscription).
3. **Cashfree webhook endpoint** — registered in the Cashfree dashboard pointing
   at `/webhook/cashfree`.

Locally, use a tunnel (`ngrok http 8000`) for 2 and 3 — both providers require a
public HTTPS URL and will not call `localhost`.

### Invoice fonts

The rupee sign (U+20B9) needs a Unicode TTF. The Docker image installs
`fonts-dejavu-core` for this. Running natively on macOS, the bundled Arial
Unicode predates U+20B9, so invoices correctly fall back to the `Rs.` spelling —
equally valid on a GST invoice, but it means local PDFs differ from production
ones. Install DejaVu to `/Library/Fonts/` if you need them identical.

---

## How it fits together

```
app/
  main.py            app factory, middleware stack, error handlers
  config.py          Settings (pydantic-settings)
  database.py        async engine, SessionLocal, session_scope
  auth.py            JWT issue/verify, refresh rotation + reuse detection
  security.py        signature verification, hashing, PII redaction helpers
  dependencies.py    CurrentUser / CurrentAgent / CurrentAdmin, DbSession
  errors.py          domain exceptions -> HTTP, no HTTPException in services
  middleware.py      correlation id, request logging, body limits, headers
  rate_limit.py      slowapi limiter (see deviations below)
  cache.py           Redis read-through caching with TTLs
  pagination.py      cursor (keyset) pagination
  models/            SQLAlchemy 2.0 models, one module per aggregate
  schemas/           Pydantic request/response models
  routers/           auth, store, admin, webhook, health
  services/          all business logic
  workers/           arq task definitions + WorkerSettings
alembic/             migrations (async env.py)
tests/               137 tests, SQLite-backed
```

**Layering rule:** routers do HTTP, services do business logic, models do persistence. Services raise domain exceptions from `app/errors.py` and never import FastAPI — which is what lets the test suite call them directly, without a request.

### Order lifecycle

```
pending_payment → pending_confirmation → confirmed → processing → shipped → delivered
                                                                         ↘ return_requested
      ↘ cancelled (until shipped)
```

Payment does **not** confirm an order. It moves it to `pending_confirmation`, where a human agent accepts it and attaches a delivery ETA. No step in the chain may be skipped.

### Inventory

Two layers, deliberately. `variant.stock` is what physically exists; `inventory_reservations` are holds taken at checkout under `SELECT ... FOR UPDATE`. Available stock is `stock` minus live holds. A hold becomes a real decrement only when payment settles, and expires on its own (`RESERVATION_TTL_MINUTES`) if it doesn't — so an abandoned checkout can never strand stock permanently.

### Money

Prices are **GST-inclusive**, matching Indian retail practice. `app/services/tax.py` extracts tax out of the gross (`taxable = gross × 100 / (100 + rate)`, ROUND_HALF_UP) rather than adding it on top. Intra-state sales split into CGST+SGST, inter-state into IGST, decided by comparing `SELLER_STATE_CODE` against the shipping address.

Order items are **frozen snapshots**. Renaming or repricing a product never rewrites history.

---

## Security notes

These are properties the code actively enforces, not aspirations:

- **Webhook amount verification.** The amount in a Cashfree callback is attacker-visible input. It is compared against our own recorded total and never trusted; any mismatch in *either* direction flags the payment and leaves the order unpaid.
- **No card data.** Payment details never reach this server — Cashfree hosted checkout only.
- **Webhook signatures.** Meta `X-Hub-Signature-256` (HMAC-SHA256, `WHATSAPP_APP_SECRET`) and Cashfree signatures are verified on every request. Optional IP allowlisting via `WEBHOOK_IP_WHITELIST_ENABLED`, off by default for local development.
- **Webhook idempotency.** Every event is recorded by `event_id` + `payload_hash`; replays return 200 without reprocessing. Cashfree retries, and settling twice must not double-apply anything.
- **Refresh token reuse detection.** Tokens are stored as bcrypt hashes (with a deterministic SHA-256 `lookup_hash` for indexing) and rotated on every use. Presenting an already-rotated token revokes the **entire family** — and does so in its own transaction, because the request session is about to roll back on the 401.
- **PII redaction.** A structlog processor masks phone numbers, emails and payment IDs in all log output.
- **Validation errors are scrubbed.** Pydantic's `ctx` and `input` fields are stripped from 422 responses; they can echo a submitted secret back to the caller. Unhandled exceptions return a generic message for the same reason — an exception string can carry a connection URI.
- **Order IDs are not probeable.** Fetching someone else's order returns 404, not 403.
- **Coupon usage is counted, not cached.** Redemption limits are enforced with `COUNT(*)` over `coupon_usage` with the coupon row locked, never a mutable `used_count` column that two concurrent checkouts could race.
- **Soft deletes throughout.** Products, categories, coupons, reviews and merged users are never hard-deleted.

---

## Testing

```bash
.venv/bin/python -m pytest           # 137 tests, ~5s
.venv/bin/python -m pytest tests/test_payments.py -q
```

No PostgreSQL or Redis needed. The suite runs on SQLite via aiosqlite, with a fake Redis and a captured arq queue (see [tests/conftest.py](tests/conftest.py)). JSON columns are declared as `JSON().with_variant(JSONB, "postgresql")` specifically so this works.

Coverage is organised by risk rather than by module:

| File | What it pins down |
| --- | --- |
| `test_payments.py` | Callback amounts are never trusted; replays are ignored |
| `test_checkout.py` | Stock reservation, coupon atomicity, GST arithmetic |
| `test_orders.py` | Status chain, no skipped steps, stock released on cancel |
| `test_auth.py` | Token rotation, reuse detection, agent domain restriction |
| `test_ai_chat.py` | The shopper always gets a reply; the model can never settle a payment |
| `test_account_merge.py` | Idempotent, non-destructive, frees unique identifiers |
| `test_webhooks.py` | Signature rejection and idempotent delivery |
| `test_cart.py`, `test_store.py` | Cart rules, and the HTTP surface incl. response-model validation |
| `test_health.py` | Liveness vs readiness |

**One caveat worth knowing.** Tests do not run inside a rolled-back transaction; each one truncates tables afterwards instead. Routers legitimately call `db.commit()`, and SQLite defers its `BEGIN` in a way that lets those commits escape an enclosing transaction even in savepoint mode — verified, not assumed. Truncation is less elegant but actually isolates, and it keeps commit semantics identical to production.

---

## Operations

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Liveness. Always cheap, never touches a dependency |
| `GET /health/ready` | Readiness. Probes PostgreSQL and Redis; this is what surfaces a degraded dependency |
| `GET /metrics` | Prometheus |

Logs are JSON in production (`ENVIRONMENT=production`), human-readable otherwise. Every request carries a correlation ID through to every log line it produces.

Scheduled work runs on the arq worker: reservation cleanup every 5 min, agent SLA checks every 5 min, payment expiry every 10 min, pending-webhook sweep every 10 min, cart expiry every 15 min, abandoned-cart nudges 4× daily.

### Migrations

```bash
.venv/bin/alembic revision --autogenerate -m "add whatever"
.venv/bin/alembic upgrade head
.venv/bin/alembic downgrade -1
```

---

## Deviations from the specification

Documented rather than silently absorbed:

**Two modules exist that the specified file tree does not name.**

- `app/services/tax.py` — GST splitting was going to be duplicated across checkout, order and invoice generation. It is arithmetic with real correctness stakes and exactly one right answer, so it lives in one tested place.
- `app/rate_limit.py` — the limiter must be constructed once and shared by `main.py` and three routers. Defining it in `main.py` would have made every router import the app module and close an import cycle.

**The rate limiter fails open.** If Redis is unreachable, slowapi is configured with `swallow_errors=True` and requests proceed unthrottled rather than 500ing. Losing rate limiting is a degradation; 500ing checkout is an outage. `/health/ready` is what reports the underlying problem.

**Three router modules deliberately omit `from __future__ import annotations`** (`auth.py`, `store.py`, `admin.py`), each with a comment saying why. slowapi's decorator wraps handlers via `functools.wraps`, which keeps slowapi's `__globals__` — so FastAPI cannot resolve string annotations and silently reinterprets request bodies as query parameters. This is a real incompatibility, not a style choice; the import will look like an oversight to a future reader, hence the comments.

### Known gaps

Left as-is, but you should know about them:

- `_normalize_google_profile` drops every phone claim from the Google payload, which makes the phone-matching branch of `link_or_merge_on_google_login` unreachable in practice. Merging still works through the explicit `POST /api/admin/users/merge` path.
- `CheckoutRequest.notes` is accepted by the schema and then dropped — `order.checkout()` takes no `notes` parameter.
- `GET /api/admin/dashboard/orders-chart` uses `date_trunc`, so it is PostgreSQL-only and is the one endpoint not exercised by the SQLite test suite.
- Admin coupon listing, review listing and user search have no service layer; those queries live in the router.

Out of scope per the specification: multi-language, multi-currency, loyalty points, advanced analytics, recommendations, a WebSocket dashboard, push notifications, delivery-partner integration, and terms/privacy pages.
