"""FastAPI backend for the web UI. Every route except /health requires a valid Supabase
bearer token (see api/auth.py). Multi-tenant: campaigns are owned by the account that
created them, and campaign- and lead-scoped routes are guarded by require_campaign_access /
require_lead_access so one account never sees another's data (file-based example campaigns
and legacy NULL-owner campaigns stay shared; with auth off there is no scoping). Every send
goes through the human-approval queue: preview -> edit -> approve -> send."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from gtm_engine import __version__
from gtm_engine.api.auth import auth_disabled, current_user_email, current_user_id, verify_request
from gtm_engine.api.keys import ALLOWED_KEYS, decrypt_key, encrypt_key, encryption_available
from gtm_engine.api.usage import DEFAULT_LIMITS, MAX_LIMITS, check_usage, get_all_usage
from gtm_engine.config import CampaignConfig, load_campaign, load_defaults, load_settings, slugify_campaign_id
from gtm_engine.config.schema import GeographyConfig
from gtm_engine.config.loader import CONFIG_DIR, PROJECT_ROOT, is_serverless, runtime_dir
from gtm_engine.export.csv_export import export_path, write_clean_csv, write_csv
from gtm_engine.export import sheets as sheets_export
from gtm_engine.models import CompanyType, EmailStatus, SequenceStatus
from gtm_engine.outreach.cli import ledger_path
from gtm_engine.outreach.config import load_outreach_settings, load_templates
from gtm_engine.outreach.ledger import Ledger
from gtm_engine.outreach.reply_state import sync_replies
from gtm_engine.outreach.mailboxes import MailboxPool, load_mailboxes, mailboxes_from_db
from gtm_engine.outreach.sequencer import ACTIVE, enqueue, prepare_drafts, send_due, stop_lead
from gtm_engine.outreach.templates import render
from gtm_engine.storage.database import Database

log = logging.getLogger(__name__)
# Applied app-wide rather than per-route: a route added later is then protected by default
# instead of open by default. auth.PUBLIC_PATHS names the only exceptions.
#
# The docs are switched off rather than guarded. FastAPI registers /docs and /openapi.json
# in its own setup(), which runs outside these dependencies - they stayed public on the
# first attempt, publishing all 35 routes. Guarding them would not help much either: a
# browser hitting /docs cannot send a bearer header. So they exist only in local dev,
# where auth is explicitly disabled.
_docs = {} if auth_disabled() else {"docs_url": None, "redoc_url": None, "openapi_url": None}
app = FastAPI(title="GTM Lead Engine", version=__version__,
              dependencies=[Depends(verify_request)], **_docs)
_extra_origins = [o.strip() for o in os.environ.get("GTM_CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", *_extra_origins],
    allow_methods=["*"], allow_headers=["*"],
)

_settings = load_settings()
_defaults = load_defaults()
CAMPAIGN_DIR = CONFIG_DIR / "campaigns"


def _db() -> Database:
    return Database(_settings.database_url)


def _read_only_ledger(campaign_id: str) -> Ledger:
    """The campaign's send ledger, loaded from the repo but never written back to it.

    The ledger file is committed by the GitHub Actions jobs, so the deployment bundle
    carries an up-to-date copy that is fine to read. Writing it is not: the bundle is
    read-only on Vercel, and `Ledger.save()` would raise OSError mid-request. Pointing
    writes at scratch keeps those endpoints working, and loses nothing - every state
    change they make is also written to Postgres, which is the real source of truth.
    Real sends, where the ledger genuinely matters as a double-send guard, happen in
    Actions against a writable checkout.
    """
    ledger = Ledger(ledger_path(campaign_id))
    if is_serverless():
        ledger.path = runtime_dir() / f"{campaign_id}_ledger.json"
    return ledger


def dispatch_workflow(workflow_file: str, inputs: dict[str, str]) -> None:
    """Trigger a GitHub Actions workflow_dispatch run. Requires GTM_GITHUB_TOKEN (a repo-scoped
    PAT or fine-grained token with 'actions: write') and GTM_GITHUB_REPO ('owner/repo')."""
    token = os.environ.get("GTM_GITHUB_TOKEN")
    repo = os.environ.get("GTM_GITHUB_REPO")
    # Name the ones actually missing. The old message listed both whatever the cause,
    # which sends you checking a variable that was set correctly all along -- and on
    # Vercel the fix differs per variable (one is a PAT you must mint, the other a
    # one-line value), so "which" is the only useful part of this error.
    missing = [n for n, v in (("GTM_GITHUB_TOKEN", token), ("GTM_GITHUB_REPO", repo)) if not v]
    if missing:
        raise HTTPException(
            500,
            f"{' and '.join(missing)} not set; cannot dispatch long-running jobs. "
            "On Vercel, adding an environment variable only takes effect after a redeploy.",
        )
    resp = httpx.post(
        f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/dispatches",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"ref": os.environ.get("GTM_GITHUB_REF", "main"), "inputs": inputs},
        timeout=15.0,
    )
    if resp.status_code >= 300:
        raise HTTPException(502, f"GitHub workflow dispatch failed ({resp.status_code}): {resp.text}")


# A run that has not reported in this long is treated as dead rather than active. The
# Actions job can die without ever writing "failed" - bad input, cancelled run, runner
# failure - and the row it left behind would otherwise block every future dispatch for
# that campaign, permanently, with no way to clear it from the UI. The pipeline reports
# after each company, so a healthy run is never this quiet.
_STALE_AFTER = timedelta(minutes=15)


def _run_is_active(live: dict | None) -> bool:
    if not live or live.get("stage") in (None, "completed", "failed"):
        return False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(live["updated_at"])
    except (KeyError, TypeError, ValueError):
        return True  # no usable timestamp: assume live, since double-dispatching is worse
    return age < _STALE_AFTER


def _workflow_campaign_path(campaign_id: str) -> str:
    """The campaign YAML as a path the *Actions runner* can open.

    _campaign_files() yields absolute paths rooted at this process's PROJECT_ROOT, which
    on Vercel is /var/task. The runner resolves the input against its own checkout, so an
    absolute path from here points at nothing there and `gtm run` dies on a missing file.
    The workflow's own default is repo-relative, so match that.
    """
    return _campaign_files()[campaign_id].relative_to(PROJECT_ROOT).as_posix()


def _campaign_files() -> dict[str, Path]:
    out: dict[str, Path] = {}
    for path in sorted(CAMPAIGN_DIR.glob("*.yaml")):
        try:
            out[load_campaign(path).campaign_id] = path
        except Exception as exc:  # noqa: BLE001 - a broken YAML must not hide the others
            log.warning("skipping %s: %s", path.name, exc)
    return out


def _require_access(campaign_id: str, user_id: str | None) -> None:
    """Guard a campaign-scoped route. A file-based example is shared; a DB campaign is
    reachable by its owner (or anyone while it has no owner - legacy/shared). user_id is None
    when auth is off (local operator, tests), which sees everything, so nothing changes for
    single-operator use. A campaign the caller may not see is reported as 404, not 403, so its
    existence is not leaked."""
    # A NUL byte cannot be stored in a Postgres text column, so no id ever contains one:
    # reject it here as "not found" rather than letting psycopg raise a 500 mid-query.
    if "\x00" in campaign_id:
        raise HTTPException(404, f"campaign '{campaign_id}' not found")
    if user_id is None or campaign_id in _campaign_files():
        return
    db = _db()
    try:
        owner = db.campaign_owner(campaign_id)
        exists = owner is not None or db.campaign_config(campaign_id) is not None
    finally:
        db.close()
    if not exists:
        raise HTTPException(404, f"campaign '{campaign_id}' not found")
    if owner not in (None, user_id):
        raise HTTPException(404, f"campaign '{campaign_id}' not found")


def require_campaign_access(campaign_id: str, user_id: str | None = Depends(current_user_id)) -> None:
    """Route dependency: 404 unless the caller may see this campaign. Attached to every
    `/campaigns/{campaign_id}/...` route via `dependencies=[...]`, so a route added later is
    guarded by adding it there rather than by remembering to call the guard in the body."""
    _require_access(campaign_id, user_id)


def require_lead_access(lead_id: str, user_id: str | None = Depends(current_user_id)) -> None:
    """Route dependency for `/leads/{lead_id}/...`: resolve the lead's campaign, then apply the
    same ownership check. A lead the caller may not see is 404, so its existence is not leaked."""
    if "\x00" in lead_id:
        raise HTTPException(404, "lead not found")
    if user_id is None:
        return
    db = _db()
    try:
        campaign_id = db.campaign_of_lead(lead_id)
    finally:
        db.close()
    if campaign_id is None:
        raise HTTPException(404, "lead not found")
    _require_access(campaign_id, user_id)


def _campaign(campaign_id: str) -> CampaignConfig:
    path = _campaign_files().get(campaign_id)
    if path:
        return load_campaign(path)
    db = _db()
    cfg = db.campaign_config(campaign_id)
    db.close()
    if not cfg:
        raise HTTPException(404, f"campaign '{campaign_id}' not found")
    return CampaignConfig.model_validate(cfg)


# -- health / campaigns -------------------------------------------------------------

# Free-tier product limits, applied per signed-in user (local/self-host operators with no
# auth are unlimited). Overridable so a managed/paid tier or a self-hoster can raise them.
FREE_MAX_CAMPAIGNS = int(os.environ.get("GTM_FREE_MAX_CAMPAIGNS", "3"))
FREE_MAX_LEADS_PER_CAMPAIGN = int(os.environ.get("GTM_FREE_MAX_LEADS_PER_CAMPAIGN", "10"))

# Master accounts exempt from every free-tier quota (unlimited campaigns + leads). Defaults to
# the project owner so it works without extra config; extend/override via GTM_UNLIMITED_EMAILS
# (comma-separated).
UNLIMITED_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("GTM_UNLIMITED_EMAILS", "ihaseebarshad10@gmail.com,hello@grydin.co").split(",")
    if e.strip()
}


def _is_unlimited(email: str | None) -> bool:
    return bool(email) and email.lower() in UNLIMITED_EMAILS


@app.get("/health")
def health() -> dict:
    # /health is unauthenticated (it is the deploy/uptime probe), so it reports only booleans.
    # Raw exception strings leak the DB host/port and internal stack traces to anyone on the
    # internet; the detail is logged server-side instead, where operators can read it.
    o = load_outreach_settings()
    auth_ok = bool(os.environ.get("GTM_SUPABASE_URL", "").strip())
    jwks_ok = False
    if auth_ok:
        try:
            from gtm_engine.api.auth import jwk_client
            jwk_client()
            jwks_ok = True
        except Exception as exc:
            log.warning("health: jwks unreachable: %s", exc)
    db_ok = False
    dsn = os.environ.get("GTM_DATABASE_URL", "")
    if dsn:
        try:
            db = _db()
            db.close()
            db_ok = True
        except Exception as exc:
            log.warning("health: database unreachable: %s", exc)
    return {"status": "ok", "version": __version__, "smtp_configured": o.credentials_present,
            "auth_mode": o.auth_mode, "require_approval": o.require_approval,
            "warmup": {"enabled": o.warmup_enabled, "start": o.warmup_start_per_day,
                       "step": o.warmup_step_per_day, "max": o.daily_limit},
            "limits": {"max_campaigns": FREE_MAX_CAMPAIGNS,
                       "max_leads_per_campaign": FREE_MAX_LEADS_PER_CAMPAIGN},
            "auth": {"supabase_url_set": auth_ok, "jwks_reachable": jwks_ok,
                     "auth_disabled": auth_disabled()},
            "encryption_available": encryption_available(),
            "database": {"connected": db_ok, "dsn_set": bool(dsn.strip())}}


@app.get("/settings/limits")
def my_limits(user_id: str | None = Depends(current_user_id),
              email: str | None = Depends(current_user_email)) -> dict:
    """The caller's effective quota. null means unlimited (local operator or master account)."""
    unlimited = user_id is None or _is_unlimited(email)
    return {
        "unlimited": unlimited,
        "max_campaigns": None if unlimited else FREE_MAX_CAMPAIGNS,
        "max_leads_per_campaign": None if unlimited else FREE_MAX_LEADS_PER_CAMPAIGN,
    }


