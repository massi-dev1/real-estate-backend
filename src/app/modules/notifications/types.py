"""Notification type registry (§8.12): per-type defaults + versioned templates.

Every ``NotificationType`` has one ``TypeDefinition`` here — the default channel
set (used when a user has no explicit preference row), whether it is
digest-eligible (batchable into a daily email rather than sent instantly), and
a per-locale template that renders the payload into ``(subject, body)``.

Templates are **versioned per type** (§8.12 point 3) and rendered with plain
``str.format`` on a whitelisted payload. MJML→HTML rendering is deferred: no
pure-Python MJML renderer is reachable without a Node toolchain, and pulling
Node in for one feature isn't worth it this part — v1 ships plain-text bodies
(the existing SMTP adapter is text-only anyway). The template *shape* (per
type, per locale, versioned) is what an MJML upgrade later slots into.

SMS/WhatsApp bodies reuse the same text — no separate adapter exists yet
(Parts 8/11 deferrals), so those channels are logged as ``skipped`` at send
time rather than pretending to deliver.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.core.i18n import DEFAULT_LOCALE, FALLBACK_CHAIN
from app.modules.notifications.models import NotificationChannel, NotificationType


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    subject: str
    body: str


@dataclass(frozen=True, slots=True)
class TypeDefinition:
    """Static config for one notification type."""

    version: int
    default_channels: frozenset[NotificationChannel]
    digest_eligible: bool
    # locale -> {"subject": "...", "body": "..."} with str.format placeholders.
    templates: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def render(self, payload: Mapping[str, Any], locale: str) -> RenderedMessage:
        """Resolve the locale (requested → fallback chain → default), then
        ``str.format`` the payload in. A missing/extra placeholder degrades to
        a safe empty string rather than raising — a notification must never be
        lost to a template typo."""
        template = self._template_for(locale)
        return RenderedMessage(
            subject=_safe_format(template.get("subject", ""), payload),
            body=_safe_format(template.get("body", ""), payload),
        )

    def _template_for(self, locale: str) -> Mapping[str, str]:
        for candidate in (locale, *FALLBACK_CHAIN, DEFAULT_LOCALE):
            if candidate in self.templates:
                return self.templates[candidate]
        # No template registered at all — an empty one renders to blank strings.
        return {}


class _SafeDict(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return ""


def _safe_format(template: str, payload: Mapping[str, Any]) -> str:
    try:
        return template.format_map(_SafeDict(payload))
    except (IndexError, ValueError):
        # Malformed template (stray brace / positional field) — never lose the
        # notification to it; fall back to the raw template text.
        return template


# ---- the registry ----

_IN_APP = NotificationChannel.IN_APP
_EMAIL = NotificationChannel.EMAIL

TYPE_DEFINITIONS: dict[NotificationType, TypeDefinition] = {
    NotificationType.LEAD_ASSIGNED: TypeDefinition(
        version=1,
        default_channels=frozenset({_IN_APP, _EMAIL}),
        digest_eligible=False,  # speed-to-lead — never delayed into a digest.
        templates={
            "en": {
                "subject": "New lead assigned to you",
                "body": (
                    "A new lead ({leadId}) has been assigned to you. "
                    "Respond quickly — speed to first contact wins deals."
                ),
            },
            "fr": {
                "subject": "Nouveau prospect qui vous est attribué",
                "body": (
                    "Un nouveau prospect ({leadId}) vous a été attribué. "
                    "Répondez rapidement — la vitesse de contact fait la différence."
                ),
            },
        },
    ),
    NotificationType.LEAD_ESCALATED: TypeDefinition(
        version=1,
        default_channels=frozenset({_IN_APP, _EMAIL}),
        digest_eligible=False,
        templates={
            "en": {
                "subject": "Unassigned lead needs attention",
                "body": (
                    "Lead {leadId} has been unassigned for over {minutes} minutes."
                ),
            },
            "fr": {
                "subject": "Un prospect non attribué requiert votre attention",
                "body": (
                    "Le prospect {leadId} est non attribué depuis plus de {minutes} minutes."
                ),
            },
        },
    ),
    NotificationType.APPOINTMENT_REMINDER: TypeDefinition(
        version=1,
        default_channels=frozenset({_IN_APP, _EMAIL}),
        digest_eligible=False,  # time-sensitive — must not be batched.
        templates={
            "en": {
                "subject": "Reminder: your property visit is {when}",
                "body": "This is a reminder for your property visit on {startAt}. See you there!",
            },
            "fr": {
                "subject": "Rappel : votre visite est {when}",
                "body": "Rappel de votre visite prévue le {startAt}. À bientôt !",
            },
        },
    ),
    NotificationType.APPOINTMENT_CONFIRMED: TypeDefinition(
        version=1,
        default_channels=frozenset({_IN_APP, _EMAIL}),
        digest_eligible=False,
        templates={
            "en": {
                "subject": "Your property visit is confirmed",
                "body": "Your visit on {startAt} has been confirmed.",
            },
            "fr": {
                "subject": "Votre visite est confirmée",
                "body": "Votre visite du {startAt} a été confirmée.",
            },
        },
    ),
    NotificationType.APPOINTMENT_CANCELLED: TypeDefinition(
        version=1,
        default_channels=frozenset({_IN_APP, _EMAIL}),
        digest_eligible=False,
        templates={
            "en": {
                "subject": "Your property visit was cancelled",
                "body": "Your visit on {startAt} has been cancelled.",
            },
            "fr": {
                "subject": "Votre visite a été annulée",
                "body": "Votre visite du {startAt} a été annulée.",
            },
        },
    ),
    NotificationType.MILESTONE_DUE: TypeDefinition(
        version=1,
        default_channels=frozenset({_IN_APP, _EMAIL}),
        digest_eligible=False,  # a due date is time-sensitive — not batched.
        templates={
            "en": {
                "subject": "Deal milestone due: {milestoneTitle}",
                "body": (
                    'The milestone "{milestoneTitle}" on deal "{dealTitle}" '
                    "is due on {dueDate}."
                ),
            },
            "fr": {
                "subject": "Étape à échéance : {milestoneTitle}",
                "body": (
                    'L\'étape "{milestoneTitle}" du dossier "{dealTitle}" '
                    "arrive à échéance le {dueDate}."
                ),
            },
        },
    ),
}


def definition_for(type_: NotificationType) -> TypeDefinition:
    return TYPE_DEFINITIONS[type_]
