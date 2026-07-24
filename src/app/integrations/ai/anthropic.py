"""Anthropic text provider (§8.18) — a live model behind the AI seam.

Wired but only selected when ``ai_provider=anthropic`` and ``ai_api_key`` are
configured (neither exists in this environment by default, so the offline stub
is the running default). Talks to the Messages API directly over ``httpx`` — no
SDK dependency, so the seam stays a thin, provider-neutral boundary. Any
failure or timeout raises :class:`AIError`; the service turns that into a 503,
never a hang (§8.18's "sane timeout + graceful error").
"""

from __future__ import annotations

import httpx
import structlog

from app.integrations.ai.base import (
    AIError,
    TextGenerationRequest,
    TextGenerationResult,
)

logger = structlog.get_logger(__name__)

PROVIDER_KEY = "anthropic"
_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"


class AnthropicTextProvider:
    """An :class:`~app.integrations.ai.base.AITextProvider` backed by the
    Anthropic Messages API."""

    def __init__(self, *, api_key: str, model: str, timeout_seconds: float) -> None:
        if not api_key:
            # Fail fast at construction, like storage/secret config — an
            # unconfigured live provider must never silently no-op.
            raise ValueError("Anthropic provider requires ai_api_key.")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds

    @property
    def key(self) -> str:
        return PROVIDER_KEY

    async def generate_text(self, request: TextGenerationRequest) -> TextGenerationResult:
        payload: dict[str, object] = {
            "model": self._model,
            "max_tokens": request.max_output_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.prompt}],
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(_API_URL, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("ai_provider_transport_error", error=str(exc))
            raise AIError(f"AI provider transport failure: {exc}", permanent=False) from exc

        if response.status_code == httpx.codes.OK:
            return self._parse(response.json())
        # 4xx (except 429) is a permanent config/request problem; 429 and 5xx
        # are transient — same classification the portal/billing adapters use.
        permanent = 400 <= response.status_code < 500 and response.status_code != 429
        logger.warning("ai_provider_error", status=response.status_code, permanent=permanent)
        raise AIError(f"AI provider returned {response.status_code}.", permanent=permanent)

    def _parse(self, body: dict[str, object]) -> TextGenerationResult:
        content = body.get("content")
        if not isinstance(content, list):
            raise AIError("AI provider returned an unexpected response shape.", permanent=True)
        # Only join string ``text`` values — a malformed block (e.g. a null
        # ``text``) must not raise a ``TypeError`` that would escape as a 500
        # instead of the intended empty-text ``AIError`` → 503.
        text = "".join(
            value
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
            if isinstance(value := block.get("text"), str)
        ).strip()
        if not text:
            raise AIError("AI provider returned empty text.", permanent=True)
        model = body.get("model")
        return TextGenerationResult(text=text, model=str(model) if model else self._model)