def _campaign_summary(db: Database, c: CampaignConfig, file: str | None) -> dict:
    counts = db.campaign_counts(c.campaign_id, c.min_score)
    last_run = (db.list_runs(c.campaign_id) or [None])[0]
    # The offer's generated need-terms the last run actually used, so the UI can show what
    # the LLM derived rather than leaving it invisible in the logs.
    relevance_keywords: list[str] = []
    discovery_sectors: list[str] = []
    if last_run and last_run.get("stats_json"):
        try:
            last_stats = json.loads(last_run["stats_json"])
            relevance_keywords = last_stats.get("relevance_keywords") or []
            discovery_sectors = last_stats.get("discovery_sectors") or []
        except (ValueError, TypeError):
            relevance_keywords, discovery_sectors = [], []
    return {
        "campaign_id": c.campaign_id, "name": c.name, "offer": c.offer, "file": file,
        "cities": c.geography.cities, "countries": c.geography.countries,
        "provinces": c.geography.provinces, "areas": c.geography.areas,
        "relevance_keywords": relevance_keywords,
        "discovery_sectors": discovery_sectors,
        "min_score": c.min_score, "max_companies": c.max_companies,
        "leads": counts["leads"], "buyers": counts["buyers"],
        "qualified": counts["qualified"],
        "outreach_ready": counts["outreach_ready"],
        "last_run": last_run,
        "live": db.get_run_progress(c.campaign_id),
    }


