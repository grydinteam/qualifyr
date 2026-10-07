"""Decomposed transparent lead score (0-100).

Reference formula (reverse-engineered from the competitor xlsx):
  review_band (0-30) + rating (0-10) + proximity_tier (0-15)
  + online_gap (0-25) + pain_evidence (0-20) = max 100

Each dimension is observable and independently auditable. When Google Places
data is missing (no API key), review_band and rating are 0 – the lead still
ranks on online_gap + pain_evidence + proximity."""

from __future__ import annotations

from dataclasses import dataclass

from gtm_engine.config.schema import CampaignConfig, EngineSettings
from gtm_engine.models import (
    Classification, CompanyQuality, CompanyType, Contact, DiscoveredCompany, EmailStatus,
    OnlinePresence, Priority, ScoreBreakdown, Signals,
)
from gtm_engine.scoring.proximity import TIER_POINTS, proximity_tier


@dataclass
class ScoreInputs:
    company: DiscoveredCompany
    classification: Classification
    quality: CompanyQuality
    contact: Contact
    signals: Signals
    online_presence: OnlinePresence | None = None
    settings: EngineSettings | None = None


def _review_band(count: int | None, max_pts: int) -> tuple[int, str | None]:
    """0-30 points based on Google review count bands."""
    if count is None:
        return 0, None
    if count >= 200:
        pts = max_pts
        reason = f"{count} Google reviews (200+)"
    elif count >= 100:
        pts = int(max_pts * 0.8)
        reason = f"{count} Google reviews (100-199)"
    elif count >= 50:
        pts = int(max_pts * 0.6)
        reason = f"{count} Google reviews (50-99)"
    elif count >= 10:
        pts = int(max_pts * 0.4)
        reason = f"{count} Google reviews (10-49)"
    elif count >= 1:
        pts = int(max_pts * 0.2)
        reason = f"{count} Google reviews (1-9)"
    else:
        pts = 0
        reason = "no Google reviews"
    return pts, reason


def _rating_score(rating: float | None, max_pts: int) -> tuple[int, str | None]:
    """0-10 points linearly scaled from Google rating (1.0-5.0)."""
    if rating is None:
        return 0, None
    clamped = max(1.0, min(5.0, rating))
    pts = int(round((clamped - 1.0) / 4.0 * max_pts))
    return pts, f"Google rating {rating:.1f}/5"


def _proximity_score(
    company: DiscoveredCompany,
    settings: EngineSettings | None,
    max_pts: int,
    reasons: list[str],
) -> int:
    """0-15 points based on distance tier from anchor."""
    lat = company.extra.get("lat")
    lon = company.extra.get("lon")
    anchor_lat = settings.anchor_lat if settings else None
    anchor_lon = settings.anchor_lon if settings else None
    tier1_km = settings.proximity_tier1_km if settings else 5.0
    tier2_km = settings.proximity_tier2_km if settings else 15.0

    tier, dist = proximity_tier(lat, lon, anchor_lat, anchor_lon, tier1_km, tier2_km)
    base = TIER_POINTS.get(tier, 8)
    pts = int(round(base * max_pts / 15))
    if dist is not None:
        reasons.append(f"Tier {tier} ({dist:.1f} km from anchor)")
    elif anchor_lat is None:
        reasons.append(f"Tier {tier} (no anchor configured, same-city default)")
    else:
        reasons.append(f"Tier {tier} (no coordinates on record)")
    return pts


def _pain_evidence_score(
    online_presence: OnlinePresence | None,
    signals: Signals,
    max_pts: int,
    reasons: list[str],
) -> int:
    """0-20 points for pain signals from reviews and site crawl."""
    pts = 0.0

    # Pain from Google review text (strongest signal)
    if online_presence and online_presence.pain_from_reviews:
        n = min(len(online_presence.pain_from_reviews), 5)
        pts += n * 3
        reasons.append(f"review pain: {', '.join(online_presence.pain_from_reviews[:3])}")

    # Pain from site crawl signals
    if signals.pain:
        n = min(len(signals.pain), 3)
        pts += n * 1.5

    # Intent signals are evidence of active need
    if signals.intent:
        pts += 2
        for s in signals.intent[:1]:
            reasons.append(f"intent: {s.get('kind')} – {s.get('text', '')[:50]}")

    return min(int(round(pts)), max_pts)


