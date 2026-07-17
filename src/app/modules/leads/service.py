"""Leads business logic: capture, contact dedupe, the assignment engine,
pipeline transitions, scoring, drip orchestration and the contact timeline
(§8.4).

One module, not the ``leads``/``clients`` split project.md §5 lists — a
deliberate deviation. Every lead has a mandatory ``contact_id`` and there is
no standalone contact-portal lifecycle in this part; splitting into two
services that both need row-level access to the same intertwined tables for
every operation is ceremony without an isolation benefit, given the
codebase's "modules call each other's service, not repository" rule. A
future part can still carve out a slim ``clients`` module if a standalone
contact/account portal materializes.

Scoping model (§7.2/§8.5): agents act on leads assigned to them; team leads
see their own plus their team members' (membership via the agents module's
boundary accessor); admins and marketing act tenant-wide.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import quote

import structlog
from fastapi import Depends

from app.core.database import SessionDep, on_commit
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.core.i18n import DEFAULT_LOCALE, pick_localized
from app.core.pagination import InvalidCursorError, clamp_limit, decode_cursor, encode_cursor
from app.core.permissions import AuthenticatedUser, Role
from app.core.tenancy import TenantContext
from app.modules.agents.service import AgentsService, build_agents_boundary
from app.modules.leads.models import (
    ActivityType,
    AssignmentRule,
    AssignmentStrategy,
    Contact,
    DripStopReason,
    Lead,
    LeadActivity,
    LeadDripState,
    LeadSource,
    LeadStage,
)
from app.modules.leads.repository import LeadsRepository
from app.modules.leads.schemas import (
    ActivityCreate,
    ActivityOut,
    AssignmentRuleConfig,
    ContactCaptureIn,
    ContactOut,
    ContactTimelineOut,
    ContactUpdate,
    LeadCaptureCreate,
    LeadCreate,
    LeadFilters,
    LeadOut,
    LeadUpdate,
    TimelineEntryOut,
    WhatsAppClickCreate,
    _CaptureBase,
)
from app.modules.listings.service import ListingService, get_listing_service
from app.modules.users.service import UserService, get_user_service
from app.workers.tasks.email import send_email

logger = structlog.get_logger(__name__)

# Manager-action gates (reassigning a lead); distinct from *visibility*, which
# lives on AgentsService.scope_user_ids_for.
MANAGES_ALL_ROLES = frozenset({Role.ADMIN, Role.TEAM_LEAD, Role.MARKETING})

# Stages where a drip sequence keeps running — a real conversation underway
# (qualified+) makes further automated nudges noise, or worse, a risk of
# stepping on a live conversation.
DRIP_ACTIVE_STAGES = frozenset({LeadStage.NEW, LeadStage.CONTACTED})

# Source-quality weight (lead scoring v1, §8.4) — phone-ins and portal leads
# tend to be warmer than a passive ad click.
_SOURCE_WEIGHT: dict[LeadSource, int] = {
    LeadSource.PHONE: 40,
    LeadSource.LISTING_FORM: 30,
    LeadSource.PORTAL: 25,
    LeadSource.VALUATION: 25,
    # A wa.me click is warm intent — the visitor is opening a direct chat.
    LeadSource.WHATSAPP_CLICK: 30,
    LeadSource.CHAT: 20,
    LeadSource.SEARCH_SIGNUP: 15,
    LeadSource.AD: 10,
    LeadSource.OTHER: 5,
}
_ENGAGEMENT_CAP = 25
_RECENCY_DECAY_CAP = 20

# Fixed default drip copy (day 0/2/7) — single-locale v1, tenant-overridable
# via ``tenant.settings["leads"]["drip_sequence"]`` (a full replacement list
# of the same shape). SMS/WhatsApp steps deferred: no adapter exists yet.
DEFAULT_DRIP_SEQUENCE: list[dict[str, Any]] = [
    {
        "day": 0,
        "subject": "Thanks for reaching out!",
        "body": "Thanks for your interest — one of our agents will be in touch shortly.",
    },
    {
        "day": 2,
        "subject": "Still looking?",
        "body": "Just checking in — let us know if you'd like to schedule a viewing.",
    },
    {
        "day": 7,
        "subject": "We're here whenever you're ready",
        "body": "No rush — reach out any time and we'll pick up where we left off.",
    },
]


def _drip_sequence(tenant: TenantContext) -> list[dict[str, Any]]:
    leads_settings: dict[str, Any] = tenant.settings.get("leads") or {}
    override = leads_settings.get("drip_sequence")
    return override if isinstance(override, list) and override else DEFAULT_DRIP_SEQUENCE


class LeadsService:
    def __init__(
        self,
        repo: LeadsRepository,
        users: UserService,
        listings: ListingService,
        agents: AgentsService,
    ) -> None:
        self.repo = repo
        self.users = users
        self.listings = listings
        self.agents = agents

    # ---- scoping helpers ----

    async def _scope_for(
        self, tenant_id: uuid.UUID, actor: AuthenticatedUser
    ) -> set[uuid.UUID] | None:
        """``None`` means tenant-wide; a user-id set narrows to assigned leads —
        one id for an agent, self + team members for a team lead (§8.5). The
        ADMIN/MARKETING/TEAM_LEAD/AGENT split itself lives once on
        ``AgentsService`` so listings and leads don't each re-derive it."""
        return await self.agents.scope_user_ids_for(tenant_id, actor)

    async def _get_scoped_lead_or_404(
        self,
        tenant_id: uuid.UUID,
        actor: AuthenticatedUser,
        lead_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> Lead:
        lead = await self.repo.get_lead(
            tenant_id,
            lead_id,
            scope_user_ids=await self._scope_for(tenant_id, actor),
            for_update=for_update,
        )
        if lead is None:
            # 404 for both "doesn't exist" and "not yours" — no existence oracle.
            raise NotFoundError("Lead not found.")
        return lead

    # ---- contact dedupe ----

    async def find_or_create_contact(
        self, tenant_id: uuid.UUID, data: ContactCaptureIn
    ) -> Contact:
        """Match priority: email first (stronger identity signal than a
        possibly-shared phone/landline), then phone. On a match, merge-fill
        only — never overwrite a non-NULL field with a fresher-but-not-
        necessarily-better one, union tags, only ever upgrade consent."""
        contact = await self.repo.find_contact_for_dedupe(
            tenant_id, email=data.email, phone=data.phone
        )
        if contact is None:
            contact = Contact(
                tenant_id=tenant_id,
                first_name=data.first_name,
                last_name=data.last_name,
                email=data.email,
                phone=data.phone,
                whatsapp=data.whatsapp,
                consent={"marketing_email": data.marketing_consent}
                if data.marketing_consent
                else {},
                tags=[],
            )
            self.repo.add(contact)
            await self.repo.flush()
            return contact

        if contact.first_name is None and data.first_name:
            contact.first_name = data.first_name
        if contact.last_name is None and data.last_name:
            contact.last_name = data.last_name
        if contact.email is None and data.email:
            contact.email = data.email
        if contact.phone is None and data.phone:
            contact.phone = data.phone
        if contact.whatsapp is None and data.whatsapp:
            contact.whatsapp = data.whatsapp
        if data.marketing_consent and not contact.consent.get("marketing_email"):
            contact.consent = {**contact.consent, "marketing_email": True}
        await self.repo.flush()
        return contact

    # ---- capture (public) ----

    async def capture_lead(self, tenant: TenantContext, data: LeadCaptureCreate) -> Lead | None:
        """Honeypot hits return ``None`` — the router synthesizes a
        real-shaped response without persisting anything, so a bot gets no
        signal to adapt against and nothing pollutes the CRM. A genuinely
        too-fast submission fails visibly instead (422, via the schema's own
        validator) — that case can be a legit client clock bug, not just a
        bot, and deserves feedback."""
        if data.hp:
            logger.info("lead_capture_honeypot_triggered")
            return None

        contact = await self.find_or_create_contact(tenant.id, data.contact)

        if data.listing_id is not None:
            await self.listings.get_public(tenant, str(data.listing_id))

        return await self._create_captured_lead(
            tenant,
            contact,
            listing_id=data.listing_id,
            source=data.source,
            source_meta=_capture_source_meta(data),
            message=data.message,
        )

    async def capture_whatsapp_click(
        self, tenant: TenantContext, data: WhatsAppClickCreate
    ) -> tuple[Lead | None, str]:
        """The wa.me handoff (§8.6): log the click as a lead *before* the
        visitor leaves for WhatsApp, then hand back the deep link. Number
        resolution: the listing's assigned agent's ``whatsapp_number`` first,
        the tenant's ``settings.contact.whatsapp_number`` second; neither
        configured is a loud 409 — silently swallowing the click would lose
        the lead's contact channel. Honeypot semantics mirror
        :meth:`capture_lead`: ``(None, url)`` still carries a real URL so a
        bot sees nothing different in the response."""
        listing = None
        if data.listing_id is not None:
            listing = await self.listings.get_public(tenant, str(data.listing_id))

        number: str | None = None
        if listing is not None and listing.agent_id is not None:
            number = await self.agents.whatsapp_number_for(tenant.id, listing.agent_id)
        if number is None:
            contact_settings: dict[str, Any] = tenant.settings.get("contact") or {}
            raw = contact_settings.get("whatsapp_number")
            number = raw if isinstance(raw, str) else None
        # Agent numbers are schema-validated E.164, but the tenant-settings
        # fallback is free-form JSONB — reduce to digits and treat anything
        # empty as unconfigured rather than minting a dead wa.me link.
        digits = re.sub(r"\D", "", number or "")
        if not digits:
            raise ConflictError("This agency has not configured a WhatsApp contact number.")

        if listing is not None:
            title = pick_localized(listing.title, DEFAULT_LOCALE)
            prefill = f"Hello, I'm interested in listing {listing.reference_code}"
            if title:
                prefill += f" — {title}"
        else:
            prefill = "Hello, I'd like more information about your listings."
        whatsapp_url = f"https://wa.me/{digits}?text={quote(prefill)}"

        if data.hp:
            logger.info("lead_capture_honeypot_triggered")
            return None, whatsapp_url

        contact = await self.find_or_create_contact(tenant.id, data.contact)
        lead = await self._create_captured_lead(
            tenant,
            contact,
            listing_id=data.listing_id,
            source=LeadSource.WHATSAPP_CLICK,
            source_meta=_capture_source_meta(data),
            message=data.message,
        )
        return lead, whatsapp_url

    async def register_signup_lead(self, tenant: TenantContext, email: str) -> Lead:
        """Boundary for favorites' confirmed anonymous search signup (§8.9).
        The double-opt-in confirm *is* the capture — same dedupe, scoring,
        assignment and drip path, minus the form-level spam defense (the
        consumed token already proved a live mailbox). Confirming the alert
        subscription is the marketing-email consent being recorded."""
        contact = await self.find_or_create_contact(
            tenant.id, ContactCaptureIn(email=email, marketing_consent=True)
        )
        return await self._create_captured_lead(
            tenant,
            contact,
            listing_id=None,
            source=LeadSource.SEARCH_SIGNUP,
            source_meta={},
            message=None,
        )

    async def _create_captured_lead(
        self,
        tenant: TenantContext,
        contact: Contact,
        *,
        listing_id: uuid.UUID | None,
        source: LeadSource,
        source_meta: dict[str, Any],
        message: str | None,
    ) -> Lead:
        """The shared capture trunk: lead row → score → assignment engine →
        activities → drip seed → post-commit speed-to-lead notification."""
        lead = Lead(
            tenant_id=tenant.id,
            contact_id=contact.id,
            listing_id=listing_id,
            source=source,
            source_meta=source_meta,
        )
        self.repo.add(lead)
        await self.repo.flush()

        await self._recompute_score(tenant.id, lead)
        agent_id = await self.assign_lead(tenant, lead)

        if message:
            self.repo.add(
                LeadActivity(
                    tenant_id=tenant.id,
                    lead_id=lead.id,
                    actor_id=None,
                    type=ActivityType.NOTE,
                    payload={"text": message, "via": "capture"},
                )
            )
        if agent_id is not None:
            self.repo.add(
                LeadActivity(
                    tenant_id=tenant.id,
                    lead_id=lead.id,
                    actor_id=None,
                    type=ActivityType.ASSIGNMENT,
                    payload={"agent_id": str(agent_id)},
                )
            )
        self._seed_drip(tenant, lead)
        await self.repo.flush()

        lead_id: uuid.UUID = lead.id
        agent_email: str | None = None
        if agent_id is not None:
            identity = await self.users.get_identity_if_active(tenant.id, agent_id)
            agent_email = identity.email if identity else None

        async def _notify() -> None:
            # Speed-to-lead (§8.4): enqueued post-commit so the notification
            # never fires before the lead row is actually visible.
            if agent_email:
                send_email.delay(
                    to=agent_email,
                    subject="New lead assigned to you",
                    text=f"A new lead ({lead_id}) has been assigned to you. Respond quickly!",
                )

        on_commit(self.repo.session, _notify)
        return lead

    # ---- assignment engine ----

    async def assign_lead(self, tenant: TenantContext, lead: Lead) -> uuid.UUID | None:
        rule = await self.repo.get_assignment_rule(tenant.id)
        strategy = rule.strategy if rule is not None else AssignmentStrategy.LISTING_AGENT
        config = rule.config if rule is not None else {}

        agent_id: uuid.UUID | None = None
        if strategy is AssignmentStrategy.LISTING_AGENT:
            if lead.listing_id is not None:
                listing_agent = await self.listings.agent_for(tenant.id, lead.listing_id)
                if listing_agent is not None:
                    # Re-validated: the agent may have been disabled since.
                    identity = await self.users.get_identity_if_active(tenant.id, listing_agent)
                    if identity is not None:
                        agent_id = listing_agent
        elif strategy is AssignmentStrategy.ROUND_ROBIN:
            agent_id = await self._pick_round_robin(tenant.id, config)
        elif strategy is AssignmentStrategy.TERRITORY:
            agent_id = await self._pick_territory(tenant, lead, config)

        lead.agent_id = agent_id
        return agent_id

    async def _least_loaded(
        self, tenant_id: uuid.UUID, candidates: list[uuid.UUID], config: dict[str, Any]
    ) -> uuid.UUID | None:
        """Least-loaded pick over an already-validated candidate pool.
        Deliberately best-effort under concurrency: two simultaneous captures
        can read the same counts and pick the same agent, briefly skewing
        balance (or nudging past ``max_open_leads_per_agent`` by one). A
        per-tenant lock on every public capture isn't worth serializing the
        hot path for a soft load-balancing target — unlike reference codes
        (Part 4), nothing here must be unique. Shared by round-robin and
        territory assignment (§8.4/§8.5)."""
        if not candidates:
            return None
        max_open = config.get("max_open_leads_per_agent")
        counts = await self.repo.open_lead_counts(tenant_id, candidates)
        best_id: uuid.UUID | None = None
        best_count: int | None = None
        for agent_id in candidates:
            count = counts.get(agent_id, 0)
            if max_open is not None and count >= max_open:
                continue
            if best_count is None or count < best_count:
                best_id, best_count = agent_id, count
        return best_id

    async def _pick_territory(
        self, tenant: TenantContext, lead: Lead, config: dict[str, Any]
    ) -> uuid.UUID | None:
        """Point-in-polygon over published profiles' ``service_areas`` (§8.5);
        ties broken least-loaded like round-robin. Leads without a listing
        point stay unassigned — the >30-min escalation sweep already covers
        those."""
        if lead.listing_id is None:
            return None
        point = await self.listings.point_for(tenant.id, lead.listing_id)
        if point is None:
            return None
        lon, lat = point
        candidate_ids = await self.agents.candidates_for_point(tenant.id, lon, lat)
        if not candidate_ids:
            return None
        # Re-validated in one batch: an agent may have been disabled since.
        identities = await self.users.identities_for(tenant.id, candidate_ids)
        candidates = [cid for cid in candidate_ids if cid in identities]
        return await self._least_loaded(tenant.id, candidates, config)

    async def _pick_round_robin(
        self, tenant_id: uuid.UUID, config: dict[str, Any]
    ) -> uuid.UUID | None:
        # config is written only through AssignmentRuleConfig, so agent_pool
        # entries are guaranteed UUID strings and max_open an int ≥ 1.
        pool_ids = config.get("agent_pool")
        if pool_ids:
            requested = [uuid.UUID(raw_id) for raw_id in pool_ids]
            identities = await self.users.identities_for(tenant_id, requested)
            candidates = [uid for uid in requested if uid in identities]
        else:
            candidates = [u.id for u in await self.users.list_active_agents(tenant_id)]
        return await self._least_loaded(tenant_id, candidates, config)

    async def update_assignment_rule(
        self, tenant: TenantContext, strategy: AssignmentStrategy, config: AssignmentRuleConfig
    ) -> AssignmentRule:
        if strategy is AssignmentStrategy.TERRITORY and not (
            await self.agents.has_territory_data(tenant.id)
        ):
            # Same fail-early stance as before §8.5 existed: don't accept a
            # policy that can never actually match an agent.
            raise ConflictError(
                "Territory-based assignment needs at least one published agent "
                "profile with service areas."
            )
        return await self.repo.upsert_assignment_rule(
            tenant.id, strategy=strategy, config=config.model_dump(mode="json", exclude_none=True)
        )

    async def get_assignment_rule(self, tenant: TenantContext) -> AssignmentRule:
        rule = await self.repo.get_assignment_rule(tenant.id)
        if rule is None:
            raise NotFoundError("No assignment rule is configured for this tenant.")
        return rule

    # ---- scoring ----

    async def _recompute_score(self, tenant_id: uuid.UUID, lead: Lead) -> None:
        activities = await self.repo.list_activities_for_lead(tenant_id, lead.id)
        engagement = sum(1 for a in activities if a.type is not ActivityType.SYSTEM)
        last_at = max((a.created_at for a in activities), default=lead.created_at)
        days_since = max((datetime.now(UTC) - last_at.replace(tzinfo=UTC)).days, 0)

        score = _SOURCE_WEIGHT.get(lead.source, 0)
        # Budget/price-match approximation: no budget field exists on
        # Contact/Lead in the fixed §6.4 schema, so "attached to a listing"
        # stands in as a coarse intent signal, not a real price match.
        score += 15 if lead.listing_id is not None else 0
        score += min(engagement * 5, _ENGAGEMENT_CAP)
        score -= min(days_since, _RECENCY_DECAY_CAP)
        lead.score = max(0, min(100, score))

    # ---- pipeline ----

    async def create_manual(
        self, tenant: TenantContext, actor: AuthenticatedUser, data: LeadCreate
    ) -> Lead:
        if data.contact_id is not None:
            contact = await self.repo.get_contact(tenant.id, data.contact_id)
            if contact is None:
                raise NotFoundError("Contact not found.")
        else:
            assert data.contact is not None
            contact = await self.find_or_create_contact(tenant.id, data.contact)

        if data.listing_id is not None:
            await self.listings.get_portal(tenant, actor, data.listing_id)

        agent_id = data.agent_id
        if agent_id is not None:
            identity = await self.users.get_identity_if_active(tenant.id, agent_id)
            if identity is None:
                raise ConflictError("The assigned agent does not exist or is not active.")

        lead = Lead(
            tenant_id=tenant.id,
            contact_id=contact.id,
            listing_id=data.listing_id,
            agent_id=agent_id,
            source=data.source,
        )
        self.repo.add(lead)
        await self.repo.flush()
        if agent_id is None:
            await self.assign_lead(tenant, lead)
        await self._recompute_score(tenant.id, lead)
        self._seed_drip(tenant, lead)
        await self.repo.flush()
        return lead

    async def get_portal(
        self, tenant: TenantContext, actor: AuthenticatedUser, lead_id: uuid.UUID
    ) -> Lead:
        return await self._get_scoped_lead_or_404(tenant.id, actor, lead_id)

    async def list_portal(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        *,
        filters: LeadFilters,
        cursor: str | None,
        limit: int | None,
    ) -> tuple[list[Lead], str | None, int]:
        page_size = clamp_limit(limit)
        after = _decode_keyset(cursor) if cursor else None
        scope = await self._scope_for(tenant.id, actor)
        rows = await self.repo.list_leads(
            tenant.id, scope_user_ids=scope, filters=filters, after=after, limit=page_size
        )
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size:
            last = items[-1]
            next_cursor = encode_cursor(
                {"created_at": last.created_at.isoformat(), "id": str(last.id)}
            )
        total = await self.repo.count_leads(tenant.id, scope_user_ids=scope, filters=filters)
        return items, next_cursor, total

    async def update(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        lead_id: uuid.UUID,
        data: LeadUpdate,
    ) -> Lead:
        lead = await self._get_scoped_lead_or_404(tenant.id, actor, lead_id)
        patch = data.model_dump(exclude_unset=True)
        if "agent_id" in patch:
            # Reassignment is LEAD_ASSIGN-blast-radius territory (an agent
            # could otherwise hand any lead they own to anyone) — mirror
            # listings' manager-only agent_id gate.
            if actor.role not in MANAGES_ALL_ROLES:
                raise PermissionDeniedError("Only managers can reassign a lead.")
            if data.agent_id is not None:
                identity = await self.users.get_identity_if_active(tenant.id, data.agent_id)
                if identity is None:
                    raise ConflictError("The assigned agent does not exist or is not active.")
        for field, value in patch.items():
            setattr(lead, field, value)
        await self.repo.flush()
        return lead

    async def transition_stage(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        lead_id: uuid.UUID,
        to_stage: LeadStage,
        lost_reason: str | None,
    ) -> Lead:
        lead = await self._get_scoped_lead_or_404(tenant.id, actor, lead_id, for_update=True)
        if to_stage is LeadStage.LOST and not lost_reason:
            raise ConflictError("A reason is required when marking a lead lost.")
        from_stage = lead.stage
        lead.stage = to_stage
        if to_stage is LeadStage.LOST:
            lead.lost_reason = lost_reason
        self.repo.add(
            LeadActivity(
                tenant_id=tenant.id,
                lead_id=lead.id,
                actor_id=actor.id,
                type=ActivityType.STATUS_CHANGE,
                payload={"from": from_stage.value, "to": to_stage.value},
            )
        )
        if to_stage not in DRIP_ACTIVE_STAGES:
            await self.repo.stop_drip(tenant.id, lead.id, DripStopReason.STAGE_ADVANCED)
        await self._recompute_score(tenant.id, lead)
        await self.repo.flush()
        return lead

    # ---- activities / speed-to-lead ----

    async def record_activity(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        lead_id: uuid.UUID,
        data: ActivityCreate,
    ) -> LeadActivity:
        lead = await self._get_scoped_lead_or_404(tenant.id, actor, lead_id, for_update=True)
        activity = LeadActivity(
            tenant_id=tenant.id,
            lead_id=lead.id,
            actor_id=actor.id,
            type=data.type,
            payload=data.payload,
        )
        self.repo.add(activity)
        await self.repo.flush()

        agent_authored_types = {
            ActivityType.NOTE,
            ActivityType.CALL,
            ActivityType.EMAIL,
            ActivityType.SMS,
        }
        if lead.first_response_at is None and data.type in agent_authored_types:
            lead.first_response_at = datetime.now(UTC)
        if data.type in agent_authored_types:
            # Reply-detection approximation (no inbound webhook exists yet):
            # any agent-authored touch is treated as "the conversation moved."
            await self.repo.stop_drip(tenant.id, lead.id, DripStopReason.REPLIED)
        await self._recompute_score(tenant.id, lead)
        await self.repo.flush()
        return activity

    async def list_activities(
        self, tenant: TenantContext, actor: AuthenticatedUser, lead_id: uuid.UUID
    ) -> list[LeadActivity]:
        await self._get_scoped_lead_or_404(tenant.id, actor, lead_id)
        return await self.repo.list_activities_for_lead(tenant.id, lead_id)

    # ---- drip ----

    def _seed_drip(self, tenant: TenantContext, lead: Lead) -> None:
        if lead.stage not in DRIP_ACTIVE_STAGES:
            return
        self.repo.add(
            LeadDripState(
                tenant_id=tenant.id, lead_id=lead.id, step_index=0, next_send_at=datetime.now(UTC)
            )
        )

    # ---- contacts ----

    async def get_contact(
        self, tenant: TenantContext, actor: AuthenticatedUser, contact_id: uuid.UUID
    ) -> Contact:
        contact = await self.repo.get_contact(tenant.id, contact_id)
        if contact is None:
            raise NotFoundError("Contact not found.")
        return contact

    async def update_contact(
        self,
        tenant: TenantContext,
        actor: AuthenticatedUser,
        contact_id: uuid.UUID,
        data: ContactUpdate,
    ) -> Contact:
        contact = await self.get_contact(tenant, actor, contact_id)
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(contact, field, value)
        await self.repo.flush()
        return contact

    async def get_contact_timeline(
        self, tenant: TenantContext, actor: AuthenticatedUser, contact_id: uuid.UUID
    ) -> ContactTimelineOut:
        contact = await self.get_contact(tenant, actor, contact_id)
        scope = await self._scope_for(tenant.id, actor)
        # Ownership scoping applies here too: contacts aren't agent-owned,
        # leads are — an agent viewing a shared contact sees only their own
        # leads (and activity) against it, not a colleague's history.
        leads = await self.repo.list_leads(
            tenant.id,
            scope_user_ids=scope,
            filters=LeadFilters(),
            contact_id=contact_id,
            after=None,
            limit=1000,
        )
        activities = await self.repo.list_activities_for_leads(
            tenant.id, [lead.id for lead in leads]
        )

        entries = [
            TimelineEntryOut(
                kind="lead_created", at=lead.created_at, lead_id=lead.id, lead_stage=lead.stage
            )
            for lead in leads
        ] + [
            TimelineEntryOut(
                kind="activity",
                at=activity.created_at,
                lead_id=activity.lead_id,
                activity=ActivityOut.model_validate(activity),
            )
            for activity in activities
        ]
        entries.sort(key=lambda e: e.at, reverse=True)

        return ContactTimelineOut(
            contact=ContactOut.model_validate(contact),
            leads=[LeadOut.model_validate(lead) for lead in leads],
            entries=entries,
        )

    # ---- boundary accessors (agents' §8.5 stats) ----

    async def stats_for_agent(
        self, tenant_id: uuid.UUID, agent_user_id: uuid.UUID
    ) -> tuple[dict[str, int], float | None]:
        """(leads-by-stage, avg first-response seconds) for one agent."""
        return await self.repo.stats_for_agent(tenant_id, agent_user_id)

    # ---- drip advancement (called from the Beat task) ----

    async def advance_drip(self, tenant: TenantContext, drip: LeadDripState) -> None:
        sequence = _drip_sequence(tenant)
        if drip.step_index >= len(sequence):
            await self.repo.stop_drip(tenant.id, drip.lead_id, DripStopReason.SEQUENCE_COMPLETE)
            return

        lead = await self.repo.get_lead(tenant.id, drip.lead_id)
        if lead is None or lead.stage not in DRIP_ACTIVE_STAGES:
            await self.repo.stop_drip(tenant.id, drip.lead_id, DripStopReason.STAGE_ADVANCED)
            return

        contact = await self.repo.get_contact(tenant.id, lead.contact_id)
        step = sequence[drip.step_index]
        if contact is not None and contact.email:
            send_email.delay(to=contact.email, subject=step["subject"], text=step["body"])

        drip.step_index += 1
        if drip.step_index >= len(sequence):
            drip.stopped_at = datetime.now(UTC)
            drip.stopped_reason = DripStopReason.SEQUENCE_COMPLETE
        else:
            next_step = sequence[drip.step_index]
            drip.next_send_at = datetime.now(UTC) + timedelta(
                days=next_step["day"] - step["day"]
            )
        await self.repo.flush()


def _capture_source_meta(data: _CaptureBase) -> dict[str, Any]:
    return {
        k: v
        for k, v in {
            "utm_source": data.utm_source,
            "utm_medium": data.utm_medium,
            "utm_campaign": data.utm_campaign,
            "page": data.page,
            "referrer": data.referrer,
        }.items()
        if v is not None
    }


def _decode_keyset(cursor: str) -> tuple[datetime, uuid.UUID]:
    values = decode_cursor(cursor)
    try:
        return datetime.fromisoformat(values["created_at"]), uuid.UUID(values["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursorError("The provided cursor is malformed.") from exc


def get_leads_service(session: SessionDep) -> LeadsService:
    return LeadsService(
        LeadsRepository(session),
        get_user_service(session),
        get_listing_service(session),
        build_agents_boundary(session),
    )


LeadsServiceDep = Annotated[LeadsService, Depends(get_leads_service)]