@app.get("/campaigns")
def campaigns(user_id: str | None = Depends(current_user_id)) -> list[dict]:
    """File-based example campaigns (shared) plus the caller's own DB campaigns. A file wins
    if an id exists in both, so editing a shipped example on disk is not shadowed by a stale
    DB copy. With no signed-in user (local operator) every DB campaign is returned."""
    db = _db()
    hidden = db.hidden_campaign_ids()
    out, seen = [], set()
    for cid, path in _campaign_files().items():
        if cid in hidden:
            seen.add(cid)
            continue
        out.append(_campaign_summary(db, load_campaign(path), path.name))
        seen.add(cid)
    for row in db.list_campaigns(user_id):
        if row["campaign_id"] in seen:
            continue
        try:
            out.append(_campaign_summary(db, CampaignConfig.model_validate(row["config"]), None))
        except Exception as exc:  # noqa: BLE001 - one bad stored config must not hide the rest
            log.warning("skipping DB campaign %s: %s", row["campaign_id"], exc)
    db.close()
    return out


# Bounds on user-supplied campaign input: unbounded text/lists are a cost and DoS vector
# (stored, crawled, and fed to the LLM). These are generous – far above any real campaign – so
# they never bite a legitimate user, only a payload meant to abuse.
class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    offer: str = Field(min_length=1, max_length=4000)
    countries: list[str] = Field(default=[], max_length=20)
    provinces: list[str] = Field(default=[], max_length=50)
    cities: list[str] = Field(default=[], max_length=50)
    target_industries: list[str] = Field(default=[], max_length=50)
    buyer_keywords: list[str] = Field(default=[], max_length=200)
    osm_categories: list[str] = Field(default=[], max_length=200)
    overture_categories: list[str] = Field(default=[], max_length=200)
    min_score: int = Field(default=70, ge=0, le=100)
    max_companies: int = Field(default=60, ge=1, le=1000)


def _own_campaign_count(db: Database, user_id: str | None) -> int:
    """How many campaigns this user owns (DB campaigns scoped to them)."""
    if user_id is None:
        return 0
    return sum(1 for _ in db.list_campaigns(user_id))


def _cap_leads(max_companies: int | None, user_id: str | None, email: str | None = None) -> int | None:
    """On the free tier, never crawl more than FREE_MAX_LEADS_PER_CAMPAIGN per run.
    Local operators (user_id None) and master accounts are uncapped."""
    if user_id is None or _is_unlimited(email):
        return max_companies
    return min(max_companies or FREE_MAX_LEADS_PER_CAMPAIGN, FREE_MAX_LEADS_PER_CAMPAIGN)


@app.post("/campaigns", status_code=201)
def create_campaign(body: CampaignCreate, user_id: str | None = Depends(current_user_id),
                    email: str | None = Depends(current_user_email)) -> dict:
    """Create a user-defined campaign, stored in the DB (not a file) so it works on the
    read-only serverless filesystem and can be run by id. It is owned by the caller, so only
    they see it and its leads. The id is slugged from the name and de-duped globally."""
    if not body.name.strip() or not body.offer.strip():
        raise HTTPException(422, "name and offer are required")
    db = _db()
    if user_id is not None and not _is_unlimited(email) and _own_campaign_count(db, user_id) >= FREE_MAX_CAMPAIGNS:
        db.close()
        raise HTTPException(403, f"Free tier is limited to {FREE_MAX_CAMPAIGNS} campaigns. "
                                 "Delete one to create another.")
    # De-dupe the id against all campaigns (every owner + files), so ids stay globally unique
    # even though visibility is per-owner.
    existing = db.all_campaign_ids() | set(_campaign_files().keys())
    cid = slugify_campaign_id(body.name, existing)
    try:
        cfg = CampaignConfig(
            campaign_id=cid, name=body.name.strip(), offer=body.offer.strip(),
            geography=GeographyConfig(countries=body.countries, provinces=body.provinces, cities=body.cities),
            target_industries=body.target_industries, buyer_keywords=body.buyer_keywords,
            osm_categories=body.osm_categories, overture_categories=body.overture_categories,
            min_score=body.min_score, max_companies=_cap_leads(body.max_companies, user_id, email),
        )
    except Exception as exc:  # noqa: BLE001 - surface pydantic's message
        db.close()
        raise HTTPException(422, str(exc))
    db.upsert_campaign(cfg.campaign_id, cfg.name, cfg.model_dump(mode="json"), owner_id=user_id)
    db.close()
    return {"campaign_id": cfg.campaign_id, "name": cfg.name}


class CampaignNLRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    max_companies: int | None = Field(default=None, ge=1, le=1000)
    # Optional discovery hints from the NL form (shown when there is no Brave key). When given,
    # they pin discovery to exactly what the user asked for – the pipeline treats user-set map
    # categories as authoritative and never broadens past them (e.g. no pharmacies in a doctors
    # search). Bounded like every other user-supplied list.
    osm_categories: list[str] = Field(default=[], max_length=200)
    search_queries: list[str] = Field(default=[], max_length=200)


@app.post("/campaigns/nl", status_code=201)
async def create_campaign_nl(body: CampaignNLRequest, user_id: str | None = Depends(current_user_id),
                             email: str | None = Depends(current_user_email)) -> dict:
    """Create a campaign from natural-language description.

    Parses the text into a CampaignConfig (deterministic extraction + optional LLM
    refinement), saves it to the DB, and returns the interpreted config with an
    explanation of what was extracted."""
    if not body.text.strip():
        raise HTTPException(422, "text is required")
    from gtm_engine.campaign.nl_parser import build_campaign_from_nl
    from gtm_engine.llm.client import build_llm

    db = _db()
    if user_id is not None and not _is_unlimited(email) and _own_campaign_count(db, user_id) >= FREE_MAX_CAMPAIGNS:
        db.close()
        raise HTTPException(403, f"Free tier is limited to {FREE_MAX_CAMPAIGNS} campaigns. "
                                 "Delete one to create another.")
    existing = db.all_campaign_ids() | set(_campaign_files().keys())

    llm = build_llm(_settings) if _settings.enable_llm else None
    try:
        cfg, explanation = await build_campaign_from_nl(body.text, llm=llm, existing_ids=existing)
    except Exception as exc:  # noqa: BLE001
        db.close()
        raise HTTPException(422, str(exc))

    if body.max_companies is not None:
        cfg.max_companies = max(1, min(body.max_companies, 500))
    # Free tier: never store a cap above the per-campaign lead limit.
    cfg.max_companies = _cap_leads(cfg.max_companies, user_id, email) or cfg.max_companies
    explanation["max_companies"] = cfg.max_companies

    # Honour the user's explicit discovery hints: they pin the search scope so the pipeline
    # keeps it tight (user map categories win over derived ones) instead of re-broadening.
    osm = [c.strip() for c in body.osm_categories if c.strip()]
    queries = [q.strip() for q in body.search_queries if q.strip()]
    if osm:
        cfg.osm_categories = osm
        explanation["osm_categories"] = osm
    if queries:
        cfg.search_queries = queries
        explanation["search_queries"] = queries

    db.upsert_campaign(cfg.campaign_id, cfg.name, cfg.model_dump(mode="json"), owner_id=user_id)
    db.close()
    return {
        "campaign_id": cfg.campaign_id,
        "config": cfg.model_dump(mode="json"),
        "explanation": explanation,
        "status": "draft",
    }


