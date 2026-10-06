"""Configuration schema. Everything that drives qualification and scoring lives here,
not in code, so an ICP change never requires a code change."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class GeographyConfig(BaseModel):
    countries: list[str] = Field(default_factory=lambda: ["Pakistan"])
    # Provinces / states, searched as broader areas than a single city (a province geocodes
    # to a bbox that covers many towns). Optional middle tier between country and city.
    provinces: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    # Specific localities/sectors/neighborhoods within a city (e.g. G-7, DHA, Saddar).
    areas: list[str] = Field(default_factory=list)

    @field_validator("countries", "provinces", "cities", "areas")
    @classmethod
    def _strip(cls, values: list[str]) -> list[str]:
        return [v.strip() for v in values if v and v.strip()]

    def search_areas(self) -> list[str]:
        """Places to geocode into search bboxes: provinces first (broader), then cities.
        De-duped, order preserved, so a run does not scan the same area twice."""
        seen: set[str] = set()
        out: list[str] = []
        for area in [*self.provinces, *self.cities]:
            key = area.lower()
            if key not in seen:
                seen.add(key)
                out.append(area)
        return out


class ScoringWeights(BaseModel):
    """Maximum points per dimension. Must sum to 100.

    The decomposed transparent score (reference xlsx formula):
      review_band 0-30 + rating 0-10 + proximity_tier 0-15
      + online_gap 0-25 + pain_evidence 0-20 = 100
    """

    review_band: int = 30
    rating: int = 10
    proximity_tier: int = 15
    online_gap: int = 25
    pain_evidence: int = 20

    def total(self) -> int:
        return (
            self.review_band
            + self.rating
            + self.proximity_tier
            + self.online_gap
            + self.pain_evidence
        )


class RoutingThresholds(BaseModel):
    high_priority: int = 55
    qualified: int = 40
    review: int = 20


class CampaignConfig(BaseModel):
    campaign_id: str
    name: str
    offer: str = Field(description="What we sell; used only for context and personalization.")
    target_industries: list[str] = Field(default_factory=list)
    geography: GeographyConfig = Field(default_factory=GeographyConfig)
    company_size: str | None = None
    target_roles: list[str] = Field(default_factory=list)
    # Terms that indicate an end-buyer company for this campaign (retailer, clinic, ...).
    buyer_keywords: list[str] = Field(default_factory=list)
    # Campaign-specific vendor terms; merged with the global default negative list.
    negative_keywords: list[str] = Field(default_factory=list)
    # Vendor categories the campaign explicitly wants anyway (rare; e.g. targeting agencies).
    allowed_vendor_keywords: list[str] = Field(default_factory=list)
    # OSM tag filters, e.g. ["shop=*", "shop=clothes", "amenity=clinic"].
    osm_categories: list[str] = Field(default_factory=list)
    # Overture category substrings, e.g. ["clothing", "shoe_store", "supermarket"].
    overture_categories: list[str] = Field(default_factory=list)
    # Web-search discovery queries (E2). Normally derived from the offer at run time; the user
    # rarely sets these directly.
    search_queries: list[str] = Field(default_factory=list)
    # Optional seed list of companies/domains supplied by the user.
    seed_csv: Path | None = None
    # Chamber directories (Pakistan): "kcci" today. Member names are filtered by the campaign's
    # industry/buyer terms plus these extra name keywords (directories carry no sector field).
    chamber_sources: list[str] = Field(default_factory=list)
    chamber_name_keywords: list[str] = Field(default_factory=list)
    # Intent sources: "ppra" turns organisations tendering for the offer into leads.
    intent_sources: list[str] = Field(default_factory=list)
    # Terms that describe what we sell, matched against tender text (in addition to industries).
    intent_keywords: list[str] = Field(default_factory=list)
    min_score: int = 40
    max_companies: int = 150
    max_pages_per_site: int = 6
    allow_multiple_contacts_per_company: bool = False
    # Drop branches of national/international chains (OSM `brand` tag): decisions are not
    # made at the outlet and the only public contact is a customer-care mailbox.
    exclude_chains: bool = False
    # Post-scoring hard filters extracted from NL campaign descriptions.
    # Keys: min_google_reviews (int), max_proximity_tier (int),
    # require_online_gap (list of gap labels like "no_website", "no_app").
    hard_filters: dict = Field(default_factory=dict)
    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    routing: RoutingThresholds = Field(default_factory=RoutingThresholds)

    @field_validator("weights")
    @classmethod
    def _weights_sum(cls, w: ScoringWeights) -> ScoringWeights:
        if w.total() != 100:
            raise ValueError(f"scoring weights must sum to 100, got {w.total()}")
        return w

    @field_validator(
        "target_industries", "target_roles", "buyer_keywords", "negative_keywords",
        "allowed_vendor_keywords", "osm_categories", "overture_categories", "chamber_sources", "chamber_name_keywords", "intent_sources", "intent_keywords",
    )
    @classmethod
    def _lower(cls, values: list[str]) -> list[str]:
        return [v.strip().lower() for v in values if v and v.strip()]


class DefaultRules(BaseModel):
    """Global rule sets shipped in config/defaults/*.yaml. Campaigns extend, never replace."""

    negative_keywords: list[str] = Field(default_factory=list)
    # Self-descriptions typical of service sellers ("our clients include", "hire us").
    vendor_phrases: list[str] = Field(default_factory=list)
    # Discovery categories (OSM tags) that describe sellers of services by definition.
    vendor_categories: list[str] = Field(default_factory=list)
    buyer_role_whitelist: list[str] = Field(default_factory=list)
    role_blacklist: list[str] = Field(default_factory=list)
    generic_email_prefixes: list[str] = Field(default_factory=list)
    buying_signal_keywords: dict[str, list[str]] = Field(default_factory=dict)
    pain_signal_keywords: dict[str, list[str]] = Field(default_factory=dict)
    technology_markers: dict[str, list[str]] = Field(default_factory=dict)
    geography_tiers: dict[str, list[str]] = Field(default_factory=dict)
    intent_rfq_phrases: list[str] = Field(default_factory=list)
    intent_hiring_roles: list[str] = Field(default_factory=list)
    # GTM intelligence (job boards / GitHub / press): phrases matched against press/RSS
    # entry titles, grouped by what kind of event they signal.
    press_signal_keywords: dict[str, list[str]] = Field(default_factory=dict)
    # Job-board roles that imply growth/budget rather than routine backfill.
    job_growth_roles: list[str] = Field(default_factory=list)


class EngineSettings(BaseModel):
    """Runtime settings. Loaded from config/engine.yaml, overridable via env GTM_*."""

    db_path: Path = Path("data/gtm.sqlite")
    # Postgres connection string (Supabase pooler URL). Required in any deployed
    # environment; storage.Database refuses to construct without it.
    database_url: str | None = None
    export_dir: Path = Path("data/exports")
    user_agent: str = "GTMLeadEngine/0.1 (+business research; contact via site form)"
    request_timeout_s: float = 15.0
    # Hard cap on a single response body. A handful of 50 MB pages in one batch is enough
    # to exhaust memory; no company website needs more than a few MB of HTML.
    max_response_bytes: int = 4_000_000
    per_host_delay_s: float = 2.0
    max_retries: int = 2
    # After this many consecutive failures, stop calling a host for the rest of the run:
    # a dead or hostile host must not consume the batch's time in retries.
    host_failure_limit: int = 3
    # Hard ceiling on the time spent on one company's website.
    per_company_timeout_s: float = 90.0
    concurrency: int = 4
    respect_robots: bool = True
    # SSRF guard: refuse to fetch URLs whose host is (or resolves to) a non-public address -
    # loopback, private, link-local, cloud-metadata - including IPv6-mapped IPv4 forms. Domains
    # are resolved and checked before the request, which stops a public-looking name that points
    # at a private IP; resolution failures fail open (no address to reach = no SSRF). On for
    # safety; a self-hoster crawling an internal mirror can turn it off. It does not pin the
    # resolved address, so network-level egress rules are still the backstop against DNS
    # rebinding between this check and the connection.
    block_private_hosts: bool = True
    # Render JS-only sites with headless Chromium when static HTTP returns an empty shell.
    # Needs the `browser` extra; off by default because no target site has needed it yet.
    enable_browser_fallback: bool = False
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    # Tried in order when the primary returns an error or rate-limits (shared public instances).
    overpass_mirrors: list[str] = Field(default_factory=lambda: [
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass.private.coffee/api/interpreter",
    ])
    overpass_timeout_s: int = 90
    enable_search_fallback: bool = True
    search_delay_s: float = 5.0
    # Web-search discovery (E2): find companies from the offer's queries, not just map tags.
    # Bounded per run to cap Brave spend; the run's max_companies still caps what gets processed.
    enable_web_search_discovery: bool = True
    web_search_max_queries_per_run: int = 6
    # External signals (all keyless): RDAP domain age, GDELT news mentions (1 req / 5.5 s).
    enable_domain_age: bool = True
    enable_news_signals: bool = True
    news_max_companies_per_run: int = 40
    dns_timeout_s: float = 5.0
    # Mailbox-level email verification: auto | direct | reacher | hunter | off (see validation/verifier.py)
    email_verification: str = "auto"
    reacher_url: str | None = None
    # Try first.last@ style candidates for a named decision-maker when only a generic mailbox is public.
    discover_decision_maker_email: bool = True
    # Optional LLM layer (docs/DIRECTION.md). Off by default; deterministic paths always run.
    enable_llm: bool = False
    llm_provider: str = "auto"      # auto | ollama | groq | gemini
    llm_model: str | None = None
    enable_intent_signals: bool = True
    # GTM intelligence (all keyless, budget-limited like domain age / news): job-board
    # postings (Greenhouse/Lever), GitHub org activity, press/RSS mentions.
    enable_job_board_signals: bool = True
    job_board_max_companies_per_run: int = 40
    enable_github_signals: bool = True
    github_max_companies_per_run: int = 30
    enable_press_signals: bool = True
    press_max_companies_per_run: int = 40
    enable_places_enrichment: bool = False
    google_places_api_key: str | None = None
    places_max_companies_per_run: int = 20
    enable_review_text: bool = False
    # Proximity scoring anchor: the user's office/home location.
    # Companies are ranked by distance from this point (Tier 1/2/3).
    # When unset, all companies get Tier 1 (same-city assumption).
    anchor_lat: float | None = None
    anchor_lon: float | None = None
    proximity_tier1_km: float = 5.0
    proximity_tier2_km: float = 15.0
    # Website finder (Brave) is a metered API on a small monthly free credit. Cap the searches
    # per run so a large max_companies cannot drain the month's budget in one go; companies
    # past the cap keep whatever website discovery already gave them.
    website_finder_max_per_run: int = 60
    log_level: str = "INFO"
