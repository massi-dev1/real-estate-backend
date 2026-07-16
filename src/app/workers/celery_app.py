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
}
