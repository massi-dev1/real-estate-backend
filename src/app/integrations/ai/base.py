"""AI provider contract (§8.18, §5 ``integrations/``).

Infrastructure, not a feature module: no DB, no RBAC, no router. Defines the
provider-agnostic interface every AI text provider is driven through, so a
module (listings' description drafting) never imports a model SDK — swapping
one provider for another is a config change with no call-site edit.

Same "design the seam, defer the tuned product" stance as Part 19's
e-signature, Part 20's portal adapter, and Part 22's billing: no real model
credentials exist in this environment, so the concrete adapter shipped is a
clearly-labelled offline :class:`~app.integrations.ai.stub.StubTextProvider`.
An :class:`~app.integrations.ai.anthropic.AnthropicTextProvider` is wired for
when ``ai_provider=anthropic`` + ``ai_api_key`` are configured.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class AIError(Exception):
    """A provider call failed. ``permanent`` splits an unrecoverable rejection
    (bad request/auth/config) from a transient transport failure or timeout,
    mirroring the portal and billing adapters' error split. The service layer
    turns either into a 503 problem+json — the caller may retry."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


@dataclass(frozen=True, slots=True)
class TextGenerationRequest:
    """A provider-neutral text-generation request.

    ``system`` frames the task; ``prompt`` carries the structured inputs. Kept
    deliberately small — a richer message/turn shape is added the day a
    provider needs it, not speculatively.
    """

    system: str
    prompt: str
    max_output_tokens: int = 1024
    # A soft steer, honoured on a best-effort basis by a real provider (the
    # stub ignores it); ``None`` = the provider default.
    temperature: float | None = None


@dataclass(frozen=True, slots=True)
class TextGenerationResult:
    """What a provider returns. ``text`` is the raw model output — never
    auto-persisted (§8.18): the caller returns it as a draft the human edits
    and explicitly saves."""

    text: str
    model: str


@runtime_checkable
class AITextProvider(Protocol):
    """The contract every AI text provider satisfies (§8.18).

    ``key`` is the stable provider identifier (config ``ai_provider``).
    """

    @property
    def key(self) -> str: ...

    async def generate_text(self, request: TextGenerationRequest) -> TextGenerationResult:
        """Generate text for ``request``. Raises :class:`AIError` on failure or
        timeout — the service turns that into a 503, never a hang."""
