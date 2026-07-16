"""Email delivery task (§12) — moved off the request path per Part 3/4's note
that a slow SMTP relay must never turn auth flows into a 500.

Args are primitives (to/subject/text), never objects, so the task survives a
broker restart without carrying live references (§12 rule: task args are IDs,
not objects).
"""

import structlog
from celery import shared_task

from app.core.config import get_settings
from app.integrations.email.service import EmailMessage, SmtpEmailService
from app.workers.db import run_sync

logger = structlog.get_logger(__name__)


@shared_task(
    name="app.workers.tasks.email.send_email",
    max_retries=3,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
)
def send_email(to: str, subject: str, text: str) -> None:
    service = SmtpEmailService(get_settings())
    run_sync(service.send(EmailMessage(to=to, subject=subject, text=text)))
    logger.info("email_task_sent", subject=subject)
