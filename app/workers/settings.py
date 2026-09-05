"""arq worker entrypoint.

Run with:  arq app.workers.settings.WorkerSettings

Cron cadence is chosen by what each job protects:

* reservation cleanup — every 5 min. Held stock that nobody is paying for is
  stock we cannot sell, so this is the tightest loop.
* payment expiry — every 10 min, just behind the payment link TTL.
* cart expiry — every 15 min, per spec.
* SLA check — every 5 min, so a waiting customer is noticed quickly.
* abandoned carts — every 6 hours, per spec. Any more often is spam.
* catalogue sweep — nightly, when Meta's API is quietest.
* webhook sweep — every 10 min, to re-drive events whose enqueue was lost.
"""

from __future__ import annotations

from arq import cron

from app.config import settings
from app.database import dispose_engine
from app.redis import arq_redis_settings, close_redis
from app.workers.cart_tasks import (
    abandoned_cart_nudge,
    cleanup_reservations,
    expire_carts,
    expire_payments,
)
from app.workers.catalog_tasks import delete_product_task, sync_pending_task, sync_product_task
from app.workers.email_tasks import send_order_email_task
from app.workers.notification_tasks import (
    check_sla_breaches,
    notify_order_confirmed,
    notify_order_placed,
    notify_order_status,
)
from app.workers.webhook_processor import (
    process_message_status,
    process_payment_event,
    process_whatsapp_message,
    sweep_pending_events,
)
from logging_config import configure_logging, get_logger

log = get_logger(__name__)


async def startup(ctx: dict) -> None:
    configure_logging(settings.log_level, json_output=settings.is_production)
    log.info("worker_started", environment=settings.environment)


async def shutdown(ctx: dict) -> None:
    await dispose_engine()
    await close_redis()
    log.info("worker_stopped")


class WorkerSettings:
    redis_settings = arq_redis_settings()
    on_startup = startup
    on_shutdown = shutdown

    functions = [
        process_whatsapp_message,
        process_message_status,
        process_payment_event,
        sweep_pending_events,
        sync_product_task,
        delete_product_task,
        sync_pending_task,
        send_order_email_task,
        notify_order_placed,
        notify_order_confirmed,
        notify_order_status,
        cleanup_reservations,
        expire_carts,
        expire_payments,
        abandoned_cart_nudge,
        check_sla_breaches,
    ]

    cron_jobs = [
        cron(cleanup_reservations, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        cron(check_sla_breaches, minute={2, 7, 12, 17, 22, 27, 32, 37, 42, 47, 52, 57}),
        cron(expire_payments, minute={1, 11, 21, 31, 41, 51}),
        cron(sweep_pending_events, minute={4, 14, 24, 34, 44, 54}),
        cron(expire_carts, minute={3, 18, 33, 48}),
        cron(abandoned_cart_nudge, hour={0, 6, 12, 18}, minute=10),
        cron(sync_pending_task, hour=3, minute=30),
    ]

    # A stuck WhatsApp send must not wedge the queue behind it.
    max_jobs = 20
    job_timeout = 120
    max_tries = 3
    keep_result = 3600
