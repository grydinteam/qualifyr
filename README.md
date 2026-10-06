<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/hero-dark.svg">
    <img alt="Qualifyr — describe what you sell, it finds the companies that actually need it" src=".github/assets/hero-light.svg" width="900">
  </picture>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: GPLv3" src="https://img.shields.io/badge/License-GPLv3-0a0a0a?style=flat-square"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/Python-3.12+-0a0a0a?style=flat-square">
  <img alt="Next.js 16" src="https://img.shields.io/badge/Next.js-16-0a0a0a?style=flat-square">
  <a href="https://github.com/grydinteam/qualifyr/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/grydinteam/qualifyr/ci.yml?style=flat-square&color=0a0a0a&label=tests"></a>
</p>

---

## In one sentence

**You describe what you sell. Qualifyr finds the real companies that would actually buy it** — from free public data — reads their websites, works out who's a genuine buyer, finds a decision-maker and a validated email, and ranks each match 0–100 with a plain-English reason.

It's built for **quality over quantity**: a handful of strong, well-explained matches beats a list of a thousand cold contacts. Vendors, agencies, and competitors are filtered *out*, not mixed in. There's an optional outreach flow that drafts emails — but **nothing is ever sent without a human approving it first.**

> **New here?** Think of it as a research assistant for sales. You say *"I sell inventory software for grocery stores in Islamabad"* — it goes and finds those grocery stores, checks each one, and hands you a short, ranked shortlist with the reasoning for every pick.

---

## How it works

```mermaid
flowchart LR
    A["Describe your offer"] --> B["Discover companies"]
    B --> C["Qualify buyer vs vendor"]
    C --> D["Enrich: contact, email, signals"]
    D --> E["Score 0-100 with reasons"]
    E --> F["Ready-to-contact leads"]
```

Every stage is driven by plain YAML config — no code change is needed to run a different search. And a **deterministic fallback always runs**, so a missing API key only disables that one feature; it never breaks a run.

1. **Describe → targets.** Plain-English offer becomes the map categories and search queries most likely to contain buyers (a curated taxonomy, plus an optional LLM). You don't need to know OpenStreetMap tag syntax.
2. **Discover from free sources.** OpenStreetMap / Overture Maps, the KCCI chamber directory, PPRA government tenders, web search, and seed CSVs.
3. **Qualify with evidence.** Each company's site is crawled and classified **BUYER / VENDOR / UNKNOWN**; an optional grounded LLM judges *intent* (does this company actually need the offer?). Every verdict carries the evidence behind it.
4. **Enrich.** A named decision-maker and their email (syntax + MX checked, optionally verified), plus buying/pain signals.
5. **Score transparently.** 0–100 from five components — review band, rating, proximity, online gap, pain evidence — each with a reason.
6. **Deliver.** A client-ready CSV, a per-company research brief, and a web dashboard.

---

## See it

<p align="center">
  <img alt="Qualifyr dashboard — qualified buyers, score distribution, and the reasons behind each match" src=".github/assets/app-preview.svg" width="900">
</p>

The web app has five pages — **Dashboard** (counts, score spread, top matches), **Campaigns** (create a search in plain English or by hand, run it with live progress, export CSV), **Leads** (every company processed, filterable, each one opening to its full reasoning and research brief), **Outreach** (the approval queue), and **Settings** (your API keys, usage, mailboxes).

---

## Architecture

```mermaid
flowchart TB
    UI["Next.js web app (Vercel)"] --> API["FastAPI API (Vercel function)"]
    API --> DB[("Supabase Postgres")]
    API -->|dispatches long jobs| GHA["GitHub Actions: crawl and send"]
    GHA --> ENG["Engine pipeline (Python)"]
    ENG --> SRC["Free public data: OSM, Overture, KCCI, PPRA, web"]
    ENG --> DB
```

A full campaign run takes minutes to hours, so the deployed API never crawls in-request — it **dispatches** the work to GitHub Actions and reads progress back from Postgres. The API itself is stateless; Postgres is the single source of truth.

---

## Table of contents

