"""Celery application (§12): one broker, four queues by workload profile.

``default`` carries emails/notifications, ``media`` runs the CPU-heavy image
pipeline (§8.2), ``sync`` for portal/geocoding work,
``analytics`` for rollups — a slow queue can never starve lead notifications
sitting in ``default``. Beat drives scheduled jobs from this same app.
"""

from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

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

celery_app.autodiscover_tasks(["app.workers.tasks"])

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
}
