"""Celery application (§12): one broker, four queues by workload profile.

``default`` carries emails/notifications, ``media`` runs the CPU-heavy image
pipeline (§8.2), ``sync`` for portal/geocoding work,
``analytics`` for rollups — a slow queue can never starve lead notifications
sitting in ``default``. Beat drives scheduled jobs from this same app.
"""

import pkgutil
from typing import Any

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_process_init

from app.core.config import get_settings
from app.core.telemetry import init_sentry, instrument_celery
from app.workers import tasks as _task_package

settings = get_settings()

celery_app = Celery("real_estate_backend")
celery_app.conf.update(
    broker_url=settings.celery_broker_url,
    result_backend=settings.celery_result_backend,
    task_default_queue="default",
    task_queues={
        "default": {},
        "media": {},
        "sync": {},
        "analytics": {},
    },
    task_routes={
        "app.workers.tasks.email.*": {"queue": "default"},
        "app.workers.tasks.listings.*": {"queue": "analytics"},
        "app.workers.tasks.media.*": {"queue": "media"},
        # Agent photo processing is the same CPU profile as listing media.
        "app.workers.tasks.agents.*": {"queue": "media"},
        # Not `analytics`: the sweep sends latency-sensitive lead-notification
        # emails — the same class email.* already occupies. `analytics` is for
        # pure-batch work with no human-facing side effect.
        "app.workers.tasks.leads.*": {"queue": "default"},
        # Same reasoning: saved-search alerts/digests are human-facing email.
        "app.workers.tasks.favorites.*": {"queue": "default"},
        # Same reasoning: tour reminders are human-facing email.
        "app.workers.tasks.appointments.*": {"queue": "default"},
        # Batch, no human-facing latency — same class as flag_stale_listings.
        "app.workers.tasks.blog.*": {"queue": "analytics"},
        # Guide-stats sweep is batch/analytics; the report-PDF render is a
        # rendering job (media queue, same class as image processing). Routed
        # by explicit task name since one module holds both profiles.
        "app.workers.tasks.content.recompute_guide_stats": {"queue": "analytics"},
        "app.workers.tasks.content.generate_report_pdf": {"queue": "media"},
        # Notification delivery + digest emails are human-facing — same class as
        # email.* (default queue), never starved behind batch/analytics work.
        "app.workers.tasks.notifications.*": {"queue": "default"},
        # Milestone reminders notify a human (via notify() → email default) —
        # same class as tour reminders, never behind batch work.
        "app.workers.tasks.transactions.*": {"queue": "default"},
        # Portal syndication (§8.14): external I/O to third-party portals — the
        # `sync` queue exists specifically for this profile (portals/geocoding).
        "app.workers.tasks.syndication.*": {"queue": "sync"},
        # Analytics rollups/pruning/partition maintenance (§8.15): pure batch,
        # no human-facing latency — the queue this profile is named for.
        "app.workers.tasks.analytics.*": {"queue": "analytics"},
        # Tenant lifecycle & billing sweeps (§8.16): dunning/trial/purge/export
        # are batch back-office work — no human-facing latency.
        "app.workers.tasks.tenants.*": {"queue": "analytics"},
        # Compliance retention & DSR sweeps (§8.17): erasure purge + lost-lead
        # anonymization — batch back-office, same class as the retention jobs.
        "app.workers.tasks.compliance.*": {"queue": "analytics"},
        # Outbox relay (§12): drains durable domain events to their handlers. On
        # `default` — it drives the human-facing speed-to-lead notification, the
        # same class as email.*, never starved behind batch work.
        "app.workers.tasks.outbox.*": {"queue": "default"},
        # Outbound webhook delivery (§8.14): external I/O to tenant-registered
        # receivers — the same profile as portal syndication (`sync` queue).
        "app.workers.tasks.webhooks.*": {"queue": "sync"},
    },
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_track_started=True,
    # Every task is idempotent (§12): safe defaults for transient broker/worker
    # hiccups. Tasks still set their own max_retries where a tighter bound applies.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    timezone="UTC",
    enable_utc=True,
)

# `autodiscover_tasks(["app.workers.tasks"])` does NOT work here: it treats each
# entry as a *package* and looks for a `tasks` submodule inside it — i.e.
# `app.workers.tasks.tasks`, which does not exist — so the worker started with
# zero registered tasks and every `.delay()` would fail with NotRegistered.
# The task modules are siblings under one package, so enumerate and import them.
celery_app.conf.imports = tuple(
    f"app.workers.tasks.{module.name}"
    for module in pkgutil.iter_modules(_task_package.__path__)
    if not module.name.startswith("_")
)


@worker_process_init.connect
def _init_worker_telemetry(**_kwargs: Any) -> None:
    """Error tracking + tracing for the worker process (§14).

    Bound to ``worker_process_init`` rather than module import so it runs once
    per forked child (each prefork worker is its own process and needs its own
    Sentry client), and never in eager mode where tasks execute inline in the
    caller's already-initialised process. Both calls no-op when unconfigured.
    """
    init_sentry(settings, component="worker")
    instrument_celery(settings)


