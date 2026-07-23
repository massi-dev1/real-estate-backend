"""Billing provider resolution (§8.16).

The app is configured with one active provider (``settings.billing_provider``).
Only the sandbox ``stub`` provider ships here; a real Stripe/Chargily adapter
registers under its own key and is selected by config with no call-site change.
"""

from app.core.config import Settings
from app.integrations.billing.base import BillingProvider
from app.integrations.billing.stub import PROVIDER_KEY as STUB_KEY
from app.integrations.billing.stub import StubBillingProvider


def build_billing_provider(settings: Settings) -> BillingProvider:
    """The configured provider. An unknown key falls back to the stub (the safe
    non-charging default) rather than crashing startup."""
    if settings.billing_provider == STUB_KEY:
        return StubBillingProvider(settings.billing_webhook_secret)
    # A real provider adapter would be constructed here by key. Until one exists
    # with credentials, everything routes through the sandbox stub.
    return StubBillingProvider(settings.billing_webhook_secret)
