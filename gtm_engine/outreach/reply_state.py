"""Reply, bounce and unsubscribe detection over IMAP (Gmail). Runs before every send
batch so a lead who answered yesterday never gets today's follow-up."""

from __future__ import annotations

import email
import imaplib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr

from gtm_engine.models import Lead, SequenceStatus
from gtm_engine.outreach.config import OutreachSettings
from gtm_engine.outreach.gmail_oauth import AccessTokenProvider, credentials_from_env, xoauth2_string
from gtm_engine.outreach.ledger import Ledger
from gtm_engine.outreach.reply_classifier import classify, strip_quoted
from gtm_engine.llm.tasks import classify_reply as classify_reply_llm
from gtm_engine.outreach.sequencer import ACTIVE, stop_lead
from gtm_engine.storage.database import Database

log = logging.getLogger(__name__)

_STOP_RE = re.compile(r"\b(stop|unsubscribe|remove me|opt[ -]?out|don'?t (email|contact) me)\b", re.I)
_BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail delivery", "delivery status")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@dataclass
class InboundMessage:
    from_addr: str
    subject: str
    body: str
    in_reply_to: str | None = None
    references: str = ""


@dataclass
class SyncReport:
    replied: int = 0
    unsubscribed: int = 0
    bounced: int = 0
    scanned: int = 0
    interested: int = 0
    not_interested: int = 0
    out_of_office: int = 0
    wrong_person: int = 0
    auto_reply: int = 0
    details: list[str] = field(default_factory=list)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - malformed headers are common in bounces
        return value


def _text_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return ""
    payload = msg.get_payload(decode=True) or b""
    return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")


def _is_bounce(m: InboundMessage) -> bool:
    haystack = f"{m.from_addr} {m.subject}".lower()
    return any(s in haystack for s in _BOUNCE_SENDERS) or "undeliverable" in haystack


def parse_message(raw: bytes) -> InboundMessage:
    msg = email.message_from_bytes(raw)
    return InboundMessage(
        from_addr=parseaddr(_decode(msg.get("From")))[1].lower(),
        subject=_decode(msg.get("Subject")),
        body=_text_body(msg)[:5000],
        in_reply_to=(msg.get("In-Reply-To") or "").strip() or None,
        references=msg.get("References") or "",
    )


def _imap_login(conn: imaplib.IMAP4_SSL, box) -> None:
    if box.oauth:
        token = AccessTokenProvider(*box.oauth).token()
        conn.authenticate("XOAUTH2", lambda _: xoauth2_string(box.address, token).encode())
    else:
        conn.login(box.address, box.password)


def fetch_recent(settings: OutreachSettings, since_days: int = 14, box=None) -> list[InboundMessage]:
    """Inbox of one mailbox (default: every configured mailbox, concatenated)."""
    from gtm_engine.outreach.mailboxes import load_mailboxes
    boxes = [box] if box is not None else [b for b in load_mailboxes() if b.can_send]
    if not boxes:
        log.warning("no credentials: skipping reply sync")
        return []
    out: list[InboundMessage] = []
    for b in boxes:
        out.extend(_fetch_one(settings, b, since_days))
    return out


def _fetch_one(settings: OutreachSettings, box, since_days: int) -> list[InboundMessage]:
    since = (datetime.now() - timedelta(days=since_days)).strftime("%d-%b-%Y")
    out: list[InboundMessage] = []
    with imaplib.IMAP4_SSL(settings.imap_host) as conn:
        _imap_login(conn, box)
        conn.select("INBOX", readonly=True)
        status, data = conn.search(None, f'(SINCE "{since}")')
        if status != "OK":
            return out
        for num in data[0].split():
            status, parts = conn.fetch(num, "(RFC822)")
            if status == "OK" and parts and isinstance(parts[0], tuple):
                out.append(parse_message(parts[0][1]))
    return out


