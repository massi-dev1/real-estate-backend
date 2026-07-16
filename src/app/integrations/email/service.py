"""Transactional email — thin adapter interface (§5 integrations/).

Local/dev delivery goes to Mailpit over plain SMTP. Provider implementations
(Brevo/SES) slot in behind the same protocol later; from Part 5 on, sends move
into Celery tasks so a slow provider never blocks a request (>200ms rule).
"""

from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage
from typing import Protocol

import aiosmtplib
import structlog

from app.core.config import Settings

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class EmailMessage:
    to: str
    subject: str
    text: str


class EmailService(Protocol):
    async def send(self, message: EmailMessage) -> None: ...


class SmtpEmailService:
    def __init__(self, settings: Settings) -> None:
        self._host = settings.smtp_host
        self._port = settings.smtp_port
        self._from = settings.email_from

    async def send(self, message: EmailMessage) -> None:
        mime = MimeMessage()
        mime["From"] = self._from
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.text)
        await aiosmtplib.send(mime, hostname=self._host, port=self._port)
        logger.info("email_sent", subject=message.subject)
