"""structlog configuration: JSON logs in staging/production, pretty console locally.

Every log line automatically carries the contextvars bound by
``RequestContextMiddleware`` (request_id, method, path — later tenant_id and
user_id).
"""

import logging
import sys

import structlog

from app.core.config import Settings

# Keys that must never appear in logs (PII / secrets). Extended as modules land.
REDACTED_KEYS = frozenset(
    {"password", "password_hash", "token", "refresh_token", "access_token", "authorization"}
)


def _redact_processor(
    _logger: logging.Logger, _name: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    for key in event_dict.keys() & REDACTED_KEYS:
        event_dict[key] = "[redacted]"
    return event_dict


def configure_logging(settings: Settings) -> None:
    renderer: structlog.typing.Processor = (
        structlog.dev.ConsoleRenderer()
        if settings.is_local
        else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if settings.app_debug else logging.INFO
        ),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.DEBUG if settings.app_debug else logging.INFO,
        format="%(message)s",
    )
