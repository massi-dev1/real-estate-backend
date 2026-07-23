"""AI features (§8.18): the description-generation endpoint behind the
provider-agnostic seam, and the LeadScorer seam preserving Part 8's score.

No live model credentials exist in this environment, so the offline stub
provider is the running default — the whole request → draft → agent-saves flow
is exercised deterministically. A monkeypatched failing provider covers the
graceful-503 path.
"""

import uuid
from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient

from app.core.permissions import Role
from app.integrations.ai.base import AIError, TextGenerationRequest, TextGenerationResult
from app.integrations.ai.scoring import (
    LeadScoringFeatures,
    RulesLeadScorer,
    build_lead_scorer,
)
from app.integrations.ai.stub import StubTextProvider
from tests.helpers import HOST_A
from tests.test_listings import make_listing, tenant_and_login

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]


# ---- the provider seam itself (no HTTP) ----


async def test_stub_provider_is_deterministic_and_offline() -> None:
    provider = StubTextProvider()
    assert provider.key == "stub"
    req = TextGenerationRequest(system="s", prompt="Title: Nice flat\nPrice: 100 DZD")
    first = await provider.generate_text(req)
    second = await provider.generate_text(req)
    assert isinstance(first, TextGenerationResult)
    assert first.model == "stub-echo"
    assert first.text == second.text  # deterministic
    assert "Nice flat" in first.text  # echoes the structured prompt


# ---- POST /listings/{id}/generate-description ----


async def test_generate_description_returns_draft_all_locales(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)

    resp = await client.post(
        f"/api/v1/portal/listings/{listing['id']}/generate-description",
        json={},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["model"] == "stub-echo"
    # Default = every supported locale, each drafted.
    assert set(body["description"]) == {"ar", "fr", "en"}
    for text in body["description"].values():
        assert text.strip()

    # It is a DRAFT — never auto-persisted over the agent's own copy.
    got = await client.get(f"/api/v1/portal/listings/{listing['id']}", headers=admin)
    assert got.json()["description"] == {"fr": "Lumineux, proche du centre."}


async def test_generate_description_specific_locale(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)

    resp = await client.post(
        f"/api/v1/portal/listings/{listing['id']}/generate-description",
        json={"locales": ["en"], "tone": "luxury"},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["description"]) == {"en"}


async def test_generate_description_rejects_unknown_locale(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)

    resp = await client.post(
        f"/api/v1/portal/listings/{listing['id']}/generate-description",
        json={"locales": ["de"]},
        headers=admin,
    )
    assert resp.status_code == 422, resp.text


async def test_generate_description_missing_listing_404(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await client.post(
        f"/api/v1/portal/listings/{uuid.uuid4()}/generate-description",
        json={},
        headers=admin,
    )
    assert resp.status_code == 404, resp.text


async def test_generate_description_requires_listing_manage(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    listing = await make_listing(client, admin)
    # A buyer-tier account (no LISTING_MANAGE) is forbidden.
    from tests.conftest import FIXTURE_PASSWORD
    from tests.helpers import bearer, login_user

    await create_tenant_user(str(tenant["id"]), "buyer@a.example.com", Role.BUYER_RENTER)
    login = await login_user(client, HOST_A, "buyer@a.example.com", FIXTURE_PASSWORD)
    buyer = {"Host": HOST_A, "Authorization": bearer(login)}

    resp = await client.post(
        f"/api/v1/portal/listings/{listing['id']}/generate-description",
        json={},
        headers=buyer,
    )
    assert resp.status_code == 403, resp.text


async def test_generate_description_provider_failure_is_503(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)

    class FailingProvider:
        @property
        def key(self) -> str:
            return "failing"

        async def generate_text(
            self, request: TextGenerationRequest
        ) -> TextGenerationResult:
            raise AIError("boom", permanent=False)

    # Patch where the listings service resolves the provider.
    monkeypatch.setattr(
        "app.modules.listings.service.build_ai_text_provider",
        lambda _settings: FailingProvider(),
    )

    resp = await client.post(
        f"/api/v1/portal/listings/{listing['id']}/generate-description",
        json={},
        headers=admin,
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["type"].endswith("upstream-unavailable")


# ---- LeadScorer seam (pure, no HTTP): Part 8 behaviour preserved ----


def test_rules_scorer_matches_part8_formula() -> None:
    scorer = build_lead_scorer()
    assert isinstance(scorer, RulesLeadScorer)
    # source 30 + listing 15 + engagement min(3*5,25)=15 - recency 4 - no-show 0
    features = LeadScoringFeatures(
        source_weight=30,
        attached_to_listing=True,
        engagement_count=3,
        days_since_last_activity=4,
        no_show_count=0,
    )
    assert scorer.score(features) == 56


def test_rules_scorer_clamps_and_penalises() -> None:
    scorer = RulesLeadScorer()
    # Two no-shows (-30) drag a warm lead down but never below 0.
    low = scorer.score(
        LeadScoringFeatures(
            source_weight=10,
            attached_to_listing=False,
            engagement_count=0,
            days_since_last_activity=0,
            no_show_count=2,
        )
    )
    assert low == 0
    # Engagement is capped at 25; recency decay is capped at 20.
    high = scorer.score(
        LeadScoringFeatures(
            source_weight=40,
            attached_to_listing=True,
            engagement_count=100,
            days_since_last_activity=0,
            no_show_count=0,
        )
    )
    assert high == min(40 + 15 + 25, 100)