@app.delete("/campaigns/{campaign_id}", status_code=204, response_class=Response, dependencies=[Depends(require_campaign_access)])
def delete_campaign(campaign_id: str) -> Response:
    """Delete a campaign. DB campaigns are removed; file-based ones are hidden."""
    db = _db()
    if campaign_id in _campaign_files():
        db.hide_campaign(campaign_id)
        db.close()
        return Response(status_code=204)
    if not db.campaign_config(campaign_id):
        db.close()
        raise HTTPException(404, f"campaign '{campaign_id}' not found")
    db.delete_campaign(campaign_id)
    db.close()
    return Response(status_code=204)


@app.get("/campaigns/{campaign_id}", dependencies=[Depends(require_campaign_access)])
def campaign_detail(campaign_id: str) -> dict:
    return _campaign(campaign_id).model_dump(mode="json")


class RunRequest(BaseModel):
    max_companies: int | None = Field(default=None, ge=1, le=1000)


@app.post("/campaigns/{campaign_id}/run", dependencies=[Depends(require_campaign_access)])
def run_campaign(campaign_id: str, req: RunRequest,
                 user_id: str | None = Depends(current_user_id),
                 email: str | None = Depends(current_user_email)) -> dict:
    """Dispatches the crawl to GitHub Actions (gather-leads.yml) rather than running it
    in-request: a full campaign run is minutes-to-hours, far past any serverless timeout.
    Progress lands in the run_progress table, written by the Actions runner."""
    campaign = _campaign(campaign_id)
    db = _db()
    live = db.get_run_progress(campaign_id)
    if _run_is_active(live):
        db.close()
        raise HTTPException(409, "a run is already in progress for this campaign")
    # Per-user daily cap on run dispatches (atomic). Local operators (user_id None) and master
    # accounts are unlimited; everyone else is backstopped against spamming workflow dispatches.
    if user_id is not None and not _is_unlimited(email) and not check_usage(db, user_id, "runs"):
        db.close()
        raise HTTPException(429, "Daily run limit reached – try again tomorrow.")
    db.close()
    # A file-based campaign is dispatched by its repo path; a user-created (DB) one by its
    # id, which the runner resolves from Postgres. Either way the runner's `gtm run` accepts it.
    campaign_input = _workflow_campaign_path(campaign_id) if campaign_id in _campaign_files() else campaign_id
    max_companies = _cap_leads(req.max_companies or campaign.max_companies, user_id, email)
    dispatch_workflow(
        "gather-leads.yml",
        {
            "campaign": campaign_input,
            "max_companies": str(max_companies),
            "user_id": user_id or "",
        },
    )
    db = _db()
    db.set_run_progress(campaign_id, None, "starting", 0, 0, "dispatched to GitHub Actions")
    ticket = db.get_run_progress(campaign_id)
    db.close()
    return ticket


@app.get("/campaigns/{campaign_id}/progress", dependencies=[Depends(require_campaign_access)])
def progress(campaign_id: str) -> dict:
    db = _db()
    live = db.get_run_progress(campaign_id)
    db.close()
    return live or {"stage": "idle"}


@app.get("/campaigns/{campaign_id}/stats", dependencies=[Depends(require_campaign_access)])
def stats(campaign_id: str) -> dict:
    campaign = _campaign(campaign_id)
    db = _db()
    row = db.campaign_stats(campaign_id, campaign.min_score)
    db.close()
    return row


# -- leads --------------------------------------------------------------------------------

@app.get("/campaigns/{campaign_id}/leads", dependencies=[Depends(require_campaign_access)])
def leads(campaign_id: str, min_score: int = 0, company_type: str | None = None,
          outreach_ready: bool | None = None, q: str | None = None, order: str = "score",
          limit: int = 50, offset: int = 0) -> dict:
    db = _db()
    total = db.count_leads(campaign_id, min_score=min_score, company_type=company_type,
                           outreach_ready=outreach_ready, q=q)
    rows = db.list_leads(campaign_id, min_score=min_score, company_type=company_type,
                         outreach_ready=outreach_ready, q=q, order=order, limit=limit, offset=offset)
    db.close()
    return {"items": [_lead_summary(l) for l in rows], "total": total}


def _lead_summary(l) -> dict:
    d = l.model_dump(mode="json")
    d.pop("evidence", None)
    return d


@app.get("/leads/{lead_id}", dependencies=[Depends(require_lead_access)])
def lead_detail(lead_id: str) -> dict:
    db = _db()
    l = db.get_lead(lead_id)
    if not l:
        db.close()
        raise HTTPException(404, "lead not found")
    events = db.events_for(lead_id)
    drafts = [db.get_draft(lead_id, s) for s in ("email_1", "followup_1", "followup_2")]
    db.close()
    d = l.model_dump(mode="json")
    d["events"] = events
    d["drafts"] = [x for x in drafts if x]
    return d


class SuppressRequest(BaseModel):
    reason: str | None = None


@app.post("/leads/{lead_id}/suppress", dependencies=[Depends(require_lead_access)])
def suppress(lead_id: str, req: SuppressRequest) -> dict:
    db = _db()
    l = db.get_lead(lead_id)
    if not l:
        db.close()
        raise HTTPException(404, "lead not found")
    if l.domain:
        db.add_suppression(l.domain, "domain", req.reason or "suppressed from UI")
    if l.contact_email:
        db.add_suppression(l.contact_email, "email", req.reason or "suppressed from UI")
    stop_lead(db, l, SequenceStatus.SUPPRESSED, req.reason or "suppressed from UI", _read_only_ledger(l.campaign_id))
    db.close()
    return {"ok": True}


# -- settings: suppressions, mailboxes, campaign YAML, sheets -----------------------------

@app.get("/suppressions")
def list_suppressions() -> list[dict]:
    db = _db()
    rows = db.list_suppressions()
    db.close()
    return rows


class SuppressionCreate(BaseModel):
    value: str
    reason: str | None = None


@app.post("/suppressions")
def add_suppression(req: SuppressionCreate) -> dict:
    value = req.value.strip().lower()
    if not value:
        raise HTTPException(422, "empty value")
    db = _db()
    db.add_suppression(value, "email" if "@" in value else "domain", req.reason or "added from UI")
    db.close()
    return {"ok": True, "value": value}


@app.delete("/suppressions/{value}")
def delete_suppression(value: str) -> dict:
    db = _db()
    removed = db.remove_suppression(value)
    db.close()
    if not removed:
        raise HTTPException(404, "not suppressed")
    return {"ok": True}


@app.get("/mailboxes")
def mailboxes(campaign_id: str | None = None, user_id: str | None = Depends(current_user_id)) -> list[dict]:
    osettings = load_outreach_settings()
    boxes = load_mailboxes()
    # Merge user's own mailboxes from the DB
    if user_id and encryption_available():
        db_tmp = _db()
        user_rows = db_tmp.list_user_mailboxes(user_id)
        db_tmp.close()
        if user_rows:
            db_tmp2 = _db()
            full_rows = [db_tmp2.get_user_mailbox(user_id, r["address"]) for r in user_rows]
            db_tmp2.close()
            user_boxes = mailboxes_from_db([r for r in full_rows if r], decrypt_fn=decrypt_key)
            env_addrs = {b.address for b in boxes}
            boxes.extend(b for b in user_boxes if b.address not in env_addrs)
    if not boxes:
        return []
    cid = campaign_id or next(iter(_campaign_files()), None)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db = _db()
    states = MailboxPool(boxes, osettings, runtime_dir() / "outbox").states(
        db, cid or "", _read_only_ledger(cid) if cid else Ledger(runtime_dir() / "no-campaign.json"), day)
    db.close()
    return [st.as_dict() for st in states.values()]