def score_lead(inputs: ScoreInputs, campaign: CampaignConfig) -> ScoreBreakdown:
    w = campaign.weights
    reasons: list[str] = []
    op = inputs.online_presence

    # --- Review band (default 30)
    review_count = op.google_review_count if op else None
    rb_pts, rb_reason = _review_band(review_count, w.review_band)
    if rb_reason:
        reasons.append(rb_reason)

    # --- Rating (default 10)
    rating = op.google_rating if op else None
    rt_pts, rt_reason = _rating_score(rating, w.rating)
    if rt_reason:
        reasons.append(rt_reason)

    # --- Proximity tier (default 15)
    px_pts = _proximity_score(inputs.company, inputs.settings, w.proximity_tier, reasons)

    # --- Online gap (default 25)
    og_pts = 0
    if op:
        og_pts = min(op.online_gap_score, w.online_gap)
        if op.online_gap_score >= 18:
            reasons.append("online gap: no ordering channel – strong candidate")
        elif op.online_gap_score >= 10:
            reasons.append("online gap: limited digital presence")
        elif op.online_gap_score > 0:
            reasons.append("online gap: some digital channels present")
        else:
            reasons.append("online gap: mature digital presence")
        if op.delivery_platforms:
            reasons.append(f"listed on: {', '.join(op.delivery_platforms)}")

    # --- Pain evidence (default 20)
    pe_pts = _pain_evidence_score(op, inputs.signals, w.pain_evidence, reasons)

    total = rb_pts + rt_pts + px_pts + og_pts + pe_pts

    # --- Classification context (informs reasons but not score)
    cls = inputs.classification
    if cls.company_type == CompanyType.BUYER:
        reasons.append("classified as BUYER")
    elif cls.company_type == CompanyType.VENDOR:
        reasons.append("classified as VENDOR")
    if cls.intent_buyer is True:
        reasons.append(f"intent match ({cls.intent_confidence:.0%}): {cls.intent_reason}")
    elif cls.intent_buyer is False:
        reasons.append(f"intent: no evident need ({cls.intent_confidence:.0%})")

    # --- Contact context
    contact = inputs.contact
    if contact.is_decision_maker and contact.name:
        reasons.append(f"decision-maker: {contact.name} ({contact.role})")
    if contact.email_status == EmailStatus.DELIVERABLE:
        reasons.append("email confirmed")

    # --- Routing
    r = campaign.routing
    if cls.company_type == CompanyType.VENDOR:
        priority = Priority.REJECT
        reasons.insert(0, "classified as VENDOR: rejected regardless of score")
    elif total >= r.high_priority:
        priority = Priority.HIGH
    elif total >= r.qualified:
        priority = Priority.QUALIFIED
    elif total >= r.review:
        priority = Priority.REVIEW
    else:
        priority = Priority.REJECT

    if cls.company_type == CompanyType.UNKNOWN and priority in (Priority.HIGH, Priority.QUALIFIED):
        priority = Priority.REVIEW
        reasons.append("held for review: buyer status unconfirmed")
    q = inputs.quality
    if q.website_mismatch and priority in (Priority.HIGH, Priority.QUALIFIED):
        priority = Priority.REVIEW
        reasons.append("held for review: website may belong to a different company")

    return ScoreBreakdown(
        review_band=rb_pts, rating_score=rt_pts, proximity_tier=px_pts,
        online_gap=og_pts, pain_evidence=pe_pts,
        total=int(total), reasons=reasons, priority=priority,
    )


def is_outreach_ready(cls: Classification, score: ScoreBreakdown, contact: Contact, campaign: CampaignConfig) -> bool:
    """Whether a qualified buyer is actually reachable.

    A usable contact is a deliverable email OR a phone number. Phone matters because most SMBs
    in this market (shops, clinics, retailers) publish a number and run on call / WhatsApp, while
    the companies that score highest on online-gap are precisely the ones least likely to expose a
    scrapeable email — so an email-only gate marked almost every qualified lead unreachable.

    The email sequencer requires a usable email on its own (see outreach.sequencer.eligible), so a
    phone-only lead is surfaced as reachable here but is never auto-emailed; it is a call/WhatsApp
    lead for the operator."""
    if not (
        cls.company_type == CompanyType.BUYER
        and score.total >= campaign.min_score
        and score.priority in (Priority.HIGH, Priority.QUALIFIED)
    ):
        return False
    has_email = contact.email is not None and contact.email_status in (
        EmailStatus.MX_VALID, EmailStatus.GENERIC, EmailStatus.DELIVERABLE)
    has_phone = bool(contact.phone)
    return has_email or has_phone
