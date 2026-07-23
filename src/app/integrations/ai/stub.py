"""Offline AI text provider (§8.18) — a clearly-labelled stub.

No live model credentials exist in this environment, so this stands in for a
real provider. It is fully deterministic: it echoes the structured ``prompt``
into a templated sentence, so the whole request → draft → agent-edits-and-saves
flow is exercised and tested end to end without a network call. Swapping in the
real :class:`~app.integrations.ai.anthropic.AnthropicTextProvider` is a config
change; no call site in ``modules/listings`` changes.
"""

from __future__ import annotations

from app.integrations.ai.base import (
    TextGenerationRequest,
    TextGenerationResult,
)

PROVIDER_KEY = "stub"
_STUB_MODEL = "stub-echo"


class StubTextProvider:
    """A deterministic offline
    :class:`~app.integrations.ai.base.AITextProvider` implementation."""

    @property
    def key(self) -> str:
        return PROVIDER_KEY

    async def generate_text(self, request: TextGenerationRequest) -> TextGenerationResult:
        # Deterministic templated output built only from the structured prompt —
        # no network, no non-determinism, so tests assert exact content.
        summary = request.prompt.strip()
        text = (
            f"{summary}\n\n"
            "This is a preview draft generated offline (no AI provider is "
            "configured). Review and edit it before saving."
        )
        return TextGenerationResult(text=text, model=_STUB_MODEL)