@app.get("/campaigns/{campaign_id}/yaml", dependencies=[Depends(require_campaign_access)])
def campaign_yaml(campaign_id: str) -> dict:
    path = _campaign_files().get(campaign_id)
    if path:
        return {"campaign_id": campaign_id, "file": path.name, "yaml": path.read_text(encoding="utf-8")}
    # A user-created campaign lives in the DB, not on disk: serialise its stored config to YAML
    # so it can be loaded and edited in the same editor as the shipped examples.
    import yaml as _yaml
    db = _db()
    cfg = db.campaign_config(campaign_id)
    db.close()
    if not cfg:
        raise HTTPException(404, f"campaign '{campaign_id}' not found")
    return {"campaign_id": campaign_id, "file": None,
            "yaml": _yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)}


class YamlBody(BaseModel):
    yaml: str


@app.post("/campaigns/validate")
def validate_campaign_yaml(body: YamlBody) -> dict:
    import yaml as _yaml
    try:
        data = _yaml.safe_load(body.yaml) or {}
        cfg = CampaignConfig.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - surface the validator's message verbatim
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "campaign_id": cfg.campaign_id, "name": cfg.name,
            "sources": [s for s, on in (("overture", bool(cfg.overture_categories)), ("osm", bool(cfg.osm_categories)),
                                        ("kcci", "kcci" in cfg.chamber_sources), ("seed_csv", bool(cfg.seed_csv))) if on]}


@app.put("/campaigns/{campaign_id}/yaml")
def save_campaign_yaml(campaign_id: str, body: YamlBody,
                       user_id: str | None = Depends(current_user_id)) -> dict:
    """Validate, then persist. A user campaign is stored in the DB (works on the read-only
    serverless filesystem and is per-owner), created if new and updated if it already exists;
    another owner's campaign is 404. A shipped example (a file) is written to disk in local dev
    and refused on the hosted app, where the bundle is read-only."""
    import yaml as _yaml
    try:
        cfg = CampaignConfig.model_validate(_yaml.safe_load(body.yaml) or {})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, str(exc))
    if cfg.campaign_id != campaign_id:
        raise HTTPException(422, f"campaign_id in YAML ({cfg.campaign_id}) must match the URL ({campaign_id})")
    if campaign_id in _campaign_files():
        if is_serverless():
            raise HTTPException(400, "shipped example campaigns are read-only in the hosted app; "
                                     "create a new campaign instead")
        path = _campaign_files()[campaign_id]
        path.write_text(body.yaml, encoding="utf-8")
        return {"ok": True, "file": path.name}
    db = _db()
    try:
        owner = db.campaign_owner(campaign_id)
        exists = owner is not None or db.campaign_config(campaign_id) is not None
        if exists and owner not in (None, user_id):
            raise HTTPException(404, f"campaign '{campaign_id}' not found")
        db.upsert_campaign(cfg.campaign_id, cfg.name, cfg.model_dump(mode="json"), owner_id=user_id)
    finally:
        db.close()
    return {"ok": True, "file": None, "campaign_id": cfg.campaign_id}


@app.get("/sheets/status")
def sheets_status() -> dict:
    return {"configured": sheets_export.configured(),
            "spreadsheet_id": (__import__("os").environ.get("GTM_SHEETS_SPREADSHEET_ID") or None)}


@app.post("/campaigns/{campaign_id}/export/sheets", dependencies=[Depends(require_campaign_access)])
def export_to_sheets(campaign_id: str, min_score: int = 70, buyers_only: bool = True) -> dict:
    if not sheets_export.configured():
        raise HTTPException(400, "Google Sheets not configured (GTM_SHEETS_CREDENTIALS_JSON, GTM_SHEETS_SPREADSHEET_ID)")
    db = _db()
    rows = db.list_leads(campaign_id, min_score=min_score, company_type=CompanyType.BUYER.value if buyers_only else None)
    db.close()
    try:
        return sheets_export.export_leads(rows, campaign_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"sheets export failed: {exc}")


class ReviewBody(BaseModel):
    verdict: str  # correct | wrong_company | wrong_person | wrong_email | clear


@app.post("/leads/{lead_id}/review", dependencies=[Depends(require_lead_access)])
def review_lead(lead_id: str, body: ReviewBody) -> dict:
    """The success metric the CEO asked for: a human says whether this lead is right."""
    if body.verdict not in ("correct", "wrong_company", "wrong_person", "wrong_email", "clear"):
        raise HTTPException(422, "verdict must be correct | wrong_company | wrong_person | wrong_email | clear")
    db = _db()
    l = db.get_lead(lead_id)
    if not l:
        db.close()
        raise HTTPException(404, "lead not found")
    l.review_verdict = None if body.verdict == "clear" else body.verdict
    l.reviewed_at = None if body.verdict == "clear" else datetime.now(timezone.utc)
    if body.verdict == "wrong_company":
        l.outreach_ready = False
    db.update_lead(l)
    db.add_event(lead_id, "reviewed", detail=body.verdict)
    db.close()
    return {"ok": True, "review_verdict": l.review_verdict}


class ReferralAction(BaseModel):
    accept: bool = True


@app.post("/leads/{lead_id}/referral", dependencies=[Depends(require_lead_access)])
def act_on_referral(lead_id: str, req: ReferralAction) -> dict:
    """Accept: the referred person becomes the contact and the lead re-enters the queue
    for a fresh Email 1 (which still needs approval). Decline: referral is dismissed."""
    db = _db()
    l = db.get_lead(lead_id)
    if not l or not l.referred_contact:
        db.close()
        raise HTTPException(404, "no pending referral")
    ref = dict(l.referred_contact)
    if not req.accept:
        ref["status"] = "declined"
        l.referred_contact = ref
        db.update_lead(l)
        db.add_event(lead_id, "referral_declined", detail=ref.get("email"))
        db.close()
        return {"ok": True, "referred_contact": ref}
    previous = {"name": l.contact_name, "role": l.contact_role, "email": l.contact_email}
    l.contact_name, l.contact_role = ref.get("name"), "Referred by previous contact"
    l.contact_email, l.email_status = ref["email"], EmailStatus.UNVERIFIED
    l.provenance["contact_email"] = f"referred by {previous['email']} in a reply"
    l.provenance["contact_name"] = f"referred by {previous['email']} in a reply"
    l.sequence_status, l.thread_message_id, l.mailbox = SequenceStatus.NOT_QUEUED, None, None
    l.email_1_sent_at = l.followup_1_at = l.followup_2_at = l.next_contact_at = None
    l.reply_label, l.reply_status = None, None
    l.outreach_ready = True
    ref["status"] = "accepted"
    l.referred_contact = ref
    db.update_lead(l)
    db.add_event(lead_id, "referral_accepted", detail=f"{previous['email']} -> {ref['email']}")
    db.close()
    return {"ok": True, "referred_contact": ref, "previous_contact": previous}


