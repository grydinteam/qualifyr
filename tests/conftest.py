import os
import urllib.parse
import uuid
from pathlib import Path

import psycopg
import pytest

from gtm_engine.config.schema import CampaignConfig, DefaultRules, EngineSettings, GeographyConfig, RoutingThresholds
from gtm_engine.config.loader import load_defaults

FIXTURES = Path(__file__).parent / "fixtures"

# Captured at import time: the isolate_credentials fixture strips every GTM_* variable
# from the environment before each test, so reading it later would always come back None.
# GTM_TEST_DATABASE_URL is preferred, so a careless run can't point the suite at the
# production database (each test drops its schema afterwards).
TEST_DSN = os.environ.get("GTM_TEST_DATABASE_URL") or os.environ.get("GTM_DATABASE_URL")


def _dsn_with_schema(schema: str) -> str:
    """Same database, but every unqualified table name resolves inside `schema`.

    Storage.Database re-applies this as an explicit `SET search_path`, because the
    `options=` startup parameter alone is dropped by Supabase's PgBouncer pooler.
    """
    sep = "&" if "?" in TEST_DSN else "?"
    return f"{TEST_DSN}{sep}options={urllib.parse.quote(f'-csearch_path={schema}')}"


def _assert_isolated(dsn: str, schema: str) -> None:
    """Fail loudly if writes would land outside `schema`.

    Worth the extra round-trip: when isolation silently breaks, the suite does not
    error - it writes into the real `public` tables and tests start failing on
    leftover rows from *other* tests, which reads like a logic bug and is expensive
    to trace back to the fixture. (It cost exactly that once already.)
    """
    from gtm_engine.storage.database import Database

    db = Database(dsn, ensure_schema=False)
    try:
        got = db.conn.execute("SELECT current_schema() AS s").fetchone()["s"]
    finally:
        db.close()
    if got != schema:
        raise RuntimeError(
            f"test isolation is broken: expected writes in schema {schema!r}, "
            f"but current_schema() is {got!r}. Refusing to run - the suite would "
            f"write into the real database. Check Database.__init__'s SET search_path."
        )


@pytest.fixture
def pg_schema():
    """An empty, disposable Postgres schema. Gives each test the isolation a throwaway
    SQLite file used to give it, since every test now shares one real database."""
    if not TEST_DSN:
        pytest.skip("set GTM_TEST_DATABASE_URL (or GTM_DATABASE_URL) to run DB-backed tests")
    made: list[str] = []

    def _make() -> str:
        name = f"test_{uuid.uuid4().hex[:16]}"
        with psycopg.connect(TEST_DSN, autocommit=True) as conn:
            conn.execute(f'CREATE SCHEMA "{name}"')
        made.append(name)
        dsn = _dsn_with_schema(name)
        _assert_isolated(dsn, name)
        return dsn

    yield _make

    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        for name in made:
            conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


def pytest_configure(config):
    # Make the DB-test skip loud: without a DSN, ~160 tests silently skip, so a developer can
    # "pass" the suite locally while shipping broken DB code. A config-time warning surfaces it.
    if not TEST_DSN:
        config.issue_config_time_warning(
            pytest.PytestConfigWarning(
                "GTM_TEST_DATABASE_URL (or GTM_DATABASE_URL) is not set: all DB-backed tests "
                "will be SKIPPED. Set it to a throwaway Postgres to run the full suite."
            ),
            stacklevel=2,
        )


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def defaults() -> DefaultRules:
    return load_defaults()


@pytest.fixture
def campaign() -> CampaignConfig:
    return CampaignConfig(
        campaign_id="test-retail",
        name="Retail test",
        offer="Inventory automation",
        target_industries=["retail", "clothing", "fashion"],
        geography=GeographyConfig(countries=["Pakistan"], cities=["Islamabad", "Rawalpindi"]),
        target_roles=["founder", "ceo", "head of ecommerce"],
        buyer_keywords=["retailer", "store", "brand", "outlet", "online store", "shop"],
        osm_categories=["shop=clothes", "shop=furniture"],
        min_score=10,
        routing=RoutingThresholds(qualified=10, high_priority=20),
        max_pages_per_site=4,
    )


@pytest.fixture
def settings(tmp_path: Path, pg_schema) -> EngineSettings:
    return EngineSettings(
        # db_path is still a real directory: outbox .eml files and the dry-run ledger
        # live beside it. Only the datastore itself moved to Postgres.
        db_path=tmp_path / "test.sqlite",
        database_url=pg_schema(),
        export_dir=tmp_path / "exports",
        per_host_delay_s=0.0,
        search_delay_s=0.0,
        max_retries=0,
        respect_robots=False,
        enable_search_fallback=False,
        enable_web_search_discovery=False,
        overpass_url="https://overpass.test/api/interpreter",
        overpass_mirrors=[],
        email_verification="off",
        enable_domain_age=False,
        enable_news_signals=False,
        enable_intent_signals=False,
    )


def bypass_auth(api_main, monkeypatch) -> None:
    """Let an API test reach the routes it is actually testing.

    Every route now sits behind a Supabase bearer token. These tests are about campaign
    and lead behaviour, not the gate, and minting a real ES256 token in each of them would
    only test PyJWT again - tests/test_api_auth.py covers the gate itself, against the real
    verifier. Overriding the dependency is narrower than setting GTM_AUTH_DISABLED, which
    would also switch the OpenAPI routes back on and change what is being tested.
    """
    from gtm_engine.api.auth import verify_request

    monkeypatch.setitem(api_main.app.dependency_overrides, verify_request, lambda: None)


@pytest.fixture(autouse=True)
def isolate_credentials(monkeypatch):
    """Tests must never see a developer's real .env: the engine loads one automatically, and
    a live GTM_BRAVE_API_KEY (or any other) would silently change which code path runs."""
    for key in [k for k in os.environ if k.startswith(("GTM_", "OLLAMA_"))]:
        monkeypatch.delenv(key, raising=False)
