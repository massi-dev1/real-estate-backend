"""AI provider resolution (§8.18).

The app is configured with one active provider (``settings.ai_provider``). The
offline ``stub`` is the default; the Anthropic adapter is selected by config and
requires an API key. An unknown key falls back to the stub (the safe offline
default) rather than crashing startup.
"""

from app.core.config import Settings
from app.integrations.ai.anthropic import PROVIDER_KEY as ANTHROPIC_KEY
from app.integrations.ai.anthropic import AnthropicTextProvider
from app.integrations.ai.base import AITextProvider
from app.integrations.ai.stub import StubTextProvider


def build_ai_text_provider(settings: Settings) -> AITextProvider:
    """The configured AI text provider. Anthropic is used only when it is both
    selected *and* has an API key; otherwise the offline stub keeps the feature
    working (and testable) without credentials."""
    if settings.ai_provider == ANTHROPIC_KEY and settings.ai_api_key:
        return AnthropicTextProvider(
            api_key=settings.ai_api_key,
            model=settings.ai_model,
            timeout_seconds=settings.ai_timeout_seconds,
        )
    # A real provider adapter registers under its own key here. Until one is
    # configured with credentials, everything routes through the offline stub.
    return StubTextProvider()