@app.get("/campaigns/{campaign_id}/export", dependencies=[Depends(require_campaign_access)])
def export(campaign_id: str, min_score: int = 70, buyers_only: bool = True,
           company_type: str | None = None, full: bool = False) -> FileResponse:
    # The CSV honours the same filters the Leads table shows. `company_type` (BUYER/UNKNOWN/
    # VENDOR) is preferred when given; `buyers_only` is the older default for callers that
    # don't pass one. None means "every type at or above min_score".
    # Default export is the clean, client-ready sheet; `?full=1` gives every raw column.
    ct = company_type or (CompanyType.BUYER.value if buyers_only else None)
    campaign = _campaign(campaign_id)
    db = _db()
    rows = db.list_leads(campaign_id, min_score=min_score, company_type=ct)
    db.close()
    # Scratch on serverless: the CSV only has to survive long enough to be streamed back.
    export_dir = runtime_dir() / "exports" if is_serverless() else _settings.export_dir
    out_path = export_path(export_dir, campaign_id, "ui", buyers_only)
    path = write_csv(rows, out_path) if full else write_clean_csv(rows, out_path)
    # The browser download name comes from the campaign's human name (short + sanitized),
    # not the long internal campaign_id slug, so saved files stay readable.
    import re
    label = re.sub(r"[^a-z0-9]+", "-", (campaign.name or campaign_id).lower()).strip("-")[:48] or "leads"
    suffix = "buyers" if ct == CompanyType.BUYER.value else "leads"
    download_name = f"{label}-{suffix}{'-full' if full else ''}.csv"
    return FileResponse(path, media_type="text/csv", filename=download_name)


# -- outreach: approval queue ------------------------------------------------------------

@app.get("/campaigns/{campaign_id}/outreach/queue", dependencies=[Depends(require_campaign_access)])
def outreach_queue(campaign_id: str) -> dict:
    """Everything due now, each with its draft (rendered on first view)."""
    campaign = _campaign(campaign_id)
    osettings, templates = load_outreach_settings(), load_templates()
    db = _db()
    enqueue(db, campaign_id, osettings, _read_only_ledger(campaign_id))
    items = prepare_drafts(db, campaign, osettings, templates)
    counts = {s.value: 0 for s in SequenceStatus}
    for l in db.list_leads(campaign_id):
        counts[l.sequence_status.value] += 1
    db.close()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ledger = _read_only_ledger(campaign_id)
    db2 = _db()
    states = MailboxPool(load_mailboxes(), osettings, runtime_dir() / "outbox").states(db2, campaign_id, ledger, day)
    db2.close()
    return {
        "items": [{"lead": _lead_summary(i["lead"]), "step": i["step"], "draft": i["draft"]} for i in items],
        "counts": counts,
        "smtp_configured": osettings.credentials_present,
        "daily_limit": sum(st.cap for st in states.values()),
        "sent_today": ledger.sent_on(day),
        "mailboxes": [st.as_dict() for st in states.values()],
    }


class DraftUpdate(BaseModel):
    # Bounded so a single draft can't be used to store megabytes in the drafts table. Generous
    # versus any real email: a long subject and a multi-part body still fit comfortably.
    subject: str = Field(max_length=2000)
    body: str = Field(max_length=100_000)


@app.get("/leads/{lead_id}/drafts/{step}", dependencies=[Depends(require_lead_access)])
def get_draft(lead_id: str, step: str) -> dict:
    db = _db()
    l = db.get_lead(lead_id)
    if not l:
        db.close()
        raise HTTPException(404, "lead not found")
    draft = db.get_draft(lead_id, step)
    if not draft:
        campaign = _campaign(l.campaign_id)
        r = render(step, l, campaign, load_outreach_settings(), load_templates())
        draft = db.upsert_draft(lead_id, step, r.subject, r.body)
    db.close()
    return draft


@app.put("/leads/{lead_id}/drafts/{step}", dependencies=[Depends(require_lead_access)])
def update_draft(lead_id: str, step: str, req: DraftUpdate) -> dict:
    db = _db()
    existing = db.get_draft(lead_id, step)
    if existing and existing["status"] == "sent":
        db.close()
        raise HTTPException(409, "already sent")
    if "{" in req.subject + req.body and "}" in req.subject + req.body:
        db.close()
        raise HTTPException(422, "unfilled {placeholder} in draft")
    draft = db.upsert_draft(lead_id, step, req.subject.strip(), req.body, status="pending", edited=True)
    db.close()
    return draft


@app.post("/leads/{lead_id}/drafts/{step}/approve", dependencies=[Depends(require_lead_access)])
def approve_draft(lead_id: str, step: str) -> dict:
    db = _db()
    d = db.get_draft(lead_id, step)
    if not d:
        db.close()
        raise HTTPException(404, "no draft")
    if d["status"] == "sent":
        db.close()
        raise HTTPException(409, "already sent")
    db.set_draft_status(lead_id, step, "approved")
    db.add_event(lead_id, "approved", step=step)
    out = db.get_draft(lead_id, step)
    db.close()
    return out


@app.post("/leads/{lead_id}/drafts/{step}/reject", dependencies=[Depends(require_lead_access)])
def reject_draft(lead_id: str, step: str) -> dict:
    db = _db()
    if not db.get_draft(lead_id, step):
        db.close()
        raise HTTPException(404, "no draft")
    db.set_draft_status(lead_id, step, "rejected")
    db.add_event(lead_id, "rejected", step=step)
    out = db.get_draft(lead_id, step)
    db.close()
    return out


@app.post("/leads/{lead_id}/drafts/{step}/reset", dependencies=[Depends(require_lead_access)])
def reset_draft(lead_id: str, step: str) -> dict:
    """Discard edits and re-render from the template."""
    db = _db()
    l = db.get_lead(lead_id)
    if not l:
        db.close()
        raise HTTPException(404, "lead not found")
    r = render(step, l, _campaign(l.campaign_id), load_outreach_settings(), load_templates())
    draft = db.upsert_draft(lead_id, step, r.subject, r.body, status="pending", edited=False)
    db.close()
    return draft


class SendRequest(BaseModel):
    limit: int | None = None
    dry_run: bool = False
    ignore_window: bool = False


