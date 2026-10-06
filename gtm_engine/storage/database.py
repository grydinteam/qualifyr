"""Postgres (Supabase) repository. Was SQLite; kept the same plain-SQL, thin-repository
shape (see docs/DECISIONS.md) so only this module and its constructor argument changed –
every caller still just does `Database(dsn)` and calls the same methods."""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse

import psycopg
from psycopg.rows import dict_row

from gtm_engine.models import Lead, utcnow

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    owner_id TEXT
);
-- Multi-tenancy: scope a campaign (and thus its leads) to the account that created it.
-- Added by migration so databases created before multi-tenancy pick the column up too;
-- NULL owner means a shared/legacy campaign, visible to everyone.
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS owner_id TEXT;
-- Soft delete: deleting a campaign stamps deleted_at and hides it from the UI, but the row
-- (and its leads) stay in the database permanently and for every account. Never a hard DELETE.
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS deleted_at TEXT;

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    stats_json TEXT
);

CREATE TABLE IF NOT EXISTS companies (
    company_key TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    name TEXT NOT NULL,
    domain TEXT,
    website TEXT,
    country TEXT,
    city TEXT,
    source TEXT,
    source_url TEXT,
    discovered_at TEXT NOT NULL,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS pages (
    url TEXT PRIMARY KEY,
    company_key TEXT NOT NULL,
    kind TEXT,
    status_code INTEGER,
    title TEXT,
    text TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    lead_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    run_id TEXT,
    company_key TEXT,
    company_type TEXT NOT NULL,
    total_score INTEGER NOT NULL,
    priority TEXT NOT NULL,
    outreach_ready INTEGER NOT NULL DEFAULT 0,
    sequence_status TEXT NOT NULL,
    contact_email TEXT,
    data_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_campaign ON leads(campaign_id);
CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company_key);
-- Composite indexes so the paginated Leads list (newest-first) and the dashboard top-buyers
-- (highest-score-first) both stay fast as a campaign accumulates leads.
CREATE INDEX IF NOT EXISTS idx_leads_campaign_updated ON leads(campaign_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_leads_campaign_score ON leads(campaign_id, total_score DESC);

-- Per-tenant do-not-contact list. owner_id scopes a suppression to the account that created it;
-- '' is the shared/global scope (CLI/operator adds, legacy rows) that applies to everyone. The
-- PK is (owner_id, value) so two tenants can independently suppress the same value without one
-- clobbering the other.
CREATE TABLE IF NOT EXISTS suppressions (
    value TEXT NOT NULL,
    kind TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    owner_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (owner_id, value)
);
-- Migrate pre-tenant databases: add owner_id, then move the primary key from (value) to
-- (owner_id, value). Guarded so it runs once and is a no-op on a database already migrated or
-- freshly created with the composite key.
ALTER TABLE suppressions ADD COLUMN IF NOT EXISTS owner_id TEXT NOT NULL DEFAULT '';
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
        WHERE i.indrelid = 'suppressions'::regclass AND i.indisprimary AND a.attname = 'owner_id'
    ) THEN
        ALTER TABLE suppressions DROP CONSTRAINT IF EXISTS suppressions_pkey;
        ALTER TABLE suppressions ADD PRIMARY KEY (owner_id, value);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS drafts (
    lead_id TEXT NOT NULL,
    step TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL,               -- pending | approved | rejected | sent
    edited INTEGER NOT NULL DEFAULT 0,  -- 1 when a human changed the rendered text
    created_at TEXT NOT NULL,
    approved_at TEXT,
    PRIMARY KEY (lead_id, step)
);

CREATE TABLE IF NOT EXISTS outreach_events (
    event_id SERIAL PRIMARY KEY,
    lead_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    step TEXT,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_progress (
    campaign_id TEXT PRIMARY KEY,
    run_id TEXT,
    stage TEXT,
    done INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_api_keys (
    user_id TEXT NOT NULL,
    key_name TEXT NOT NULL,
    encrypted_value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, key_name)
);

CREATE TABLE IF NOT EXISTS user_mailboxes (
    user_id TEXT NOT NULL,
    address TEXT NOT NULL,
    encrypted_password TEXT,
    smtp_host TEXT NOT NULL DEFAULT 'smtp.gmail.com',
    smtp_port INTEGER NOT NULL DEFAULT 587,
    sender_name TEXT,
    daily_limit INTEGER,
    enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, address)
);

CREATE TABLE IF NOT EXISTS usage_counts (
    user_id TEXT NOT NULL,
    resource TEXT NOT NULL,
    daily_count INTEGER NOT NULL DEFAULT 0,
    last_reset_date TEXT NOT NULL,
    PRIMARY KEY (user_id, resource)
);

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id TEXT NOT NULL,
    pref_key TEXT NOT NULL,
    pref_value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, pref_key)
);

CREATE TABLE IF NOT EXISTS hidden_campaigns (
    campaign_id TEXT PRIMARY KEY,
    hidden_at TEXT NOT NULL
);

