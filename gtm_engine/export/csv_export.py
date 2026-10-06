"""CSV export in the exact column order the spec requires. UTF-8 with BOM so Excel on
Windows opens Urdu/Arabic company names correctly."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Iterable

from gtm_engine.enrichment.fieldclean import clean_reason, humanize_industry
from gtm_engine.models import CSV_COLUMNS, Lead


# Leading characters a spreadsheet treats as the start of a formula. A scraped company name
# like `=cmd|'/c calc'!A1` or `+HYPERLINK(...)` would otherwise execute on open in Excel/Sheets
# (CSV injection). Prefixing a single quote makes the cell display as literal text.
_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _sanitize(text: str) -> str:
    return "'" + text if text[:1] in _FORMULA_TRIGGERS else text


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, list):
        return _sanitize("; ".join(str(v) for v in value))
    return _sanitize(str(value))


def lead_row(lead: Lead) -> dict[str, str]:
    # mode="json" renders enums as their .value ("BUYER", not "CompanyType.BUYER") and
    # datetimes as ISO strings, so the full export never leaks Python repr noise.
    data = lead.model_dump(mode="json")
    return {col: _cell(data.get(col)) for col in CSV_COLUMNS}


# -- Clean, client-ready export --------------------------------------------------------
# A sheet a non-technical reader can act on: business columns only, human headers,
# readable values. Internal plumbing (ids, sub-scores, sequence timestamps) is omitted.

CLEAN_COLUMNS: list[str] = [
    "Company", "Website", "City", "Address", "Category", "Type", "Score", "Priority",
    "Summary", "Why it qualified", "Signals", "Contact", "Role", "Email",
    "Email status", "Phone", "LinkedIn", "Source",
]

_EMAIL_STATUS_LABELS = {
    "mx_valid": "Valid (domain accepts mail)",
    "generic": "Generic mailbox (info@/sales@)",
    "deliverable": "Verified deliverable",
    "candidate": "Guessed – unconfirmed",
    "unverified": "Unverified",
    "risky": "Risky / catch-all",
    "invalid": "Invalid",
    "bounced": "Bounced",
    "none": "",
}


def _enum_val(v) -> str:
    return str(getattr(v, "value", v) or "")


def clean_row(lead: Lead) -> dict[str, str]:
    return {
        "Company": lead.company_name or "",
        "Website": lead.website or "",
        "City": lead.city or "",
        "Address": lead.address or "",
        "Category": humanize_industry(lead.industry),
        "Type": _enum_val(lead.company_type),
        "Score": str(lead.total_score),
        "Priority": (_enum_val(lead.priority)).replace("_", " "),
        "Summary": lead.research_brief or lead.company_description or "",
        "Why it qualified": clean_reason(lead.score_reason),
        "Signals": lead.personalization_hook or "",
        "Contact": lead.contact_name or "",
        "Role": lead.contact_role or "",
        "Email": lead.contact_email or "",
        "Email status": _EMAIL_STATUS_LABELS.get(_enum_val(lead.email_status), _enum_val(lead.email_status)),
        "Phone": lead.phone or "",
        "LinkedIn": lead.linkedin_or_public_profile_url or "",
        "Source": lead.source or "",
    }


def write_clean_csv(leads: Iterable[Lead], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CLEAN_COLUMNS)
        writer.writeheader()
        for lead in leads:
            writer.writerow({k: _cell(v) for k, v in clean_row(lead).items()})
    return path


def write_csv(leads: Iterable[Lead], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for lead in leads:
            writer.writerow(lead_row(lead))
    return path


def export_path(export_dir: Path, campaign_id: str, run_id: str, qualified_only: bool) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = "qualified" if qualified_only else "all"
    return export_dir / f"{campaign_id}_{stamp}_{run_id}_{suffix}.csv"