def await_sync(coro):
    """Run a coroutine from sync code (the sync path is synchronous by design)."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    return loop.run_until_complete(coro) if not loop.is_running() else None


def apply_inbound(db: Database, campaign_id: str, messages: list[InboundMessage],
                  ledger: Ledger | None = None, now: datetime | None = None, llm=None) -> SyncReport:
    """Match inbound mail to active leads by sender address or by thread id, classify the
    reply, and act: stop, suppress, postpone, or record a referral."""
    from gtm_engine.models import utcnow
    now = now or utcnow()
    report = SyncReport(scanned=len(messages))
    active: list[Lead] = db.leads_by_status(campaign_id, [s.value for s in ACTIVE])
    by_email = {l.contact_email.lower(): l for l in active if l.contact_email}
    by_thread = {l.thread_message_id: l for l in active if l.thread_message_id}
    # A bounce notice quotes our thread, so it must be recognised before thread matching.
    # Leads already stopped as REPLIED are re-checked here in case a bounce was mistaken
    # for a reply earlier (self-healing).
    replied = db.leads_by_status(campaign_id, [SequenceStatus.REPLIED.value])
    bounce_candidates = {**{l.contact_email.lower(): l for l in replied if l.contact_email}, **by_email}
    bounce_threads = {**{l.thread_message_id: l for l in replied if l.thread_message_id}, **by_thread}

    for m in messages:
        if _is_bounce(m):
            lead = None
            for addr in _EMAIL_RE.findall(m.body):
                if addr.lower() in bounce_candidates:
                    lead = bounce_candidates[addr.lower()]
                    break
            if lead is None and m.in_reply_to:
                lead = bounce_threads.get(m.in_reply_to)
            if lead is None:
                lead = next((l for mid, l in bounce_threads.items() if mid in m.references), None)
            if lead is not None and lead.sequence_status not in (SequenceStatus.BOUNCED, SequenceStatus.UNSUBSCRIBED):
                stop_lead(db, lead, SequenceStatus.BOUNCED, "bounce notification", ledger)
                report.bounced += 1
                report.details.append(f"{lead.company_name}: bounced")
                by_email.pop((lead.contact_email or "").lower(), None)
                by_thread.pop(lead.thread_message_id, None)
            continue

        lead = by_email.get(m.from_addr)
        if lead is None and m.in_reply_to:
            lead = by_thread.get(m.in_reply_to)
        if lead is None:
            lead = next((l for mid, l in by_thread.items() if mid in m.references), None)
        if lead is None:
            continue

        c = classify(m.subject, m.body, own_email=lead.mailbox, sender=m.from_addr, now=now)
        if c.label == "reply" and llm is not None:
            second = await_sync(classify_reply_llm(llm, m.subject, strip_quoted(m.body)))
            if second and second != "reply":
                c.label, c.matched = second, f"llm:{llm.name}"
        excerpt = " ".join(strip_quoted(m.body).split())[:300]
        lead.reply_label = c.label
        lead.reply_excerpt = excerpt or None
        detail = f"{c.label}" + (f" ({c.matched})" if c.matched else "")

        if c.label == "auto_reply":
            # An acknowledgement is not an answer: the sequence carries on.
            report.auto_reply += 1
            db.update_lead(lead)
            db.add_event(lead.lead_id, "auto_reply", detail=detail)
            report.details.append(f"{lead.company_name}: auto-reply ignored")
            continue

        if c.label == "out_of_office":
            # Push the next step past the return date (or a week) instead of stopping.
            resume = (c.return_date or now + timedelta(days=7)) + timedelta(days=1)
            if lead.next_contact_at is None or resume > lead.next_contact_at:
                lead.next_contact_at = resume
            report.out_of_office += 1
            db.update_lead(lead)
            db.add_event(lead.lead_id, "out_of_office", detail=f"resume {resume:%Y-%m-%d}" + (f" ({c.matched})" if c.matched else ""))
            report.details.append(f"{lead.company_name}: out of office, next step {resume:%Y-%m-%d}")
            continue

        if c.label == "unsubscribe":
            stop_lead(db, lead, SequenceStatus.UNSUBSCRIBED, "asked to stop", ledger)
            report.unsubscribed += 1
        elif c.label == "not_interested":
            stop_lead(db, lead, SequenceStatus.REPLIED, f"not interested: {c.matched}", ledger)
            db.add_suppression(lead.contact_email, "email", "replied not interested",
                               owner_id=db.campaign_owner(lead.campaign_id))
            report.not_interested += 1
        elif c.label == "wrong_person":
            if c.referred_email:
                lead.referred_contact = {"name": c.referred_name, "email": c.referred_email, "status": "pending"}
            stop_lead(db, lead, SequenceStatus.REPLIED, f"wrong person: {c.matched}"
                      + (f"; referred to {c.referred_email}" if c.referred_email else ""), ledger)
            report.wrong_person += 1
        elif c.label == "interested":
            stop_lead(db, lead, SequenceStatus.REPLIED, f"interested: {c.matched}", ledger)
            report.interested += 1
        else:
            stop_lead(db, lead, SequenceStatus.REPLIED, f"replied: {m.subject[:80]}", ledger)
        report.replied += 1 if c.label in ("interested", "reply", "wrong_person", "not_interested") else 0
        db.add_event(lead.lead_id, "reply_classified", detail=detail)
        report.details.append(f"{lead.company_name}: {c.label}" + (f" -> {c.referred_email}" if c.referred_email else ""))
        # A lead stops once; drop it from further matching in this batch.
        by_email.pop(lead.contact_email.lower(), None)
        if lead.thread_message_id:
            by_thread.pop(lead.thread_message_id, None)
    return report


def sync_replies(db: Database, campaign_id: str, settings: OutreachSettings, ledger: Ledger | None = None) -> SyncReport:
    return apply_inbound(db, campaign_id, fetch_recent(settings), ledger)


def verify_sent(settings: OutreachSettings, ledger: Ledger, folder: str = '"[Gmail]/Sent Mail"') -> list[tuple[str, str, bool]]:
    """Confirm each ledger Message-ID exists in the Sent folder of the mailbox that sent it.
    Returns (email, step, found)."""
    from gtm_engine.outreach.mailboxes import load_mailboxes
    boxes = {b.address: b for b in load_mailboxes() if b.can_send}
    results: list[tuple[str, str, bool]] = []
    if not boxes:
        return results
    default = next(iter(boxes))
    by_box: dict[str, list[tuple[str, str, str]]] = {}
    for addr, steps in ledger.data["sent"].items():
        for step, rec in steps.items():
            by_box.setdefault(rec.get("mailbox") or default, []).append((addr, step, rec.get("message_id") or ""))
    for owner, items in by_box.items():
        box = boxes.get(owner)
        if box is None:
            results.extend((a, s, False) for a, s, _ in items)
            continue
        with imaplib.IMAP4_SSL(settings.imap_host) as conn:
            _imap_login(conn, box)
            status, _ = conn.select(folder, readonly=True)
            if status != "OK":
                raise RuntimeError(f"cannot open {folder} on {owner}")
            for addr, step, mid in items:
                if not mid:
                    results.append((addr, step, False))
                    continue
                st, data = conn.search(None, "HEADER", "Message-ID", mid)
                results.append((addr, step, st == "OK" and bool(data and data[0])))
    return results