-- Foreign-key lookups that have no index scan the whole table as a campaign/lead accumulates
-- rows. Added here so the activity feed, run history, and per-campaign company counts stay fast.
CREATE INDEX IF NOT EXISTS idx_runs_campaign ON runs(campaign_id);
CREATE INDEX IF NOT EXISTS idx_companies_campaign ON companies(campaign_id);
CREATE INDEX IF NOT EXISTS idx_pages_company ON pages(company_key);
CREATE INDEX IF NOT EXISTS idx_events_lead ON outreach_events(lead_id);
CREATE INDEX IF NOT EXISTS idx_events_type_created ON outreach_events(event_type, created_at);
"""


# DSNs whose schema this process has already ensured. Every API request opens a fresh
# Database, and re-running ~10 DDL statements per request would dominate the latency of
# an otherwise trivial read; once per process (warm serverless container) is enough.
_SCHEMA_READY: set[str] = set()

# A fixed key for the advisory lock that serialises schema creation (see __init__). Any
# constant works - it only ever guards the DDL below, which is idempotent and rare.
_SCHEMA_LOCK_KEY = 0x67746D5F736368  # "gtm_sch"


def _search_path_of(dsn: str) -> str | None:
    """The schema named by `?options=-csearch_path=NAME` in a DSN, if any.

    Only the tests use this (each gets a disposable schema); production DSNs carry no
    options and land in `public`.
    """
    query = urllib.parse.urlsplit(dsn).query
    for value in urllib.parse.parse_qs(query).get("options", []):
        match = re.search(r"-c\s*search_path\s*=\s*([^\s,]+)", value)
        if match:
            schema = match.group(1)
            # Defence in depth: this value is interpolated into `SET search_path TO "..."`, so
            # restrict it to a plain SQL identifier. The DSN is operator-controlled today, but a
            # non-identifier here is either a misconfiguration or an injection attempt.
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
                raise ValueError(f"unsafe search_path schema in DSN: {schema!r}")
            return schema
    return None


class Database:
    def __init__(self, dsn: str, *, dry_run: bool = False, ensure_schema: bool | None = None):
        """`dry_run=True` opens a transaction that is rolled back on close() instead of
        committed, so simulated writes (e.g. `outreach send --dry-run`) are invisible to
        every other connection and never touch real data - replaces the old SQLite
        file-copy trick, and works against live current state instead of a stale copy.

        `ensure_schema` defaults to "once per DSN per process"; pass False to skip the
        DDL entirely or True to force it."""
        if not dsn:
            raise ValueError(
                "no database DSN: set GTM_DATABASE_URL to the Supabase Postgres "
                "connection string (see .env.example)"
            )
        self.dsn = dsn
        self.dry_run = dry_run
        self.conn = self._connect()
        if ensure_schema is None:
            ensure_schema = dsn not in _SCHEMA_READY
        if ensure_schema:
            # `CREATE TABLE IF NOT EXISTS` is not atomic against a concurrent create: two
            # connections can both find a table absent and both try to create it, and one
            # then fails on the pg_type unique index (seen when a burst of requests hits a
            # cold, empty database at once). A transaction-scoped advisory lock serialises
            # this DDL across connections; it releases on commit, so it is safe through a
            # transaction-mode pooler (PgBouncer) too.
            self._execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_KEY,))
            self._execute(SCHEMA)
            self.conn.commit()
            _SCHEMA_READY.add(dsn)

    def _connect(self) -> "psycopg.Connection":
        # prepare_threshold=None disables psycopg's automatic prepared statements, which
        # PgBouncer in transaction mode (Supabase's :6543 pooler, the right choice for
        # serverless) cannot carry across pooled connections. Harmless on :5432.
        conn = psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False, prepare_threshold=None)
        # A search_path in the DSN's `options=` is a *startup parameter*, and PgBouncer does not
        # forward those - against Supabase's pooler it is silently dropped and every query quietly
        # resolves in `public` instead. That failure is invisible (no error, just the wrong
        # schema), so re-apply it as an explicit SET, which always takes effect. Re-applied on
        # every (re)connect, since a fresh connection resets it.
        schema = _search_path_of(self.dsn)
        if schema:
            conn.execute(f'SET search_path TO "{schema}"')
            conn.commit()
        return conn

    def _reconnect(self, attempts: int = 3, base_delay: float = 0.5) -> None:
        """Re-establish the connection after it drops mid-run. A long crawl outlives a pooler's
        idle recycle, so without this the rest of the run silently fails every query. Best-effort
        close of the dead handle, then a few backed-off reconnect attempts; the schema already
        exists, so no DDL is re-run."""
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001 - the handle is already broken; closing is best-effort
            pass
        last: Exception | None = None
        for i in range(attempts):
            try:
                self.conn = self._connect()
                log.info("db reconnected")
                return
            except psycopg.OperationalError as exc:
                last = exc
                time.sleep(base_delay * (2 ** i))
        raise last if last else psycopg.OperationalError("reconnect failed")

    def _execute(self, query, params=None):
        """Run a statement, reconnecting once if the connection has dropped. Returns the cursor,
        so callers can `.fetchone()/.fetchall()`/iterate exactly as with `conn.execute`."""
        try:
            return self.conn.execute(query, params)
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            if self.dry_run:
                raise  # a dry-run transaction can't be meaningfully resumed on a new connection
            log.warning("db connection lost (%s); reconnecting", exc)
            self._reconnect()
            return self.conn.execute(query, params)

    def _commit(self) -> None:
        if self.dry_run:
            return
        try:
            self.conn.commit()
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            # The in-flight transaction is lost and its writes are gone. Recover the connection
            # so later operations can proceed, but re-raise: a silent return here would make
            # save_lead (and every other writer) report success for data that never landed.
            # The caller - the pipeline's per-company loop - logs and moves on; the lead is
            # re-derivable on the next run, a silently dropped one is not.
            log.warning("db commit failed on a lost connection (%s); reconnecting then raising", exc)
            self._reconnect()
            raise

    def close(self) -> None:
        if self.dry_run:
            self.conn.rollback()
        self.conn.close()

    # -- campaigns / runs -------------------------------------------------

    def upsert_campaign(self, campaign_id: str, name: str, config: dict, owner_id: str | None = None) -> None:
        # owner_id is set on create and preserved on later updates (a run re-upserts the
        # campaign but must not blank its owner), so COALESCE keeps the existing owner when
        # the caller passes None.
        self._execute(
            "INSERT INTO campaigns (campaign_id, name, config_json, created_at, owner_id) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (campaign_id) DO UPDATE SET name = EXCLUDED.name, config_json = EXCLUDED.config_json, "
            "owner_id = COALESCE(campaigns.owner_id, EXCLUDED.owner_id)",
            (campaign_id, name, json.dumps(config, default=str), utcnow().isoformat(), owner_id),
        )
        self._commit()

    def list_campaigns(self, owner_id: str | None = None) -> list[dict]:
        """Live (not soft-deleted) user-created campaigns, newest first. With owner_id, only that
        owner's campaigns plus legacy shared (NULL-owner) ones; without it (local operator), all."""
        sql = "SELECT campaign_id, name, config_json, created_at, owner_id FROM campaigns WHERE deleted_at IS NULL"
        params: list = []
        if owner_id is not None:
            sql += " AND (owner_id = %s OR owner_id IS NULL)"
            params.append(owner_id)
        sql += " ORDER BY created_at DESC"
        rows = self._execute(sql, params).fetchall()
        return [{"campaign_id": r["campaign_id"], "name": r["name"], "created_at": r["created_at"],
                 "owner_id": r["owner_id"], "config": json.loads(r["config_json"])} for r in rows]

    def all_campaign_ids(self) -> set[str]:
        """Every campaign id ever created, INCLUDING soft-deleted ones. Used to de-dupe a new
        campaign's id so a recreate never reuses (and thus overwrites/resurrects) a kept row."""
        return {r["campaign_id"] for r in self._execute("SELECT campaign_id FROM campaigns").fetchall()}

    def campaign_owner(self, campaign_id: str) -> str | None:
        row = self._execute(
            "SELECT owner_id FROM campaigns WHERE campaign_id = %s AND deleted_at IS NULL", (campaign_id,)
        ).fetchone()
        return row["owner_id"] if row else None

    def delete_campaign(self, campaign_id: str) -> None:
        """Soft delete: stamp deleted_at so it drops out of the UI, but keep the row and its
        leads in the database permanently (for every account). Never a hard DELETE."""
        self._execute(
            "UPDATE campaigns SET deleted_at = %s WHERE campaign_id = %s AND deleted_at IS NULL",
            (utcnow().isoformat(), campaign_id),
        )
        self._commit()

    def hide_campaign(self, campaign_id: str) -> None:
        self._execute(
            "INSERT INTO hidden_campaigns (campaign_id, hidden_at) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (campaign_id, utcnow().isoformat()),
        )
        self._commit()

    def unhide_campaign(self, campaign_id: str) -> None:
        self._execute("DELETE FROM hidden_campaigns WHERE campaign_id = %s", (campaign_id,))
        self._commit()

    def hidden_campaign_ids(self) -> set[str]:
        return {r["campaign_id"] for r in self._execute("SELECT campaign_id FROM hidden_campaigns").fetchall()}

    def start_run(self, run_id: str, campaign_id: str) -> None:
        self._execute(
            "INSERT INTO runs (run_id, campaign_id, started_at, status) VALUES (%s, %s, %s, 'running')",
            (run_id, campaign_id, utcnow().isoformat()),
        )
        self._commit()

    def finish_run(self, run_id: str, status: str, stats: dict) -> None:
        self._execute(
            "UPDATE runs SET finished_at = %s, status = %s, stats_json = %s WHERE run_id = %s",
            (utcnow().isoformat(), status, json.dumps(stats, default=str), run_id),
        )
        self._commit()

    def get_run(self, run_id: str) -> dict | None:
        row = self._execute("SELECT * FROM runs WHERE run_id = %s", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, campaign_id: str | None = None) -> list[dict]:
        if campaign_id:
            rows = self._execute(
                "SELECT * FROM runs WHERE campaign_id = %s ORDER BY started_at DESC", (campaign_id,)
            )
        else:
            rows = self._execute("SELECT * FROM runs ORDER BY started_at DESC")
        return [dict(r) for r in rows]

    # -- run progress (polled by GET /campaigns/{id}/progress) --------------

    def set_run_progress(self, campaign_id: str, run_id: str | None, stage: str,
                          done: int, total: int, message: str | None = None) -> None:
        self._execute(
            "INSERT INTO run_progress (campaign_id, run_id, stage, done, total, message, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (campaign_id) DO UPDATE SET run_id = EXCLUDED.run_id, stage = EXCLUDED.stage, "
            "done = EXCLUDED.done, total = EXCLUDED.total, message = EXCLUDED.message, "
            "updated_at = EXCLUDED.updated_at",
            (campaign_id, run_id, stage, done, total, message, utcnow().isoformat()),
        )
        self._commit()

    def get_run_progress(self, campaign_id: str) -> dict | None:
        row = self._execute(
            "SELECT * FROM run_progress WHERE campaign_id = %s", (campaign_id,)
        ).fetchone()
        return dict(row) if row else None

    # -- companies ----------------------------------------------------------

    def upsert_company(self, company_key: str, campaign_id: str, name: str, *,
                       domain: str | None, website: str | None, country: str | None,
                       city: str | None, source: str, source_url: str | None, raw: dict) -> bool:
        """Returns True if the company was new for this campaign.

        Insert-or-update and the new/existing verdict are one atomic statement: the old
        SELECT-then-INSERT let two concurrent discoveries both read "absent" and both count the
        company as new, inflating discovery stats. `xmax = 0` is true only for the row this
        statement just inserted, so it distinguishes a fresh insert from a conflict update."""
        row = self._execute(
            "INSERT INTO companies (company_key, campaign_id, name, domain, website, "
            "country, city, source, source_url, discovered_at, raw_json) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (company_key) DO UPDATE SET campaign_id = EXCLUDED.campaign_id, "
            "name = EXCLUDED.name, domain = EXCLUDED.domain, website = EXCLUDED.website, "
            "country = EXCLUDED.country, city = EXCLUDED.city, source = EXCLUDED.source, "
            "source_url = EXCLUDED.source_url, discovered_at = EXCLUDED.discovered_at, "
            "raw_json = EXCLUDED.raw_json "
            "RETURNING (xmax = 0) AS inserted",
            (company_key, campaign_id, name, domain, website, country, city, source,
             source_url, utcnow().isoformat(), json.dumps(raw, default=str)),
        ).fetchone()
        self._commit()
        return bool(row["inserted"])

    # -- pages --------------------------------------------------------------

    def save_page(self, company_key: str, url: str, kind: str, status_code: int,
                  title: str | None, text: str) -> None:
        self._execute(
            "INSERT INTO pages (url, company_key, kind, status_code, title, text, fetched_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (url) DO UPDATE SET company_key = EXCLUDED.company_key, kind = EXCLUDED.kind, "
            "status_code = EXCLUDED.status_code, title = EXCLUDED.title, text = EXCLUDED.text, "
            "fetched_at = EXCLUDED.fetched_at",
            (url, company_key, kind, status_code, title, text, utcnow().isoformat()),
        )
        self._commit()

    # -- leads --------------------------------------------------------------

    def save_lead(self, lead: Lead, run_id: str | None, company_key: str | None) -> None:
        self._execute(
            "INSERT INTO leads (lead_id, campaign_id, run_id, company_key, company_type, "
            "total_score, priority, outreach_ready, sequence_status, contact_email, data_json, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (lead_id) DO UPDATE SET campaign_id = EXCLUDED.campaign_id, run_id = EXCLUDED.run_id, "
            "company_key = EXCLUDED.company_key, company_type = EXCLUDED.company_type, "
            "total_score = EXCLUDED.total_score, priority = EXCLUDED.priority, "
            "outreach_ready = EXCLUDED.outreach_ready, sequence_status = EXCLUDED.sequence_status, "
            "contact_email = EXCLUDED.contact_email, data_json = EXCLUDED.data_json, "
            "updated_at = EXCLUDED.updated_at",
            (lead.lead_id, lead.campaign_id, run_id, company_key, lead.company_type.value,
             lead.total_score, lead.priority.value, int(lead.outreach_ready),
             lead.sequence_status.value, lead.contact_email,
             lead.model_dump_json(), utcnow().isoformat()),
        )
        self._commit()

    def update_lead(self, lead: Lead) -> None:
        """Update a lead's state without touching run_id/company_key (outreach stages)."""
        self._execute(
            "UPDATE leads SET company_type = %s, total_score = %s, priority = %s, outreach_ready = %s, "
            "sequence_status = %s, contact_email = %s, data_json = %s, updated_at = %s WHERE lead_id = %s",
            (lead.company_type.value, lead.total_score, lead.priority.value, int(lead.outreach_ready),
             lead.sequence_status.value, lead.contact_email, lead.model_dump_json(),
             utcnow().isoformat(), lead.lead_id),
        )
        self._commit()

    def get_lead(self, lead_id: str) -> Lead | None:
        row = self._execute("SELECT data_json FROM leads WHERE lead_id = %s", (lead_id,)).fetchone()
        return Lead.model_validate_json(row["data_json"]) if row else None

    # `order` controls the sort: "score" (default, highest-scoring first – used by the dashboard
    # top-buyers list) or "recent" (newest scraped first – the Leads page, so freshly discovered
    # companies surface at the top rather than sinking by score).
    def list_leads(self, campaign_id: str, *, run_id: str | None = None,
                   min_score: int | None = None, company_type: str | None = None,
                   outreach_ready: bool | None = None, q: str | None = None,
                   order: str = "score",
                   limit: int | None = None, offset: int = 0) -> list[Lead]:
        sql = "SELECT data_json FROM leads WHERE campaign_id = %s"
        params: list = [campaign_id]
        sql, params = self._apply_lead_filters(sql, params, run_id, min_score, company_type,
                                               outreach_ready, q)
        # "score" keeps its original single-key sort (ties fall back to physical/insertion order,
        # which existing callers and tests rely on); only "recent" adds the recency key.
        sql += " ORDER BY updated_at DESC, total_score DESC" if order == "recent" else " ORDER BY total_score DESC"
        if limit is not None:
            sql += " LIMIT %s OFFSET %s"
            params.extend([limit, offset])
        rows = self._execute(sql, params).fetchall()
        return [Lead.model_validate_json(r["data_json"]) for r in rows]

    def count_leads(self, campaign_id: str, *, min_score: int | None = None,
                    company_type: str | None = None,
                    outreach_ready: bool | None = None, q: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS cnt FROM leads WHERE campaign_id = %s"
        params: list = [campaign_id]
        sql, params = self._apply_lead_filters(sql, params, None, min_score, company_type,
                                               outreach_ready, q)
        return self._execute(sql, params).fetchone()["cnt"]

    @staticmethod
    def _apply_lead_filters(sql: str, params: list, run_id, min_score, company_type,
                            outreach_ready, q) -> tuple[str, list]:
        """Shared WHERE-clause builder so list_leads and count_leads stay in lock-step (same
        total as the page they paginate)."""
        if run_id:
            sql += " AND run_id = %s"
            params.append(run_id)
        if min_score is not None:
            sql += " AND total_score >= %s"
            params.append(min_score)
        if company_type:
            sql += " AND company_type = %s"
            params.append(company_type)
        if outreach_ready is not None:
            sql += " AND outreach_ready = %s"
            params.append(int(outreach_ready))
        if q and q.strip():
            # Free-text search over the serialized lead (name, domain, email, city, …). Escape
            # the LIKE metacharacters (\ % _) in the user's term so a query like "%" or "a_b"
            # is matched literally instead of becoming a wildcard that scans/enumerates every row.
            term = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql += " AND data_json ILIKE %s ESCAPE '\\'"
            params.append(f"%{term}%")
        return sql, params

    def leads_by_status(self, campaign_id: str, statuses: list[str]) -> list[Lead]:
        placeholders = ",".join("%s" for _ in statuses)
        rows = self._execute(
            f"SELECT data_json FROM leads WHERE campaign_id = %s AND sequence_status IN ({placeholders}) "
            "ORDER BY total_score DESC", [campaign_id, *statuses]
        ).fetchall()
        return [Lead.model_validate_json(r["data_json"]) for r in rows]

    def campaign_config(self, campaign_id: str) -> dict | None:
        row = self._execute(
            "SELECT config_json FROM campaigns WHERE campaign_id = %s AND deleted_at IS NULL", (campaign_id,)
        ).fetchone()
        return json.loads(row["config_json"]) if row else None

    def campaign_counts(self, campaign_id: str, min_score: int) -> dict:
        """Lead tallies for a campaign in one aggregate query, computed in Postgres from the
        indexed columns rather than by loading and JSON-parsing every Lead in Python. This is
        what /campaigns needs per campaign for the dropdown, and doing it in SQL keeps that
        list fast no matter how many leads a campaign accumulates."""
        row = self._execute(
            "SELECT COUNT(*) AS leads, "
            "COUNT(*) FILTER (WHERE company_type = 'BUYER') AS buyers, "
            "COUNT(*) FILTER (WHERE company_type = 'BUYER' AND total_score >= %s) AS qualified, "
            "COUNT(*) FILTER (WHERE outreach_ready = 1) AS outreach_ready "
            "FROM leads WHERE campaign_id = %s",
            (min_score, campaign_id),
        ).fetchone()
        return {"leads": row["leads"], "buyers": row["buyers"],
                "qualified": row["qualified"], "outreach_ready": row["outreach_ready"]}

    def campaign_stats(self, campaign_id: str, min_score: int) -> dict:
        """Full dashboard stats computed in SQL – no Python deserialization of lead JSON."""
        row = self._execute(
            "SELECT "
            "COUNT(*) AS leads, "
            "COUNT(*) FILTER (WHERE company_type = 'BUYER') AS buyers, "
            "COUNT(*) FILTER (WHERE company_type = 'VENDOR') AS vendors, "
            "COUNT(*) FILTER (WHERE company_type = 'UNKNOWN') AS unknowns, "
            "COUNT(*) FILTER (WHERE company_type = 'BUYER' AND total_score >= %s) AS qualified, "
            "COUNT(*) FILTER (WHERE outreach_ready = 1) AS outreach_ready, "
            "COUNT(*) FILTER (WHERE total_score < 50) AS band_0_49, "
            "COUNT(*) FILTER (WHERE total_score >= 50 AND total_score < 70) AS band_50_69, "
            "COUNT(*) FILTER (WHERE total_score >= 70 AND total_score < 80) AS band_70_79, "
            "COUNT(*) FILTER (WHERE total_score >= 80) AS band_80_100, "
            "COUNT(*) FILTER (WHERE sequence_status = 'email_1_sent') AS st_email_1_sent, "
            "COUNT(*) FILTER (WHERE sequence_status = 'followup_1_sent') AS st_followup_1_sent, "
            "COUNT(*) FILTER (WHERE sequence_status = 'followup_2_sent') AS st_followup_2_sent, "
            "COUNT(*) FILTER (WHERE sequence_status = 'replied') AS st_replied, "
            "COUNT(*) FILTER (WHERE sequence_status = 'bounced') AS st_bounced, "
            "COUNT(*) FILTER (WHERE sequence_status = 'completed') AS st_completed, "
            "COUNT(*) FILTER (WHERE sequence_status = 'unsubscribed') AS st_unsubscribed, "
            "COUNT(*) FILTER (WHERE sequence_status = 'not_queued') AS st_not_queued, "
            "COUNT(*) FILTER (WHERE sequence_status = 'queued') AS st_queued, "
            "COUNT(*) FILTER (WHERE sequence_status = 'suppressed') AS st_suppressed, "
            "COUNT(*) FILTER (WHERE data_json::jsonb->>'review_verdict' IS NOT NULL "
            "  AND data_json::jsonb->>'review_verdict' != '') AS reviewed, "
            "COUNT(*) FILTER (WHERE data_json::jsonb->>'review_verdict' = 'correct') AS verdict_correct, "
            "COUNT(*) FILTER (WHERE data_json::jsonb->>'review_verdict' = 'wrong_company') AS verdict_wrong_company, "
            "COUNT(*) FILTER (WHERE data_json::jsonb->>'review_verdict' = 'wrong_person') AS verdict_wrong_person, "
            "COUNT(*) FILTER (WHERE data_json::jsonb->>'review_verdict' = 'wrong_email') AS verdict_wrong_email, "
            "COUNT(*) FILTER (WHERE jsonb_array_length(COALESCE(data_json::jsonb->'intent_signals', '[]'::jsonb)) > 0) AS with_intent, "
            "COUNT(*) FILTER (WHERE priority = 'high_priority') AS pr_high, "
            "COUNT(*) FILTER (WHERE priority = 'qualified') AS pr_qualified, "
            "COUNT(*) FILTER (WHERE priority = 'review') AS pr_review, "
            "COUNT(*) FILTER (WHERE priority = 'reject') AS pr_reject "
            "FROM leads WHERE campaign_id = %s",
            (min_score, campaign_id),
        ).fetchone()
        by_status = {
            "not_queued": row["st_not_queued"], "queued": row["st_queued"],
            "email_1_sent": row["st_email_1_sent"], "followup_1_sent": row["st_followup_1_sent"],
            "followup_2_sent": row["st_followup_2_sent"], "completed": row["st_completed"],
            "replied": row["st_replied"], "bounced": row["st_bounced"],
            "unsubscribed": row["st_unsubscribed"], "suppressed": row["st_suppressed"],
        }
        sent = row["st_email_1_sent"] + row["st_followup_1_sent"] + row["st_followup_2_sent"] + row["st_replied"] + row["st_bounced"] + row["st_unsubscribed"]
        reviewed = row["reviewed"]
        correct = row["verdict_correct"]
        return {
            "campaign_id": campaign_id,
            "leads": row["leads"],
            "by_type": {"BUYER": row["buyers"], "VENDOR": row["vendors"], "UNKNOWN": row["unknowns"]},
            "by_status": by_status,
            "by_priority": {"high_priority": row["pr_high"], "qualified": row["pr_qualified"],
                            "review": row["pr_review"], "reject": row["pr_reject"]},
            "score_bands": {"0-49": row["band_0_49"], "50-69": row["band_50_69"],
                            "70-79": row["band_70_79"], "80-100": row["band_80_100"]},
            "qualified": row["qualified"], "outreach_ready": row["outreach_ready"],
            "reviewed": reviewed, "correct": correct,
            "accuracy": round(correct / reviewed, 3) if reviewed else None,
            "verdicts": {"correct": correct, "wrong_company": row["verdict_wrong_company"],
                         "wrong_person": row["verdict_wrong_person"], "wrong_email": row["verdict_wrong_email"]},
            "with_intent": row["with_intent"],
            "emails_sent": sent, "replied": row["st_replied"], "bounced": row["st_bounced"],
        }

    def campaign_of_lead(self, lead_id: str) -> str | None:
        """The campaign a lead belongs to, or None if the lead is unknown. Used to scope
        lead-level routes to the campaign's owner without deserialising the whole Lead."""
        row = self._execute("SELECT campaign_id FROM leads WHERE lead_id = %s", (lead_id,)).fetchone()
        return row["campaign_id"] if row else None

    def bounced_today(self, campaign_id: str, day: str, mailbox: str | None = None,
                      legacy_mailbox: str | None = None) -> int:
        """Leads that bounced among those sent on `day` (optionally by one mailbox; leads
        without a recorded mailbox belong to `legacy_mailbox`)."""
        n = 0
        for l in self.leads_by_status(campaign_id, ["bounced"]):
            if not l.last_sent_at or l.last_sent_at.strftime("%Y-%m-%d") != day:
                continue
            owner = (l.mailbox or legacy_mailbox or mailbox or "").lower()
            if mailbox is None or owner == mailbox.lower():
                n += 1
        return n

    def events_today(self, event_type: str, day_prefix: str) -> int:
        return self._execute(
            "SELECT COUNT(*) AS n FROM outreach_events WHERE event_type = %s AND created_at LIKE %s",
            (event_type, day_prefix + "%"),
        ).fetchone()["n"]

    def lead_for_company(self, campaign_id: str, company_key: str) -> Lead | None:
        row = self._execute(
            "SELECT data_json FROM leads WHERE campaign_id = %s AND company_key = %s "
            "ORDER BY updated_at DESC LIMIT 1", (campaign_id, company_key)
        ).fetchone()
        return Lead.model_validate_json(row["data_json"]) if row else None

    # -- suppressions ---------------------------------------------------------

    def add_suppression(self, value: str, kind: str, reason: str | None = None,
                        owner_id: str | None = None) -> None:
        """Suppress a value for one tenant. owner_id None (local operator / CLI / no auth) stores
        it in the shared '' scope that applies to everyone; a real user id scopes it to them."""
        self._execute(
            "INSERT INTO suppressions (value, kind, reason, created_at, owner_id) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (owner_id, value) DO UPDATE SET kind = EXCLUDED.kind, reason = EXCLUDED.reason, "
            "created_at = EXCLUDED.created_at",
            (value.lower().strip(), kind, reason, utcnow().isoformat(), owner_id or ""),
        )
        self._commit()

    def is_suppressed(self, *values: str | None, owner_id: str | None = None) -> bool:
        """True if any value is suppressed. With owner_id set (hosted multi-tenant), only that
        tenant's own suppressions and the shared '' scope count, so one tenant's list never
        silences another's outreach. owner_id None (local operator) matches any scope."""
        vals = [v.lower().strip() for v in values if v]
        if not vals:
            return False
        placeholders = ",".join("%s" for _ in vals)
        if owner_id is None:
            return self._execute(
                f"SELECT 1 FROM suppressions WHERE value IN ({placeholders}) LIMIT 1", vals
            ).fetchone() is not None
        return self._execute(
            f"SELECT 1 FROM suppressions WHERE value IN ({placeholders}) AND owner_id IN (%s, '') LIMIT 1",
            [*vals, owner_id],
        ).fetchone() is not None

    # -- drafts (human approval) ----------------------------------------------

    def get_draft(self, lead_id: str, step: str) -> dict | None:
        row = self._execute("SELECT * FROM drafts WHERE lead_id = %s AND step = %s", (lead_id, step)).fetchone()
        return dict(row) if row else None

    def upsert_draft(self, lead_id: str, step: str, subject: str, body: str, *,
                     status: str = "pending", edited: bool = False) -> dict:
        existing = self.get_draft(lead_id, step)
        created = existing["created_at"] if existing else utcnow().isoformat()
        approved_at = utcnow().isoformat() if status == "approved" else (existing or {}).get("approved_at")
        self._execute(
            "INSERT INTO drafts (lead_id, step, subject, body, status, edited, created_at, approved_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (lead_id, step) DO UPDATE SET subject = EXCLUDED.subject, body = EXCLUDED.body, "
            "status = EXCLUDED.status, edited = EXCLUDED.edited, created_at = EXCLUDED.created_at, "
            "approved_at = EXCLUDED.approved_at",
            (lead_id, step, subject, body, status, int(edited), created, approved_at),
        )
        self._commit()
        return self.get_draft(lead_id, step)

    def set_draft_status(self, lead_id: str, step: str, status: str) -> None:
        self._execute(
            "UPDATE drafts SET status = %s, approved_at = COALESCE(approved_at, %s) WHERE lead_id = %s AND step = %s",
            (status, utcnow().isoformat() if status == "approved" else None, lead_id, step),
        )
        self._commit()

    def drafts_by_status(self, status: str) -> list[dict]:
        return [dict(r) for r in self._execute("SELECT * FROM drafts WHERE status = %s ORDER BY created_at", (status,))]

    def list_suppressions(self, owner_id: str | None = None) -> list[dict]:
        """With owner_id, only that tenant's own suppressions plus the shared '' scope; without it
        (local operator), all of them."""
        if owner_id is None:
            return [dict(r) for r in self._execute("SELECT * FROM suppressions ORDER BY created_at DESC")]
        return [dict(r) for r in self._execute(
            "SELECT * FROM suppressions WHERE owner_id IN (%s, '') ORDER BY created_at DESC", (owner_id,)
        )]

    def remove_suppression(self, value: str, owner_id: str | None = None) -> bool:
        """A tenant can only remove its own suppressions, never the shared '' compliance scope.
        owner_id None (local operator) can remove any."""
        if owner_id is None:
            cur = self._execute("DELETE FROM suppressions WHERE value = %s", (value.lower().strip(),))
        else:
            cur = self._execute(
                "DELETE FROM suppressions WHERE value = %s AND owner_id = %s",
                (value.lower().strip(), owner_id),
            )
        self._commit()
        return cur.rowcount > 0

    # -- outreach events -----------------------------------------------------

    def add_event(self, lead_id: str, event_type: str, step: str | None = None,
                  detail: str | None = None) -> None:
        self._execute(
            "INSERT INTO outreach_events (lead_id, event_type, step, detail, created_at) VALUES (%s, %s, %s, %s, %s)",
            (lead_id, event_type, step, detail, utcnow().isoformat()),
        )
        self._commit()

    def events_for(self, lead_id: str) -> list[dict]:
        return [dict(r) for r in self._execute(
            "SELECT * FROM outreach_events WHERE lead_id = %s ORDER BY event_id", (lead_id,)
        )]

    # -- user API keys (encrypted, per-user) ----------------------------------

    def list_user_keys(self, user_id: str) -> list[dict]:
        return [dict(r) for r in self._execute(
            "SELECT key_name, created_at FROM user_api_keys WHERE user_id = %s ORDER BY key_name",
            (user_id,),
        )]

    def get_user_key(self, user_id: str, key_name: str) -> str | None:
        row = self._execute(
            "SELECT encrypted_value FROM user_api_keys WHERE user_id = %s AND key_name = %s",
            (user_id, key_name),
        ).fetchone()
        return row["encrypted_value"] if row else None

    def set_user_key(self, user_id: str, key_name: str, encrypted_value: str) -> None:
        self._execute(
            "INSERT INTO user_api_keys (user_id, key_name, encrypted_value, created_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (user_id, key_name) DO UPDATE SET encrypted_value = EXCLUDED.encrypted_value, "
            "created_at = EXCLUDED.created_at",
            (user_id, key_name, encrypted_value, utcnow().isoformat()),
        )
        self._commit()

    def delete_user_key(self, user_id: str, key_name: str) -> bool:
        cur = self._execute(
            "DELETE FROM user_api_keys WHERE user_id = %s AND key_name = %s",
            (user_id, key_name),
        )
        self._commit()
        return cur.rowcount > 0

    # -- user mailboxes (encrypted, per-user) ----------------------------------

    def list_user_mailboxes(self, user_id: str) -> list[dict]:
        return [dict(r) for r in self._execute(
            "SELECT address, smtp_host, smtp_port, sender_name, daily_limit, enabled, created_at "
            "FROM user_mailboxes WHERE user_id = %s ORDER BY created_at",
            (user_id,),
        )]

    def get_user_mailbox(self, user_id: str, address: str) -> dict | None:
        row = self._execute(
            "SELECT address, encrypted_password, smtp_host, smtp_port, sender_name, daily_limit, enabled, created_at "
            "FROM user_mailboxes WHERE user_id = %s AND address = %s",
            (user_id, address.strip().lower()),
        ).fetchone()
        return dict(row) if row else None

    def set_user_mailbox(self, user_id: str, address: str, encrypted_password: str | None,
                         smtp_host: str = "smtp.gmail.com", smtp_port: int = 587,
                         sender_name: str | None = None, daily_limit: int | None = None) -> None:
        self._execute(
            "INSERT INTO user_mailboxes (user_id, address, encrypted_password, smtp_host, smtp_port, "
            "sender_name, daily_limit, enabled, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, true, %s) "
            "ON CONFLICT (user_id, address) DO UPDATE SET "
            "encrypted_password = EXCLUDED.encrypted_password, smtp_host = EXCLUDED.smtp_host, "
            "smtp_port = EXCLUDED.smtp_port, sender_name = EXCLUDED.sender_name, "
            "daily_limit = EXCLUDED.daily_limit",
            (user_id, address.strip().lower(), encrypted_password, smtp_host, smtp_port,
             sender_name, daily_limit, utcnow().isoformat()),
        )
        self._commit()

    def delete_user_mailbox(self, user_id: str, address: str) -> bool:
        cur = self._execute(
            "DELETE FROM user_mailboxes WHERE user_id = %s AND address = %s",
            (user_id, address.strip().lower()),
        )
        self._commit()
        return cur.rowcount > 0

    def toggle_user_mailbox(self, user_id: str, address: str, enabled: bool) -> bool:
        cur = self._execute(
            "UPDATE user_mailboxes SET enabled = %s WHERE user_id = %s AND address = %s",
            (enabled, user_id, address.strip().lower()),
        )
        self._commit()
        return cur.rowcount > 0

    # -- usage counts (daily per-user) ----------------------------------------

    def check_and_increment_usage(self, user_id: str, resource: str, limit: int) -> bool:
        """Atomically bump today's usage and report whether this request is within `limit`.

        One statement (INSERT ... ON CONFLICT DO UPDATE ... WHERE ... RETURNING), so two
        concurrent requests can't both read an under-limit count and both proceed – the old
        SELECT-then-UPDATE let a user slip past a daily cap under concurrency. The conditional
        UPDATE is skipped once the cap is reached for the day, so RETURNING yields no row and we
        deny. A new day (different last_reset_date) resets the count to 1 in the same statement."""
        if limit <= 0:
            return False
        today = utcnow().strftime("%Y-%m-%d")
        row = self._execute(
            "INSERT INTO usage_counts (user_id, resource, daily_count, last_reset_date) "
            "VALUES (%s, %s, 1, %s) "
            "ON CONFLICT (user_id, resource) DO UPDATE SET "
            "  daily_count = CASE WHEN usage_counts.last_reset_date <> EXCLUDED.last_reset_date THEN 1 "
            "                     ELSE usage_counts.daily_count + 1 END, "
            "  last_reset_date = EXCLUDED.last_reset_date "
            "WHERE usage_counts.last_reset_date <> EXCLUDED.last_reset_date "
            "   OR usage_counts.daily_count < %s "
            "RETURNING daily_count",
            (user_id, resource, today, limit),
        ).fetchone()
        self._commit()
        return row is not None

    def get_usage(self, user_id: str) -> list[dict]:
        today = utcnow().strftime("%Y-%m-%d")
        rows = self._execute(
            "SELECT resource, daily_count, last_reset_date FROM usage_counts WHERE user_id = %s",
            (user_id,),
        ).fetchall()
        return [
            {"resource": r["resource"],
             "daily_count": r["daily_count"] if r["last_reset_date"] == today else 0}
            for r in rows
        ]

    # -- user preferences -----------------------------------------------------

    def get_preferences(self, user_id: str) -> list[dict]:
        return [dict(r) for r in self._execute(
            "SELECT pref_key, pref_value, updated_at FROM user_preferences WHERE user_id = %s",
            (user_id,),
        )]

    def set_preference(self, user_id: str, pref_key: str, pref_value: str) -> None:
        self._execute(
            "INSERT INTO user_preferences (user_id, pref_key, pref_value, updated_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (user_id, pref_key) DO UPDATE SET pref_value = EXCLUDED.pref_value, "
            "updated_at = EXCLUDED.updated_at",
            (user_id, pref_key, pref_value, utcnow().isoformat()),
        )
        self._commit()
