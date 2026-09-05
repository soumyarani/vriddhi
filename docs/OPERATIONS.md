# Operations Guide

Day-to-day management of the three parts that need hands on them: the
**database**, the **chatbot**, and the **Meta/WhatsApp configuration**.

For architecture and local setup see [../README.md](../README.md). For the HTTP
surface see [openapi.json](openapi.json) or run the app and open `/docs`.

- [1. Database](#1-database)
- [2. Chatbot](#2-chatbot)
- [3. Meta / WhatsApp configuration](#3-meta--whatsapp-configuration)
- [4. Troubleshooting](#4-troubleshooting)

Commands assume the project venv. Use `.venv/bin/python`, not bare `python`.

---

## 1. Database

PostgreSQL 16, 28 tables, accessed asynchronously through SQLAlchemy 2.0 with
asyncpg. Models live in `app/models/`, one module per aggregate.

### 1.1 Schema map

| Area | Tables |
| --- | --- |
| Identity | `users`, `agents`, `refresh_tokens` |
| Catalogue | `categories`, `products`, `product_variants`, `product_images` |
| Shopping | `carts`, `cart_items`, `wishlists`, `inventory_reservations` |
| Orders | `orders`, `order_items`, `payments`, `refunds` |
| Promotions | `coupons`, `coupon_usage` |
| Conversations | `conversations`, `messages`, `ai_token_usage` |
| Fulfilment | `addresses`, `shipping_config`, `serviceable_pincodes` |
| Reviews | `reviews` |
| Platform | `webhook_events`, `notifications`, `email_log`, `audit_log` |

### 1.2 Migrations

Alembic with an async `env.py`. **Never** edit a migration that has run anywhere
other than your own machine — add a new one.

```bash
.venv/bin/alembic revision --autogenerate -m "add gift_message to orders"
.venv/bin/alembic upgrade head
.venv/bin/alembic downgrade -1          # step back one
.venv/bin/alembic current               # what is applied
.venv/bin/alembic history --verbose
```

**Always read what `--autogenerate` produced before running it.** It is good at
new tables and columns and unreliable at everything else: it does not see server
defaults, `CHECK` constraints, enum value changes, or index renames, and it will
happily emit a `DROP` for anything it fails to reflect.

For a column that must be `NOT NULL` on a table with existing rows, do it in
three deployable steps rather than one: add it nullable, backfill, then add the
constraint. A single-step migration takes an `ACCESS EXCLUSIVE` lock for the
length of the backfill and will stall every request behind it.

### 1.3 Seeding

```bash
.venv/bin/python seed_data.py
```

Loads a demo catalogue, coupons (including one deliberately expired, so the
rejection path is demoable), shipping zones, serviceable pincodes and one admin
agent. Idempotent — every row is looked up by natural key first, so re-running
neither duplicates nor crashes. Safe against a populated dev database; do not
run it against production.

### 1.4 Backup and restore

```bash
# Backup (custom format, compressed, restorable selectively)
pg_dump -Fc -d "$DATABASE_URL" -f backup-$(date +%F).dump

# Restore into an empty database
pg_restore -d "$DATABASE_URL" --clean --if-exists backup-2026-08-28.dump

# Single table, for a targeted recovery
pg_restore -d "$DATABASE_URL" --data-only --table=orders backup.dump
```

`DATABASE_URL` in `.env` uses the `postgresql+asyncpg://` scheme, which the
`pg_*` tools do not understand. Strip `+asyncpg` when passing it to them.

Verify a backup by restoring it somewhere and running
`.venv/bin/alembic current` — a dump that restores but reports the wrong
revision will fail confusingly on the next deploy.

### 1.5 Invariants not to break by hand

Direct `UPDATE`s bypass the service layer, which is where these are enforced.

- **Never hard-delete** products, categories, coupons, reviews or users. Set
  `deleted_at`. Orders reference catalogue rows for their history, and a real
  `DELETE` either fails on a foreign key or orphans an order.
- **`variant.stock` is physical stock, not available stock.** Available is
  `stock` minus live rows in `inventory_reservations`. Setting `stock` directly
  during an active checkout window will oversell. Restock through the admin API.
- **Never add a `used_count` column to `coupons`.** Redemption limits are
  enforced by `COUNT(*)` over `coupon_usage` with the coupon row locked. A
  counter column is a lost-update race between concurrent checkouts.
- **Order items are frozen snapshots.** Do not "fix" a historic `order_items`
  row to match a renamed or repriced product. That is the point of the table.
- **Order status follows a chain** — `pending_payment → pending_confirmation →
  confirmed → processing → shipped → delivered`. Do not jump a status by hand;
  side effects (stock commit, notifications) hang off the transitions.

### 1.6 Routine queries

```sql
-- Orders waiting on a human, oldest first
SELECT order_number, created_at, total FROM orders
WHERE status = 'pending_confirmation' ORDER BY created_at;

-- Payments flagged for review (callback amount disagreed with our total)
SELECT p.id, p.amount, o.order_number, o.total
FROM payments p JOIN orders o ON o.id = p.order_id
WHERE p.status = 'flagged';

-- Stock actually available to sell.
-- A hold counts only while it is live: released_at means it was given back, and
-- committed_at means it was already subtracted from v.stock at payment, so
-- counting either would double-subtract.
SELECT v.sku, v.stock,
       v.stock - COALESCE(SUM(r.quantity) FILTER (
           WHERE r.released_at IS NULL
             AND r.committed_at IS NULL
             AND r.expires_at > now()
       ), 0) AS available
FROM product_variants v
LEFT JOIN inventory_reservations r ON r.variant_id = v.id
GROUP BY v.id, v.sku, v.stock HAVING v.stock > 0 ORDER BY available;

-- Webhook events that never completed
SELECT event_id, source, event_type, status, attempts, error, created_at
FROM webhook_events
WHERE processed_at IS NULL AND created_at < now() - interval '15 minutes';
```

### 1.7 Connection pool

`DB_POOL_MIN=5`, `DB_POOL_MAX=20` per process. Total connections is
`DB_POOL_MAX × (API replicas + workers)` — check it against the server's
`max_connections` before scaling out. Raise `DB_POOL_RECYCLE_SECONDS` only if you
are sure nothing between the app and PostgreSQL closes idle sockets.

---

## 2. Chatbot

Implemented in `app/services/ai_chat.py`. OpenAI is asked to reply with a single
JSON object; the app parses it, executes any actions, and sends the result over
WhatsApp.

### 2.1 Message flow

```
WhatsApp → POST /webhook  (signature verified, deduplicated, ACKed immediately)
         → arq queue
         → build_context_blocks()   assemble catalogue, cart, orders, history
         → _fit_to_budget()         trim to the token budget
         → _call_model()            OpenAI, or skip if the circuit is open
         → _parse_response()        tolerant JSON extraction
         → execute_actions()        cart/order mutations
         → whatsapp.send_*()        text, buttons, list, or product message
```

The webhook answers Meta in milliseconds and does the real work on the queue,
because Meta retries anything slow. **With no arq worker running, messages are
accepted and never answered.**

### 2.2 Configuration

| Variable | Default | Effect |
| --- | --- | --- |
| `OPENAI_API_KEY` | — | Blank is supported: the bot runs the scripted fallback |
| `OPENAI_MODEL` | `gpt-4o-mini` | Any chat-completions model |
| `OPENAI_TIMEOUT_SECONDS` | `25.0` | Then fall back. Keep below Meta's retry window |
| `AI_MAX_CONTEXT_TOKENS` | `3000` | Prompt budget; raise costs money, lower loses context |
| `AI_SUMMARY_TOKEN_BUDGET` | `200` | Size of the rolling conversation summary |
| `AI_CIRCUIT_BREAKER_THRESHOLD` | `3` | Consecutive failures before the breaker opens |
| `AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS` | `60` | How long it stays open |

In-code constants (`app/services/ai_chat.py`): `MAX_CATALOG_PRODUCTS = 40`
products injected per prompt, `SUMMARY_TRIGGER_TURNS = 6` turns before
summarising, `CHARS_PER_TOKEN = 4` for the estimator.

### 2.3 Editing the prompt

`SYSTEM_PROMPT` at `app/services/ai_chat.py:53`. It pins the JSON response shape,
so if you change the shape you must also change `AIResponse` in
`app/schemas/conversation.py` and `_parse_response()`.

Rules in there that exist for a reason — removing them causes real incidents:

- *"Only reference product_id and variant_id values that appear in the catalogue"* —
  otherwise the model invents IDs and actions fail on lookup.
- *"Never quote a price that is not in the catalogue"* — hallucinated prices are a
  consumer-protection problem, not a UX one.
- *"Never promise a delivery date, a refund, or a discount"* — the model has no
  authority to commit the business to any of those.
- *"Reply ONLY with a single JSON object"* — the parser is tolerant of fences and
  surrounding prose, but tolerance is a fallback, not a licence.

After editing, run `.venv/bin/python -m pytest tests/test_ai_chat.py -q`. Those
tests pin the parser's tolerance and the guarantee that the model cannot settle a
payment.

### 2.4 Intents and actions

Twelve intents: `browse, search, add_to_cart, remove_from_cart, view_cart,
checkout, track_order, order_history, cancel_order, return_order, help,
escalate`.

An unrecognised intent degrades to `help` rather than raising; an unknown action
name is a no-op. Both are deliberate — a hallucinated field should never break
the reply path.

**The model cannot move money or stock.** A `checkout` action creates an order
and returns a payment link. The order stays `pending_payment` until Cashfree's
signed webhook settles it, and a human confirms it after that. There is no code
path from a model output to a paid order.

### 2.5 Fallback and the circuit breaker

Three consecutive failures open the breaker for 60 seconds, during which OpenAI
is not called at all. Falling back is not the same as failing: `_fallback()`
serves these intents from the database using keyword classification —

`browse`, `view_cart`, `track_order`, `order_history`, `help`
(`FALLBACK_SERVICEABLE_INTENTS` in `app/models/enums.py`).

Anything else — a return request, a complaint, an ambiguous message — escalates
to a human agent. That split is a judgement call encoded on purpose: lookups can
be answered from data, but a return needs someone to weigh it, and a canned
answer would be worse than a queue.

Check the breaker:

```bash
redis-cli GET circuit:openai            # set while open
redis-cli TTL circuit:openai            # seconds until it closes
redis-cli GET circuit:openai:failures   # consecutive failure count
```

To force the breaker shut after fixing an upstream problem, rather than waiting
out the cooldown: `redis-cli DEL circuit:openai circuit:openai:failures`.

### 2.6 Token usage and cost

Every call writes a row to `ai_token_usage`: `model`, `input_tokens`,
`output_tokens`, `latency_ms` and `fallback_used`.

The table stores **token counts, not currency** — there is no cost column,
deliberately, because per-token prices change and a number baked in at write time
would quietly go stale. Multiply by your current rate card at query time:

```sql
SELECT date_trunc('day', created_at) AS day, model,
       SUM(input_tokens)  AS input_tokens,
       SUM(output_tokens) AS output_tokens,
       COUNT(*) FILTER (WHERE fallback_used) AS fallbacks,
       ROUND(AVG(latency_ms)) AS avg_ms
FROM ai_token_usage
GROUP BY 1, 2 ORDER BY 1 DESC LIMIT 14;
```

A rising `fallbacks` count is the number worth alerting on: it means customers
are getting scripted replies instead of the assistant.

If cost climbs: lower `MAX_CATALOG_PRODUCTS` (usually the largest block), lower
`AI_MAX_CONTEXT_TOKENS`, or summarise more aggressively by lowering
`SUMMARY_TRIGGER_TURNS`. The budget trims the catalogue first and the cart and
summary last, so squeezing it degrades product breadth before it degrades the
bot's grasp of what the customer is doing.

### 2.7 Handoff to humans

Escalated conversations land in the agent queue at
`GET /api/admin/conversations`. Agents reply through
`POST /api/admin/conversations/{id}/reply`, which suppresses AI replies for that
conversation until `.../return-to-ai`. `AGENT_SLA_MINUTES` (default 15) governs
when an unanswered escalation is flagged; a cron job checks every 5 minutes.

---

## 3. Meta / WhatsApp configuration

Setup at [developers.facebook.com](https://developers.facebook.com) → your App →
WhatsApp, plus Commerce Manager for the catalogue.

### 3.1 Credentials

| Variable | Where to find it |
| --- | --- |
| `WHATSAPP_TOKEN` | System User permanent token (App Dashboard → Business Settings → System Users). **Not** the 24-hour test token |
| `WHATSAPP_PHONE_NUMBER_ID` | WhatsApp → API Setup. The numeric **ID**, not the phone number |
| `WHATSAPP_BUSINESS_ID` | WABA ID, same page |
| `WHATSAPP_APP_SECRET` | Settings → Basic → App Secret. Verifies inbound signatures |
| `WHATSAPP_VERIFY_TOKEN` | Any string you invent; must match what you paste into Meta |
| `WHATSAPP_CATALOG_ID` | Commerce Manager → Catalogue → Settings |
| `META_GRAPH_VERSION` | Pinned to `v21.0`. Bump deliberately, not casually |

Use a permanent System User token. A temporary token will work in testing and
then expire in production, and the failure looks like an outage.

### 3.2 Webhook subscription

The app must be publicly reachable over HTTPS. For local development, tunnel:

```bash
ngrok http 8000
```

In App Dashboard → WhatsApp → Configuration → Webhook:

- **Callback URL**: `https://<your-host>/webhook`
- **Verify token**: exactly your `WHATSAPP_VERIFY_TOKEN`
- **Subscribe to fields**: `messages` (required), `message_template_status_update`

Meta immediately sends `GET /webhook` with `hub.mode`, `hub.verify_token` and
`hub.challenge`. On a token match the app echoes the challenge back as
`text/plain`; on a mismatch verification fails in the Meta UI. If it fails, the
verify token differs — check for a trailing space or newline in `.env`.

Every subsequent `POST /webhook` must carry a valid `X-Hub-Signature-256`
(HMAC-SHA256 of the raw body with the app secret). Bad signature → **401**, and
the body is never parsed. The HMAC is computed over the *raw* bytes, so anything
that rewrites the body in transit will break it.

Optionally restrict by source IP with `WEBHOOK_IP_WHITELIST_ENABLED=true` plus
`META_WEBHOOK_IPS`. Off by default so local development works.

### 3.3 Catalogue sync

Products are pushed to Commerce Manager so they can be sent as rich product
messages.

```
POST /api/admin/catalog/sync/{product_id}    # one product
POST /api/admin/catalog/sync                 # everything pending
```

Both **enqueue** an arq job rather than calling Meta inline — a batch of Graph
calls has no business blocking an admin request. A 202 means queued, not synced;
confirm in Commerce Manager or via the product's sync status.

Retailer IDs are derived, never stored by hand:

```
prod_{product_id}                      # product with no variants
prod_{product_id}_v{variant_id}        # one entry per variant
```

Sync posts to `{catalog_id}/items_batch`. Prices convert to **paise**
(`₹4999.00 → 499900`). Meta can accept the batch and still reject individual
items; the app surfaces per-item errors and marks the product failed with a
reason. Common causes: image URL not publicly reachable, price ≤ 0, missing
description.

### 3.4 Message-type limits

Enforced in `app/services/whatsapp.py`. Exceeding a limit is a Graph API error,
so the app truncates rather than fails — check these when a message looks cut off.

| Type | Limit |
| --- | --- |
| Text body | 4096 chars |
| Reply buttons | 3 buttons, 20 chars each |
| Interactive list | 10 rows; title 24 chars, description 72 |
| Multi-product message | 30 products |
| Header | 60 chars |
| Image caption | 1024 chars |

### 3.5 The 24-hour window

Outside 24 hours from the customer's last message, only **pre-approved template
messages** may be sent. Free-form text is rejected by Meta.

This shapes order notifications: a confirmation minutes after checkout is inside
the window, but "your order shipped" three days later is not and needs an
approved template. Templates are created in Business Manager and take hours to
days to approve — get them approved before launch, not during it.

### 3.6 Going live

- [ ] Business verification complete
- [ ] Phone number registered and display name approved
- [ ] Permanent System User token in `.env` (not a test token)
- [ ] Webhook verified over HTTPS on the production host
- [ ] Templates submitted and approved for post-window notifications
- [ ] Catalogue synced and visible in Commerce Manager
- [ ] Messaging tier understood (new numbers start capped at 1K unique
      recipients/day and scale with quality)

---

## 4. Troubleshooting

**Webhook verification fails in the Meta UI.**
`WHATSAPP_VERIFY_TOKEN` mismatch, or the URL is not publicly reachable over
HTTPS. Confirm with `curl "https://host/webhook?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=test"` —
it should return `test`.

**Messages arrive but nothing is answered.**
Almost always no arq worker. Check the process, then the queue depth with
`redis-cli ZCARD arq:queue` (arq's queue is a sorted set, so `LLEN` returns a
WRONGTYPE error, not a length). Look for rows in `webhook_events` with
`processed_at IS NULL`; the sweeper retries them every 10 minutes.

**All webhooks return 401.**
`WHATSAPP_APP_SECRET` is wrong, or a proxy is rewriting the request body. The
signature is over raw bytes.

**Bot replies but ignores the catalogue.**
Products are not synced, or the catalogue block was trimmed out of the budget.
Check `ai_token_usage` for prompt size and lower `MAX_CATALOG_PRODUCTS`.

**Bot is generic and never uses cart or order data.**
The breaker is probably open — it is serving scripted fallbacks. Check
`redis-cli TTL ai:circuit:open` and the logs for `ai_call_failed`.

**Payments settle but orders stay unpaid.**
Look for `status = 'flagged'` in `payments`: the callback amount disagreed with
our recorded total. This is the anti-tamper check working. Investigate the
mismatch; do not mark the order paid by hand.

**Orders stuck in `pending_confirmation`.**
Working as designed — payment does not confirm an order. A human confirms it via
`POST /api/admin/orders/{id}/confirm`.

**Stock looks wrong.**
Compare `variant.stock` against live `inventory_reservations`. Expired holds are
cleaned every 5 minutes by the worker; if they are piling up, the worker is down.

**429s from our own API.**
Rate limits are per user when authenticated and per IP otherwise, so a shared NAT
should not throttle a whole office. If it does, the request is arriving
unauthenticated. Note the limiter **fails open** — if Redis is down, limits stop
applying rather than 500ing; `/health/ready` reports the real problem.