@app.post("/campaigns/{campaign_id}/outreach/send", dependencies=[Depends(require_campaign_access)])
def outreach_send(campaign_id: str, req: SendRequest) -> dict:
    """Send approved, due emails. A real send is dispatched to GitHub Actions
    (outreach.yml): sender-protection jitter spaces sends 30-120s apart, which a batch
    of even a few emails blows past any Vercel serverless timeout. Dry-run previews stay
    synchronous here since they're fast and only touch throwaway state."""
    campaign = _campaign(campaign_id)
    osettings, templates = load_outreach_settings(), load_templates()
    dry = req.dry_run or not osettings.credentials_present
    if not dry:
        dispatch_workflow("outreach.yml", {
            "campaign_id": campaign_id,
            "limit": str(req.limit or ""),
            "dry_run": "false",
            "ignore_window": "true" if req.ignore_window else "false",
        })
        return {"dispatched": True, "mode": "github-actions", "sent": 0, "skipped": 0, "failed": 0,
                "stopped_reason": None, "details": ["dispatched to GitHub Actions (outreach.yml)"],
                "sync": None, "mailboxes": {}}
    # Dry-run preview: no trace on the real DB or ledger. Transactional dry-run mode is
    # rolled back on close; the ledger is pointed at a throwaway file.
    scratch = runtime_dir() / "outbox"
    scratch.mkdir(parents=True, exist_ok=True)
    db = Database(_settings.database_url, dry_run=True)
    ledger = Ledger(ledger_path(campaign_id))
    ledger.path = scratch / "dryrun_ledger.json"
    pool = MailboxPool(load_mailboxes(), osettings, scratch, dry_run=True)
    report = send_due(db, campaign, osettings, templates, pool, ledger, limit=req.limit, ignore_window=req.ignore_window)
    pool.close()
    db.close()
    return {"sent": report.sent, "skipped": report.skipped, "failed": report.failed,
            "stopped_reason": report.stopped_reason, "details": report.details,
            "mode": pool.name, "sync": None, "mailboxes": report.mailboxes}


@app.post("/campaigns/{campaign_id}/outreach/sync", dependencies=[Depends(require_campaign_access)])
def outreach_sync(campaign_id: str) -> dict:
    osettings = load_outreach_settings()
    if not osettings.credentials_present:
        raise HTTPException(400, "SMTP credentials not configured")
    db = _db()
    r = sync_replies(db, campaign_id, osettings, _read_only_ledger(campaign_id))
    db.close()
    return {"replied": r.replied, "unsubscribed": r.unsubscribed, "bounced": r.bounced, "scanned": r.scanned,
            "interested": r.interested, "not_interested": r.not_interested, "out_of_office": r.out_of_office,
            "wrong_person": r.wrong_person, "auto_reply": r.auto_reply, "details": r.details}


@app.get("/campaigns/{campaign_id}/outreach/activity", dependencies=[Depends(require_campaign_access)])
def outreach_activity(campaign_id: str, limit: int = 100) -> list[dict]:
    db = _db()
    rows = db.conn.execute(
        "SELECT e.*, l.data_json FROM outreach_events e JOIN leads l ON l.lead_id = e.lead_id "
        "WHERE l.campaign_id = %s ORDER BY e.event_id DESC LIMIT %s", (campaign_id, limit)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        lead = json.loads(d.pop("data_json"))
        d["company_name"] = lead.get("company_name")
        d["contact_email"] = lead.get("contact_email")
        out.append(d)
    db.close()
    return out


@app.get("/campaigns/{campaign_id}/outreach/sequence", dependencies=[Depends(require_campaign_access)])
def outreach_sequence(campaign_id: str) -> list[dict]:
    """Every lead that is in or has finished the sequence."""
    db = _db()
    active = [s.value for s in ACTIVE] + [SequenceStatus.FOLLOWUP_2_SENT.value, SequenceStatus.REPLIED.value,
                                          SequenceStatus.BOUNCED.value, SequenceStatus.UNSUBSCRIBED.value,
                                          SequenceStatus.SUPPRESSED.value]
    rows = db.leads_by_status(campaign_id, active)
    db.close()
    return [_lead_summary(l) for l in rows]


# -- settings: user API keys, usage, preferences ------------------------------------------

@app.get("/settings/api-keys")
def list_api_keys(user_id: str | None = Depends(current_user_id)) -> dict:
    if not user_id:
        raise HTTPException(401, "sign in to manage API keys")
    db = _db()
    keys = db.list_user_keys(user_id)
    db.close()
    return {"keys": keys, "encryption_available": encryption_available()}


class ApiKeyBody(BaseModel):
    value: str


@app.put("/settings/api-keys/{key_name}")
def save_api_key(key_name: str, body: ApiKeyBody, user_id: str | None = Depends(current_user_id)) -> dict:
    if not user_id:
        raise HTTPException(401, "sign in to manage API keys")
    if key_name not in ALLOWED_KEYS:
        raise HTTPException(422, f"unknown key: {key_name}; allowed: {', '.join(sorted(ALLOWED_KEYS))}")
    if not encryption_available():
        raise HTTPException(503, "GTM_ENCRYPTION_KEY not configured – cannot store API keys")
    try:
        encrypted = encrypt_key(body.value.strip())
    except Exception as exc:
        raise HTTPException(500, f"encryption failed: {exc}")
    try:
        db = _db()
        db.set_user_key(user_id, key_name, encrypted)
        db.close()
    except Exception as exc:
        raise HTTPException(500, f"database write failed: {exc}")
    return {"ok": True, "key_name": key_name}


@app.delete("/settings/api-keys/{key_name}")
def delete_api_key(key_name: str, user_id: str | None = Depends(current_user_id)) -> dict:
    if not user_id:
        raise HTTPException(401, "sign in to manage API keys")
    db = _db()
    deleted = db.delete_user_key(user_id, key_name)
    db.close()
    if not deleted:
        raise HTTPException(404, "key not found")
    return {"ok": True, "key_name": key_name}


@app.post("/settings/api-keys/{key_name}/test")
def test_api_key(key_name: str, user_id: str | None = Depends(current_user_id)) -> dict:
    if not user_id:
        raise HTTPException(401, "sign in to test API keys")
    if key_name not in ALLOWED_KEYS:
        raise HTTPException(422, f"unknown key: {key_name}")
    db = _db()
    encrypted = db.get_user_key(user_id, key_name)
    db.close()
    if not encrypted:
        raise HTTPException(404, "key not configured")
    try:
        plaintext = decrypt_key(encrypted)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(500, str(exc))
    ok, message = _test_key(key_name, plaintext)
    return {"ok": ok, "message": message}


def _test_key(key_name: str, key: str) -> tuple[bool, str]:
    """Quick smoke test for an API key. Synchronous, one lightweight call."""
    try:
        if key_name == "brave":
            r = httpx.get("https://api.search.brave.com/res/v1/web/search",
                          params={"q": "test", "count": "1"},
                          headers={"X-Subscription-Token": key, "Accept": "application/json"},
                          timeout=10)
            return r.status_code == 200, f"Brave: {r.status_code}"
        if key_name in ("groq",):
            r = httpx.post("https://api.groq.com/openai/v1/chat/completions",
                           json={"model": "openai/gpt-oss-20b", "messages": [{"role": "user", "content": "hi"}],
                                 "max_tokens": 1},
                           headers={"Authorization": f"Bearer {key}"}, timeout=10)
            return r.status_code == 200, f"Groq: {r.status_code}"
        if key_name == "hunter":
            r = httpx.get("https://api.hunter.io/v2/account", params={"api_key": key}, timeout=10)
            return r.status_code == 200, f"Hunter: {r.status_code}"
        if key_name == "places":
            r = httpx.post("https://places.googleapis.com/v1/places:searchText",
                           json={"textQuery": "test"},
                           headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": "places.id",
                                    "Content-Type": "application/json"},
                           timeout=10)
            return r.status_code == 200, f"Places: {r.status_code}"
        if key_name == "gemini":
            r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={key}",
                           json={"contents": [{"parts": [{"text": "hi"}]}]}, timeout=10)
            return r.status_code == 200, f"Gemini: {r.status_code}"
    except httpx.HTTPError as exc:
        return False, str(exc)
    return False, f"unknown key type: {key_name}"


