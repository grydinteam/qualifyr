"""Queue + sequence state machine.

    not_queued -> queued -> email_1_sent -> followup_1_sent -> followup_2_sent (done)
                                 \\-> replied | bounced | unsubscribed | suppressed  (terminal)

Only leads that passed the outreach gate (outreach_ready) are ever queued, and the
ledger is consulted before every send so an address never receives a step twice."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from gtm_engine.config.schema import CampaignConfig
from gtm_engine.models import CompanyType, EmailStatus, Lead, SequenceStatus, utcnow
from gtm_engine.outreach.config import OutreachSettings, Templates
from gtm_engine.outreach.ledger import Ledger
from gtm_engine.outreach.sender import OutgoingEmail, Sender
from gtm_engine.outreach.templates import render
from gtm_engine.storage.database import Database

log = logging.getLogger(__name__)

ACTIVE = [SequenceStatus.QUEUED, SequenceStatus.EMAIL_1_SENT, SequenceStatus.FOLLOWUP_1_SENT]
TERMINAL = {SequenceStatus.REPLIED, SequenceStatus.BOUNCED, SequenceStatus.UNSUBSCRIBED,
            SequenceStatus.SUPPRESSED, SequenceStatus.FOLLOWUP_2_SENT, SequenceStatus.COMPLETED}

# status -> (step to send now, status after sending)
NEXT_STEP: dict[SequenceStatus, tuple[str, SequenceStatus]] = {
    SequenceStatus.QUEUED: ("email_1", SequenceStatus.EMAIL_1_SENT),
    SequenceStatus.EMAIL_1_SENT: ("followup_1", SequenceStatus.FOLLOWUP_1_SENT),
    SequenceStatus.FOLLOWUP_1_SENT: ("followup_2", SequenceStatus.FOLLOWUP_2_SENT),
}


@dataclass
class SendReport:
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    stopped_reason: str | None = None
    details: list[str] = field(default_factory=list)
    mailboxes: dict[str, dict] = field(default_factory=dict)


def in_send_window(now: datetime, settings: OutreachSettings) -> bool:
    local = now.astimezone(ZoneInfo(settings.timezone))
    if settings.skip_weekends and local.weekday() >= 5:
        return False
    return settings.send_window_start_hour <= local.hour < settings.send_window_end_hour


def eligible(lead: Lead, settings: OutreachSettings) -> tuple[bool, str]:
    if lead.company_type != CompanyType.BUYER:
        return False, "not a buyer"
    if not lead.outreach_ready:
        return False, "not outreach-ready"
    if not lead.contact_email or lead.email_status not in (EmailStatus.MX_VALID, EmailStatus.GENERIC, EmailStatus.DELIVERABLE):
        return False, "no validated email"
    if lead.sequence_status != SequenceStatus.NOT_QUEUED:
        return False, f"already {lead.sequence_status.value}"
    return True, "ok"


def enqueue(db: Database, campaign_id: str, settings: OutreachSettings, ledger: Ledger,
            now: datetime | None = None) -> list[Lead]:
    now = now or utcnow()
    queued: list[Lead] = []
    owner_id = db.campaign_owner(campaign_id)  # scope suppression checks to this campaign's tenant
    for lead in db.list_leads(campaign_id, outreach_ready=True):
        ok, why = eligible(lead, settings)
        if not ok:
            log.debug("skip %s: %s", lead.company_name, why)
            continue
        email = lead.contact_email
        if db.is_suppressed(lead.domain, email, owner_id=owner_id) or ledger.is_stopped(email):
            lead.sequence_status = SequenceStatus.SUPPRESSED
            db.update_lead(lead)
            continue
        if ledger.has_sent(email, "email_1"):
            # DB lost the state but the ledger remembers: rebuild from the ledger.
            _restore_from_ledger(lead, ledger, settings)
            db.update_lead(lead)
            continue
        lead.sequence_status = SequenceStatus.QUEUED
        lead.next_contact_at = now
        db.update_lead(lead)
        db.add_event(lead.lead_id, "queued")
        queued.append(lead)
    log.info("queued %d lead(s) for %s", len(queued), campaign_id)
    return queued


def _restore_from_ledger(lead: Lead, ledger: Ledger, settings: OutreachSettings) -> None:
    steps = ledger.steps_sent(lead.contact_email)
    order = ["email_1", "followup_1", "followup_2"]
    done = [s for s in order if s in steps]
    last = done[-1]
    lead.thread_message_id = steps["email_1"].get("message_id")
    lead.last_sent_at = datetime.fromisoformat(steps[last]["at"])
    lead.email_1_sent_at = datetime.fromisoformat(steps["email_1"]["at"])
    if "followup_1" in steps:
        lead.followup_1_at = datetime.fromisoformat(steps["followup_1"]["at"])
    if "followup_2" in steps:
        lead.followup_2_at = datetime.fromisoformat(steps["followup_2"]["at"])
        lead.sequence_status = SequenceStatus.FOLLOWUP_2_SENT
        return
    lead.sequence_status = SequenceStatus.FOLLOWUP_1_SENT if last == "followup_1" else SequenceStatus.EMAIL_1_SENT
    lead.next_contact_at = _next_contact(lead.sequence_status, lead.last_sent_at, settings)


def _next_contact(status: SequenceStatus, sent_at: datetime, settings: OutreachSettings) -> datetime | None:
    if status == SequenceStatus.EMAIL_1_SENT:
        return sent_at + timedelta(days=settings.followup_1_after_days)
    if status == SequenceStatus.FOLLOWUP_1_SENT:
        return sent_at + timedelta(days=settings.followup_2_after_days)
    return None


def due_leads(db: Database, campaign_id: str, now: datetime | None = None) -> list[Lead]:
    now = now or utcnow()
    out: list[Lead] = []
    for lead in db.leads_by_status(campaign_id, [s.value for s in ACTIVE]):
        # A QUEUED lead is Email 1 - reviewable the moment it is queued; next_contact_at
        # gating exists for follow-ups (wait N days after a send), not the first touch. So
        # a queued lead is always due, regardless of any next_contact_at value on it; only
        # sent-and-waiting statuses are held until their next_contact_at passes.
        due = lead.sequence_status == SequenceStatus.QUEUED or lead.next_contact_at is None or lead.next_contact_at <= now
        if due:
            out.append(lead)
    # Follow-ups first (a promise inside an existing thread, oldest due first), then new
    # conversations with whatever budget is left.
    out.sort(key=lambda l: (1 if l.sequence_status == SequenceStatus.QUEUED else 0, l.next_contact_at or now))
    return out


def stop_lead(db: Database, lead: Lead, status: SequenceStatus, reason: str, ledger: Ledger | None = None) -> None:
    lead.sequence_status = status
    lead.outreach_ready = False
    lead.next_contact_at = None
    if status == SequenceStatus.REPLIED:
        lead.reply_status = reason
    db.update_lead(lead)
    db.add_event(lead.lead_id, status.value, detail=reason)
    if status in (SequenceStatus.UNSUBSCRIBED, SequenceStatus.BOUNCED) and lead.contact_email:
        # Scope the suppression to the campaign's tenant so an unsubscribe/bounce in one
        # account's outreach does not silence that address for every other account.
        db.add_suppression(lead.contact_email, "email", reason,
                           owner_id=db.campaign_owner(lead.campaign_id))
        if ledger:
            ledger.record_stop(lead.contact_email, status.value)


def send_due(db: Database, campaign: CampaignConfig, settings: OutreachSettings, templates: Templates,
             sender_or_pool, ledger: Ledger, *, limit: int | None = None, now: datetime | None = None,
             sleep=time.sleep, ignore_window: bool = False) -> SendReport:
    """Send what is due. `sender_or_pool` is a MailboxPool, or a single Sender (wrapped
    into a one-mailbox pool). Email 1 uses the least-loaded mailbox; follow-ups use the
    mailbox that sent Email 1, and wait if that mailbox is paused or out of budget."""
    from gtm_engine.outreach.mailboxes import MailboxPool
    report = SendReport()
    now = now or utcnow()
    if not ignore_window and not in_send_window(now, settings):
        report.stopped_reason = "outside send window"
        return report

    day = now.strftime("%Y-%m-%d")
    pool = sender_or_pool if isinstance(sender_or_pool, MailboxPool) else _single_pool(sender_or_pool, settings)
    states = pool.states(db, campaign.campaign_id, ledger, day)
    report.mailboxes = {a: st.as_dict() for a, st in states.items()}
    if all(st.paused_reason for st in states.values()):
        report.stopped_reason = "all mailboxes paused: " + "; ".join(
            f"{a}: {st.paused_reason}" for a, st in states.items())
        return report
    if all(st.remaining == 0 for st in states.values()):
        report.stopped_reason = _limit_reason(states, settings)
        return report

    batch_left = limit if limit is not None else 10**9
    consecutive_failures = 0
    owner_id = db.campaign_owner(campaign.campaign_id)  # scope suppression checks to this tenant
    for lead in due_leads(db, campaign.campaign_id, now):
        if batch_left <= 0:
            report.stopped_reason = "batch limit reached"
            break
        if all(st.remaining == 0 for st in states.values()):
            report.stopped_reason = _limit_reason(states, settings)
            break
        step, next_status = NEXT_STEP[lead.sequence_status]
        email = lead.contact_email
        if db.is_suppressed(lead.domain, email, owner_id=owner_id) or ledger.is_stopped(email):
            stop_lead(db, lead, SequenceStatus.SUPPRESSED, "suppressed before send", ledger)
            report.skipped += 1
            continue
        if ledger.has_sent(email, step):
            log.warning("%s already received %s per ledger; advancing without sending", email, step)
            _advance(db, lead, step, next_status, ledger.steps_sent(email)[step].get("message_id"), settings)
            report.skipped += 1
            continue

        draft = db.get_draft(lead.lead_id, step)
        if settings.require_approval:
            if not draft or draft["status"] != "approved":
                report.skipped += 1
                report.details.append(f"{lead.company_name}: {step} awaiting human approval")
                continue
            subject, body = draft["subject"], draft["body"]
        elif draft and draft["status"] == "approved":
            subject, body = draft["subject"], draft["body"]
        elif draft and draft["status"] == "rejected":
            report.skipped += 1
            continue
        else:
            rendered = render(step, lead, campaign, settings, templates)
            subject, body = rendered.subject, rendered.body

        # --- choose the mailbox -------------------------------------------------
        if step == "email_1":
            state = pool.pick_for_new_thread(states)
            if state is None:
                report.stopped_reason = _limit_reason(states, settings)
                break
        else:
            owner = lead.mailbox or ledger.mailbox_of(email) or pool.addresses()[0]
            state = states.get(owner)
            if state is None or state.remaining == 0:
                why = "not configured" if state is None else (state.paused_reason or "daily cap reached")
                report.skipped += 1
                report.details.append(f"{lead.company_name}: {step} waits for {owner} ({why})")
                continue
        mailbox = state.mailbox.address
        sender = pool.sender_for(mailbox)

        result = sender.send(OutgoingEmail(
            to=email, subject=subject, body=body,
            in_reply_to=lead.thread_message_id if step != "email_1" else None,
            lead_id=lead.lead_id, step=step,
        ))
        if not result.ok:
            report.failed += 1
            consecutive_failures += 1
            db.add_event(lead.lead_id, "send_failed", step=step, detail=f"{mailbox}: {result.error}")
            report.details.append(f"{lead.company_name}: {result.error}")
            if result.error and result.error.startswith("recipient_refused"):
                stop_lead(db, lead, SequenceStatus.BOUNCED, result.error, ledger)
            if consecutive_failures >= 3:
                report.stopped_reason = "three consecutive send failures; check credentials/limits"
                break
            continue

        consecutive_failures = 0
        if draft:
            db.set_draft_status(lead.lead_id, step, "sent")
        ledger.record_sent(email, step, result.message_id, lead.lead_id, now, mailbox=mailbox)
        ledger.note_send_day(mailbox, day)
        if step == "email_1":
            lead.mailbox = mailbox
        _advance(db, lead, step, next_status, result.message_id, settings, now)
        db.add_event(lead.lead_id, "sent", step=step, detail=f"{mailbox} {result.message_id}")
        state.sent_today += 1
        batch_left -= 1
        report.sent += 1
        report.details.append(f"{lead.company_name} <{email}>: {step} via {mailbox}")
        if batch_left > 0 and any(st.remaining > 0 for st in states.values()):
            sleep(random.uniform(settings.jitter_min_s, settings.jitter_max_s)
                  if settings.jitter_max_s > 0 else settings.delay_between_sends_s)
    report.mailboxes = {a: st.as_dict() for a, st in states.items()}
    return report


def _single_pool(sender, settings: OutreachSettings):
    """Wrap a bare Sender (tests, legacy callers) into a one-mailbox pool."""
    from gtm_engine.outreach.mailboxes import Mailbox, MailboxPool
    address = getattr(sender, "from_addr", None) or settings.smtp_user or "dryrun@example.invalid"
    pool = MailboxPool([Mailbox(address=address, password="x")], settings, outbox=__import__("pathlib").Path("."),
                       dry_run=False, sender_factory=lambda box, dry: sender)
    return pool


def _limit_reason(states, settings: OutreachSettings) -> str:
    parts = []
    for a, st in states.items():
        tag = f"{st.sent_today}/{st.cap}"
        if settings.warmup_enabled:
            tag += f", warm-up day {st.days_active or 1}"
        parts.append(f"{a}: {tag}")
    return "daily limit reached (" + "; ".join(parts) + ")"


def _advance(db: Database, lead: Lead, step: str, next_status: SequenceStatus, message_id: str | None,
             settings: OutreachSettings, now: datetime | None = None) -> None:
    now = now or utcnow()
    if step == "email_1":
        lead.email_1_sent_at = now
        lead.thread_message_id = message_id
    elif step == "followup_1":
        lead.followup_1_at = now
    else:
        lead.followup_2_at = now
    lead.last_sent_at = now
    lead.sequence_status = next_status
    lead.next_contact_at = _next_contact(lead.sequence_status, now, settings)
    db.update_lead(lead)


def prepare_drafts(db: Database, campaign: CampaignConfig, settings: OutreachSettings, templates: Templates,
                   now: datetime | None = None) -> list[dict]:
    """Render a pending draft for every due lead that has none yet, and return the review
    queue: one row per due (lead, step) with the current draft and its status."""
    queue: list[dict] = []
    for lead in due_leads(db, campaign.campaign_id, now):
        # One lead that fails to render (an unexpected template/data edge) must not blank
        # the whole review queue - skip it, log it, and keep serving the rest.
        try:
            step, _ = NEXT_STEP[lead.sequence_status]
            draft = db.get_draft(lead.lead_id, step)
            if draft is None:
                rendered = render(step, lead, campaign, settings, templates)
                draft = db.upsert_draft(lead.lead_id, step, rendered.subject, rendered.body)
            queue.append({"lead": lead, "step": step, "draft": draft})
        except Exception:
            log.exception("could not prepare a draft for %s (%s)", lead.company_name, lead.lead_id)
    return queue