celery_app.conf.beat_schedule = {
    "flag-stale-listings": {
        "task": "app.workers.tasks.listings.flag_stale_listings",
        "schedule": crontab(hour=3, minute=0),
    },
    "sweep-lead-drips-and-escalations": {
        "task": "app.workers.tasks.leads.sweep_drips_and_escalations",
        "schedule": crontab(minute="*/15"),
    },
    # One daily tick covers both digest frequencies: daily rows every run,
    # weekly rows on Mondays (the task itself makes the split — §8.9).
    "send-saved-search-digests": {
        "task": "app.workers.tasks.favorites.send_saved_search_digests",
        "schedule": crontab(hour=7, minute=0),
    },
    # 15-minute grain keeps the 1-hour reminder timely; the sent-at stamps
    # make the tick idempotent.
    "send-tour-reminders": {
        "task": "app.workers.tasks.appointments.send_tour_reminders",
        "schedule": crontab(minute="*/15"),
    },
    # 5-minute grain so a post scheduled for e.g. 10:00 goes live close to
    # 10:00; the SCHEDULED-status filter makes each tick idempotent.
    "publish-scheduled-blog-posts": {
        "task": "app.workers.tasks.blog.publish_scheduled_posts",
        "schedule": crontab(minute="*/5"),
    },
    # Nightly neighborhood-guide stats recompute (§8.10) — same batch class
    # and cadence as flag_stale_listings.
    "recompute-guide-stats": {
        "task": "app.workers.tasks.content.recompute_guide_stats",
        "schedule": crontab(hour=3, minute=30),
    },
    # Batch quiet-hours-parked notifications into one email per user. 30-minute
    # grain keeps a digest reasonably timely once quiet hours end; the per-item
    # sent_at stamp makes each tick idempotent (§8.12).
    "send-notification-digests": {
        "task": "app.workers.tasks.notifications.send_notification_digests",
        "schedule": crontab(minute="*/30"),
    },
    # Deal-milestone due/overdue reminders (§8.13). Hourly is granular enough for
    # a date-based due signal; the per-milestone reminder_sent_at stamp makes
    # each tick idempotent.
    "send-milestone-reminders": {
        "task": "app.workers.tasks.transactions.send_milestone_reminders",
        "schedule": crontab(minute=0),
    },
    # Nightly analytics rollup (§8.15) — same batch class/cadence as the other
    # nightly jobs. Runs after midnight so "yesterday" is fully closed; the
    # today pass keeps dashboards near-current on the next morning's run.
    "rollup-analytics": {
        "task": "app.workers.tasks.analytics.rollup_analytics",
        "schedule": crontab(hour=2, minute=0),
    },
    # Prune raw events past the 90-day window by dropping whole month partitions
    # (§8.15). Monthly is granular enough — a partition only becomes droppable
    # once its whole month clears the cutoff.
    "prune-analytics-events": {
        "task": "app.workers.tasks.analytics.prune_analytics_events",
        "schedule": crontab(day_of_month=1, hour=4, minute=0),
    },
    # Create next months' partitions ahead of time so an insert never fails for
    # want of a partition. Daily is cheap (idempotent CREATE IF NOT EXISTS) and
    # guarantees the next-month partition exists well before month-end.
    "ensure-analytics-partitions": {
        "task": "app.workers.tasks.analytics.ensure_analytics_partitions",
        "schedule": crontab(hour=4, minute=30),
    },
    # Tenant lifecycle & billing sweeps (§8.16). Hourly dunning/trial checks keep
    # suspension timely; the purge and domain re-check run daily. All idempotent
    # (status filters / deleted-at guard).
    "billing-dunning-sweep": {
        "task": "app.workers.tasks.tenants.run_dunning_sweep",
        "schedule": crontab(minute=0),
    },
    "expire-tenant-trials": {
        "task": "app.workers.tasks.tenants.expire_trials",
        "schedule": crontab(minute=15),
    },
    "purge-scheduled-tenants": {
        "task": "app.workers.tasks.tenants.purge_scheduled_tenants",
        "schedule": crontab(hour=5, minute=0),
    },
    "verify-pending-domains": {
        "task": "app.workers.tasks.tenants.verify_pending_domains",
        "schedule": crontab(hour=5, minute=30),
    },
    # Compliance sweeps (§8.17). The erasure purge runs daily (a 30-day grace
    # window means day-grain is timely enough); the 24-month lost-lead
    # anonymization runs weekly (a slow-moving retention horizon). Both
    # idempotent (status / already-anonymized guards). The 90-day raw-analytics
    # prune already lives under `prune-analytics-events` (§8.15) — not
    # duplicated here.
    "purge-due-erasures": {
        "task": "app.workers.tasks.compliance.purge_due_erasures",
        "schedule": crontab(hour=6, minute=0),
    },
    "anonymize-stale-lost-leads": {
        "task": "app.workers.tasks.compliance.anonymize_stale_lost_leads",
        "schedule": crontab(day_of_week=1, hour=6, minute=30),
    },
    # Transactional-outbox relay (§12): drains durable domain events (speed-to-
    # lead, webhook fan-out) to their handlers with at-least-once + backoff.
    # Every minute — the outbox is the reliability floor under a broker hiccup,
    # so it must reconcile quickly; each event's status filter makes the tick
    # idempotent (a delivered row is never re-drained).
    "relay-outbox": {
        "task": "app.workers.tasks.outbox.relay_outbox",
        "schedule": crontab(minute="*"),
    },
}