@app.get("/settings/usage")
def usage_dashboard(user_id: str | None = Depends(current_user_id)) -> dict:
    if not user_id:
        raise HTTPException(401, "sign in to view usage")
    db = _db()
    usage = get_all_usage(db, user_id)
    db.close()
    return {"usage": usage}


class UsageLimitBody(BaseModel):
    limit: int


@app.put("/settings/usage/{resource}")
def update_usage_limit(resource: str, body: UsageLimitBody,
                       user_id: str | None = Depends(current_user_id)) -> dict:
    if not user_id:
        raise HTTPException(401, "sign in to update limits")
    if resource not in DEFAULT_LIMITS:
        raise HTTPException(422, f"unknown resource: {resource}")
    cap = MAX_LIMITS.get(resource, 1000)
    clamped = max(1, min(body.limit, cap))
    db = _db()
    db.set_preference(user_id, f"daily_limit_{resource}", str(clamped))
    db.close()
    return {"ok": True, "resource": resource, "limit": clamped}


@app.get("/settings/preferences")
def get_preferences(user_id: str | None = Depends(current_user_id)) -> dict:
    if not user_id:
        raise HTTPException(401, "sign in to view preferences")
    db = _db()
    prefs = db.get_preferences(user_id)
    db.close()
    return {"preferences": {p["pref_key"]: p["pref_value"] for p in prefs}}


class PreferenceBody(BaseModel):
    value: str


_BLOCKED_PREF_PREFIXES = ("daily_limit_",)


@app.put("/settings/preferences/{pref_key}")
def set_preference(pref_key: str, body: PreferenceBody,
                   user_id: str | None = Depends(current_user_id)) -> dict:
    if not user_id:
        raise HTTPException(401, "sign in to save preferences")
    if any(pref_key.startswith(p) for p in _BLOCKED_PREF_PREFIXES):
        raise HTTPException(422, f"use PUT /settings/usage/{{resource}} to change usage limits")
    if len(pref_key) > 100:
        raise HTTPException(422, "preference key too long")
    db = _db()
    db.set_preference(user_id, pref_key, body.value.strip()[:2000])
    db.close()
    return {"ok": True, "pref_key": pref_key}


# -- User mailboxes (self-serve SMTP credentials) ----------------------------

# Standard SMTP submission ports. Without a port allowlist the mailbox-test endpoint is an
# SSRF primitive: any signed-in user could point it at an internal host:port and read the
# connection banner / error to probe the private network.
_ALLOWED_SMTP_PORTS = {25, 465, 587, 2525}


def _validate_smtp_target(host: str, port: int) -> None:
    """Reject an SMTP target that could be used to probe the internal network. The port must be
    a real SMTP submission port, and the host must not resolve to a non-public address."""
    import socket
    from gtm_engine.scraping.fetcher import _ip_is_blocked

    host = (host or "").strip()
    if not host:
        raise HTTPException(422, "smtp_host is required")
    if port not in _ALLOWED_SMTP_PORTS:
        raise HTTPException(422, f"smtp_port must be one of {sorted(_ALLOWED_SMTP_PORTS)}")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        raise HTTPException(422, f"could not resolve smtp_host '{host}'")
    import ipaddress
    for info in infos:
        try:
            if _ip_is_blocked(ipaddress.ip_address(info[4][0])):
                raise HTTPException(422, "smtp_host resolves to a non-public address")
        except ValueError:
            continue


class MailboxBody(BaseModel):
    address: str
    password: str
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    sender_name: str | None = None
    daily_limit: int | None = None


@app.get("/settings/mailboxes")
def list_user_mailboxes(user_id: str | None = Depends(current_user_id)) -> dict:
    if not user_id:
        raise HTTPException(401, "sign in to manage mailboxes")
    db = _db()
    rows = db.list_user_mailboxes(user_id)
    db.close()
    return {"mailboxes": rows, "encryption_available": encryption_available()}


@app.put("/settings/mailboxes")
def save_user_mailbox(body: MailboxBody, user_id: str | None = Depends(current_user_id)) -> dict:
    if not user_id:
        raise HTTPException(401, "sign in to manage mailboxes")
    if not encryption_available():
        raise HTTPException(503, "GTM_ENCRYPTION_KEY not configured – cannot store mailbox credentials")
    addr = body.address.strip().lower()
    if "@" not in addr:
        raise HTTPException(422, "invalid email address")
    _validate_smtp_target(body.smtp_host, body.smtp_port)
    encrypted_pw = encrypt_key(body.password.strip()) if body.password.strip() else None
    db = _db()
    db.set_user_mailbox(user_id, addr, encrypted_pw, body.smtp_host, body.smtp_port,
                        body.sender_name, body.daily_limit)
    db.close()
    return {"ok": True, "address": addr}


@app.delete("/settings/mailboxes/{address}")
def delete_user_mailbox(address: str, user_id: str | None = Depends(current_user_id)) -> dict:
    if not user_id:
        raise HTTPException(401, "sign in to manage mailboxes")
    db = _db()
    deleted = db.delete_user_mailbox(user_id, address)
    db.close()
    if not deleted:
        raise HTTPException(404, "mailbox not found")
    return {"ok": True, "address": address}


@app.post("/settings/mailboxes/{address}/toggle")
def toggle_user_mailbox(address: str, user_id: str | None = Depends(current_user_id)) -> dict:
    if not user_id:
        raise HTTPException(401, "sign in to manage mailboxes")
    db = _db()
    mb = db.get_user_mailbox(user_id, address)
    if not mb:
        db.close()
        raise HTTPException(404, "mailbox not found")
    new_state = not mb["enabled"]
    db.toggle_user_mailbox(user_id, address, new_state)
    db.close()
    return {"ok": True, "enabled": new_state}


@app.post("/settings/mailboxes/test")
def test_user_mailbox(body: MailboxBody) -> dict:
    """Test SMTP connection without saving. Returns ok + message."""
    import smtplib
    addr = body.address.strip().lower()
    pw = body.password.strip()
    if not addr or not pw:
        raise HTTPException(422, "address and password are required")
    _validate_smtp_target(body.smtp_host, body.smtp_port)
    try:
        conn = smtplib.SMTP(body.smtp_host, body.smtp_port, timeout=15)
        conn.ehlo()
        conn.starttls()
        conn.ehlo()
        conn.login(addr, pw)
        conn.quit()
        return {"ok": True, "message": f"Connected to {body.smtp_host}:{body.smtp_port} as {addr}"}
    except smtplib.SMTPAuthenticationError:
        return {"ok": False, "message": "Authentication failed – check email and app password"}
    except (smtplib.SMTPException, OSError) as exc:
        return {"ok": False, "message": f"Connection failed: {exc}"}