1. [Quick start](#quick-start)
2. [Prerequisites](#prerequisites)
3. [Setup](#setup)
4. [Running it](#running-it)
5. [Configuration](#configuration)
6. [API keys — what each one unlocks](#api-keys--what-each-one-unlocks)
7. [Outreach (approval-gated)](#outreach-approval-gated)
8. [Deployment](#deployment)
9. [Project layout](#project-layout)
10. [Testing](#testing)
11. [Troubleshooting](#troubleshooting)
12. [Contributing & license](#contributing--license)

---

## Quick start

```bash
git clone https://github.com/grydinteam/qualifyr.git
cd qualifyr

python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[api,overture]"

cp .env.example .env            # add GTM_DATABASE_URL (Supabase) + GTM_AUTH_DISABLED=1 for local
gtm run config/campaigns/example_retail_islamabad.yaml --max-companies 20
```

That discovers, qualifies, and scores real companies and writes a CSV — no paid keys required. Add the web app and optional keys as you go (below).

---

## Prerequisites

| Requirement | Why | Notes |
|---|---|---|
| **Python 3.12+** | the engine, CLI, and API | `python --version` |
| **A Supabase project** (free tier) | Postgres storage + user auth | [supabase.com](https://supabase.com) — the only hard external dependency |
| **Node.js 20+** | the web app (optional) | only if you want the UI; the CLI works without it |
| **Git** | clone + (for scheduled runs) GitHub Actions | |

Everything else — mail sending, LLM, web search, email verification, Google Places — is **optional**. Qualifyr degrades gracefully: a missing key disables that one feature and never breaks a run. See [API keys](#api-keys--what-each-one-unlocks).

---

## Setup

### 1. Clone and install the engine

```bash
git clone https://github.com/grydinteam/qualifyr.git
cd qualifyr

python -m venv .venv
# activate it:
#   Linux / macOS:  source .venv/bin/activate
#   Windows (PowerShell):  .venv\Scripts\Activate.ps1
#   Windows (Git Bash):    source .venv/Scripts/activate

pip install -e ".[api,overture]"
```

Installing with `-e` (editable) puts a `gtm` command on your PATH. Optional extras:

| Extra | Adds | Install when |
|---|---|---|
| `api` | FastAPI + uvicorn + JWT verification | you want the API / web app |
| `overture` | DuckDB (Overture Maps discovery) | recommended — a major free data source |
| `browser` | Playwright (renders JS-only sites) | sites that don't work without JS |
| `sheets` | Google Sheets export | you mirror leads to a Sheet |
| `dev` | pytest + test deps | you run the test suite |

Full local setup: `pip install -e ".[api,overture,browser,sheets,dev]"`
(then `playwright install chromium` if you added `browser`).

### 2. Create the database (Supabase)

1. Create a free project at [supabase.com](https://supabase.com).
2. **Connection string** → Project Settings → Database → *Connection string* → **Transaction pooler** mode. Copy it and fill in your database password. This is `GTM_DATABASE_URL`.
3. **Auth** (only needed for the web app) → the project URL (`https://xxxx.supabase.co`) is `GTM_SUPABASE_URL`, and the **anon/publishable** key is `NEXT_PUBLIC_SUPABASE_ANON_KEY`. Enable the Email provider under Authentication → Providers.

You do **not** need to create any tables — the schema is created automatically on first connection (`CREATE TABLE IF NOT EXISTS …`). Only the plain Postgres connection string is used; Supabase's service-role/REST keys are not.

### 3. Configure environment variables

```bash
cp .env.example .env
```

The bare minimum to run a campaign locally:

```bash
# .env
GTM_DATABASE_URL=postgresql://postgres.xxxx:PASSWORD@aws-0-region.pooler.supabase.com:6543/postgres
GTM_AUTH_DISABLED=1          # local only – skips the API token check
```

That's enough to discover and qualify companies and export a CSV. `.env` is auto-loaded and is gitignored — never commit it. Add optional keys as you need the features behind them ([table below](#api-keys--what-each-one-unlocks)); `.env.example` documents every variable.

### 4. (Optional) Set up the web app

```bash
cd web
npm install
```

Create `web/.env.local`:

```bash
# web/.env.local
NEXT_PUBLIC_SUPABASE_URL=https://your-project.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=your-anon-key
NEXT_PUBLIC_API_URL=http://localhost:8000     # where the FastAPI server runs
```

These are **publishable** and inlined into the browser bundle (the anon key is designed for that). On Vercel they're baked in at build time, so changing them needs a redeploy.

---

## Running it

The `gtm` command (from the editable install) is cross-platform. If it isn't on your PATH, use `python -m gtm_engine.cli …` instead.

**Run a campaign end-to-end** (discover → qualify → score → CSV):

```bash
gtm run config/campaigns/example_retail_islamabad.yaml --max-companies 20
```

Outputs land in `data/exports/<campaign>_<timestamp>_<run>_qualified.csv` (matches ≥ min_score) and `..._all.csv` (everything, for audit).

**Create a campaign from plain English** (needs a Groq key for best results, works without one):

```bash
gtm nl "find grocery stores in Islamabad that need inventory software"
```

**Other CLI commands:**

```bash
gtm export <campaign_id> --min-score 70        # re-export stored leads
gtm runs                                        # list past runs
gtm campaign-id <path-or-id>                    # resolve a campaign id
gtm suppress someone@company.pk --reason "..."  # never contact again
gtm sheets <campaign_id>                         # mirror to Google Sheets (needs creds)
```

**Run the API** (needed for the web app):

```bash
uvicorn gtm_engine.api.main:app --reload        # http://localhost:8000
```

**Run the web app** (in another terminal):

```bash
cd web && npm run dev                            # http://localhost:3000
```

Open `http://localhost:3000`, sign up, and create a campaign from the UI.

---

## Configuration

Campaigns are YAML. Three worked examples live in `config/campaigns/`. A campaign defines the offer, geography, and thresholds; the engine derives discovery targets from the offer, but you can override categories/queries explicitly.

| File | Controls |
|---|---|
| `config/campaigns/*.yaml` | offer, industries, cities/areas, keywords, OSM/Overture categories, min score, max companies |
| `config/defaults/discovery_taxonomy.yaml` | offer → sector → map categories (how an offer becomes a search) |
| `config/defaults/vendor_rules.yaml` | negative keywords + vendor self-description phrases (the buyer gate) |
| `config/defaults/roles.yaml` | decision-maker role whitelist, sell-side blacklist, generic mailboxes |
| `config/defaults/signals.yaml` · `intent.yaml` | buying/pain signal phrases; tender/RFQ/hiring intent phrases |
| `config/engine.yaml` | concurrency, timeouts, rate limits, robots, LLM provider, scoring toggles |
| `config/outreach/settings.yaml` · `templates.yaml` | send window, caps, spacing; email copy |

Most `engine.yaml` settings can be overridden by `GTM_*` environment variables.

**Scoring** (max 100): `review_band(0–30) + rating(0–10) + proximity_tier(0–15) + online_gap(0–25) + pain_evidence(0–20)`. Routing thresholds: high-priority 55, qualified 40, review 20.

---

## API keys — what each one unlocks

All optional. A missing key disables only that feature. Full details and current quotas live in [`docs/API_KEYS.md`](docs/API_KEYS.md).

| Key(s) | Unlocks | Free tier |
|---|---|---|
| `GTM_BRAVE_API_KEY` | web-search discovery + website finding (falls back to keyless DuckDuckGo) | ~1k searches/mo (card required) |
| `GTM_HUNTER_API_KEY` / `GTM_REACHER_URL` | decision-maker email verification | Hunter free; Reacher self-host |
| `GTM_GROQ_API_KEY` | LLM layer: offer→targets, intent judging, relevance, query generation | Groq `gpt-oss-20b`, ~8k tokens/min |
| `GTM_GOOGLE_PLACES_API_KEY` | rating, review count, hours, review text (largest scoring boost) | ~1k calls/mo |
| `GTM_GEMINI_API_KEY` | alternate LLM (Groq is the reliable default — you only need one) | often 404/503 on free tier |
| `GTM_SMTP_*` / `GTM_GMAIL_*` | actually sending outreach (otherwise every send is a dry run) | – |
| `GTM_SHEETS_*` | Google Sheets export mirror | – |

**Which to add first?** None is required — the engine runs keyless. If you're adding them one at a time: **Brave** (broader discovery) and **Hunter** (verified emails) give the most out-of-the-box, since both are on by default. **Groq** and **Places** add the biggest quality jump and switch on automatically once their key is present.

**Per-user keys (self-hosting).** In the web app, each user stores their own keys, **encrypted at rest** with `GTM_ENCRYPTION_KEY` (Fernet), with per-day usage limits and a free tier (3 campaigns, 10 leads each). At run time a user's keys are decrypted into that run's environment on the GitHub Actions runner — never written to disk in the clear. Generate the encryption key once:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## Outreach (approval-gated)

Nothing is sent unattended. Each due email is drafted from a template, shown in the UI, editable, and sent only after you approve it. Follow-ups come back for their own approval.

```bash
gtm outreach preview <campaign_id>              # see the drafted emails
gtm outreach send <campaign_id> --dry-run       # writes .eml files to data/outbox/
gtm outreach send <campaign_id>                 # real send (needs mail credentials)
gtm outreach status <campaign_id>
```

**Sequence:** Email 1 → +3 days Follow-up 1 → +4 days Follow-up 2, threaded; stops on reply, bounce, "STOP", or suppression. **Sender protection** is on by default: warm-up ramp, 30–120s random spacing, a 09:00–18:00 Asia/Karachi weekday window, and a bounce-rate guard. Several mailboxes rotate (`GTM_MAILBOX_1_*` … `_10_*`); Email 1 goes to the least-loaded mailbox, follow-ups stay on the thread's mailbox. Replies are pulled over IMAP and classified. Every send is recorded in a durable ledger so an address never gets the same step twice.

Preferred credentials: Gmail OAuth2 — set `GTM_GMAIL_CLIENT_ID/SECRET` + `GTM_SMTP_USER` and run `gtm outreach gmail-auth` once. Fallback: `GTM_SMTP_PASSWORD` (an App Password).

---

## Deployment

The production topology (how the hosted alpha runs):

- **Web app + API → Vercel.** The API is a Python function (`web/api/index.py`); `web/vercel.json` bundles `gtm_engine/` + `config/` and rewrites `/api/*` to it.
- **Database → Supabase** (`GTM_DATABASE_URL`, `GTM_SUPABASE_URL`).
- **Long jobs → GitHub Actions.** The deployed API is treated as read-only for crawling/sending; it *dispatches* workflows (`GTM_GITHUB_TOKEN`, `GTM_GITHUB_REPO`) rather than crawling in-request, because runs take minutes-to-hours.

Workflows (`.github/workflows/`):

| Workflow | Trigger | Does |
|---|---|---|
| `gather-leads.yml` | weekly cron + manual | discovery/crawl, commits leads (never contacts anyone) |
| `outreach.yml` | **manual only, by design** | sends approved emails (no cron — nothing emails unattended) |
| `verify-sent.yml` | post-send | delivery verification |
| `ci.yml` | push/PR | tests + web build |
| `pages.yml` | push | GitHub Pages landing page |

Set `GTM_CORS_ORIGINS` to your deployed frontend origin (`localhost:3000` is always allowed). CORS is a browser policy, not access control — the Supabase bearer-token check protects the data.

---

## Project layout

```
gtm_engine/
  config/         pydantic schema + YAML loader + defaults
  discovery/      targeting.py (offer→targets), overture.py, osm.py, chambers.py (KCCI),
                  web_search.py, geocode.py (Nominatim), csv_seed.py, search.py (website finder)
  scraping/       fetcher.py (polite HTTP), integrity.py (parked/soft-404), browser.py (Playwright),
                  site_crawler.py, parsers.py
  qualification/  buyer_classifier.py (the gate) + relevance.py
  intent/         ppra.py (PK tenders), company_pages.py (RFQ/hiring signals)
  enrichment/     contacts, signals, email_patterns, places.py (Google Places), online_presence.py,
                  hours.py, research.py (research brief), fieldclean.py
  llm/            client.py (Groq/Gemini/Ollama) + tasks.py — grounded, optional
  scoring/        scoring.py (decomposed 0–100) + proximity.py
  validation/     domains, emails, dedupe, verifier (Hunter/Reacher/MX), liveness
  outreach/       sequencer, sender, reply classifier, mailboxes, durable ledger
  export/         csv_export.py, sheets.py
  storage/        database.py (Postgres via psycopg, plain SQL, no ORM)
  api/            FastAPI backend (also the Vercel function)
  pipeline.py     orchestration · cli.py
web/              Next.js 16 app (web/README.md for its own notes)
config/           campaigns, defaults, engine.yaml, outreach settings
tests/            pytest suite (HTML/Overpass fixtures, disposable Postgres schema per run)
docs/             API_KEYS.md, DECISIONS.md, DIRECTION.md, REQUIREMENTS.md, ROADMAP.txt
.github/workflows/
```

---

## Testing

```bash
pytest -q
```

Tests that need a database are skipped unless `GTM_TEST_DATABASE_URL` (or `GTM_DATABASE_URL`) is set; they create and drop a throwaway schema per run, so they never touch real data. CI runs the full suite against a disposable Postgres service, plus the web build.

```bash
# frontend checks
cd web && npm run build          # tsc + eslint + production build
```

---

## Troubleshooting

- **Every API request returns 500.** `GTM_SUPABASE_URL` is unset. This is deliberate — an unset auth variable must never silently open the API. Set it, or use `GTM_AUTH_DISABLED=1` locally.
- **DB-backed tests all skip.** Expected without `GTM_TEST_DATABASE_URL` / `GTM_DATABASE_URL`.
- **No companies discovered.** With no Brave key and no explicit categories, give the offer more signal, or add OSM categories / search queries in the campaign; install the `overture` extra for the Overture data source.
- **Sends do nothing.** With no mail credentials every send is a dry run (writes `.eml` files to `data/outbox/`). Add `GTM_GMAIL_*` or `GTM_SMTP_*`.
- **Sign-up says sign-up isn't configured.** `NEXT_PUBLIC_SUPABASE_URL` / `_ANON_KEY` are missing from the build — on Vercel, add them and redeploy (they're inlined at build time).

---

## Contributing & license

Issues and PRs welcome. Before opening a PR: `pytest -q` and `cd web && npm run build` should both pass. See `docs/DECISIONS.md` for *why* things are built the way they are, and `docs/API_KEYS.md` for the full credential registry.

**License:** [GNU General Public License v3.0](LICENSE). You may use, study, share, and modify Qualifyr; derivative works must remain under the GPLv3.
