"""End-to-end run for one campaign:
discover -> dedupe -> find website -> crawl -> classify -> enrich -> validate -> score -> store.
Each company is processed independently so one bad site never stalls the batch."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from gtm_engine.config.schema import CampaignConfig, DefaultRules, EngineSettings
from gtm_engine.discovery.chambers import KCCIDirectory
from gtm_engine.discovery.csv_seed import CSVSeedDiscovery
from gtm_engine.discovery.osm import OSMDiscovery
from gtm_engine.discovery.overture import OvertureDiscovery
from gtm_engine.discovery.search import WebsiteFinder
from gtm_engine.discovery.targeting import derive_discovery_targets, load_taxonomy
from gtm_engine.discovery.web_search import WebSearchDiscovery
from gtm_engine.enrichment.contacts import choose_contact
from gtm_engine.enrichment.fieldclean import clean_address, clean_description, prefer_latin_name
from gtm_engine.enrichment.email_patterns import discover, infer_pattern
from gtm_engine.enrichment.external_signals import NewsChecker, domain_age
from gtm_engine.enrichment.github_signals import github_activity
from gtm_engine.enrichment.job_signals import job_board_signals
from gtm_engine.enrichment.press_signals import press_mentions
from gtm_engine.enrichment.hours import parse_opening_hours
from gtm_engine.enrichment.online_presence import audit_online_presence, online_gap_labels
from gtm_engine.enrichment.places import places_enrichment
from gtm_engine.enrichment.research import build_research_brief
from gtm_engine.intent.company_pages import intent_from_pages
from gtm_engine.intent.ppra import PPRATenders
from gtm_engine.llm.client import build_llm
from gtm_engine.llm.tasks import check_discovery_relevance, draft_hook, extract_requirement, extract_review_pain, generate_keywords, generate_pitch_angle, judge_intent
from gtm_engine.qualification.relevance import relevant_terms
from gtm_engine.enrichment.phones import classify_phone
from gtm_engine.enrichment.signals import assess_quality, detect_signals, summarize
from gtm_engine.models import (
    Classification, CompanyQuality, CompanyType, Contact, DiscoveredCompany, EmailStatus, Lead,
    OnlinePresence, SequenceStatus, Signals, new_id,
)
from gtm_engine.qualification.buyer_classifier import BuyerClassifier, TextBundle
from gtm_engine.scoring.proximity import haversine_km
from gtm_engine.scoring.scoring import ScoreInputs, is_outreach_ready, score_lead
from gtm_engine.scraping.fetcher import Fetcher, HttpFetcher
from gtm_engine.scraping.site_crawler import SiteCrawler, SiteSnapshot
from gtm_engine.storage.database import Database
from gtm_engine.validation.dedupe import dedupe_companies
from gtm_engine.validation.domains import canonical_domain, company_key
from gtm_engine.validation.emails import MXChecker, classify_email, is_generic_mailbox
from gtm_engine.validation.liveness import HostResolver
from gtm_engine.validation.verifier import EmailVerifier, MxOnlyVerifier, VerifyStatus, build_verifier

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, int, int, str], Awaitable[None] | None]


@dataclass
class RunStats:
    discovered: int = 0
    after_dedupe: int = 0
    processed: int = 0
    no_website: int = 0
    unreachable: int = 0
    rejected_sites: int = 0      # parked / soft-404 / placeholder / marketplace redirect
    dead_websites: int = 0       # skipped before crawling: the domain no longer resolves
    intent_dropped_irrelevant: int = 0  # hiring/RFQ signals dropped for not matching the offer
    discovery_relevance_dropped: int = 0  # companies dropped post-discovery for not matching offer keywords
    area_proximity_dropped: int = 0  # companies dropped for being too far from the requested area
    llm_relevance_demoted: int = 0  # companies the LLM flagged as not matching the target type
    relevance_keywords: list[str] = field(default_factory=list)  # the offer's need-terms this run used
    discovery_sectors: list[str] = field(default_factory=list)   # sectors derived from the offer (E1)
    buyer: int = 0
    vendor: int = 0
    unknown: int = 0
    qualified: int = 0
    outreach_ready: int = 0
    suppressed: int = 0
    duplicates: int = 0
    chains_excluded: int = 0
    hard_filtered: int = 0
    places_api_calls: int = 0
    errors: int = 0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class RunResult:
    run_id: str
    campaign_id: str
    stats: RunStats
    leads: list[Lead] = field(default_factory=list)


def build_personalization_hook(company: DiscoveredCompany, cls: Classification, signals: Signals) -> str | None:
    """Only facts we observed. Nothing invented."""
    facts: list[str] = []
    if company.city:
        facts.append(f"based in {company.city}")
    if "ecommerce_active" in signals.buying:
        facts.append("sells online")
    if "operations_scale" in signals.buying:
        facts.append("operates multiple branches/outlets")
    if "hiring" in signals.buying:
        facts.append("currently hiring")
    if "expansion" in signals.buying:
        facts.append("recently expanding")
    tech = [t for t in signals.technologies if t in ("shopify", "woocommerce", "magento")]
    if tech:
        facts.append(f"runs on {tech[0]}")
    if "customer_service_load" in signals.pain:
        facts.append("takes orders over WhatsApp/DM")
    for s in signals.intent[:1]:
        if s.get("kind") == "tender":
            facts.append(f"tendering: {s['text'][:70]}")
        elif s.get("kind") == "hiring":
            facts.append(f"hiring a {s['matched_terms'][0]}" if s.get("matched_terms") else "hiring")
        elif s.get("kind") == "rfq":
            facts.append("has a live request for quotations")
    if signals.news:
        facts.append(f"recently in the news ({signals.news[0].get('source', '')})")
    if signals.domain_age_years is not None and signals.domain_age_years < 2:
        facts.append("recently launched online")
    if signals.job_openings:
        growth = [j for j in signals.job_openings if j.get("growth_role")]
        if growth:
            facts.append(f"hiring for {growth[0]['title']}")
        else:
            facts.append(f"{len(signals.job_openings)} open role(s) posted")
    if signals.press_mentions:
        facts.append(f"{signals.press_mentions[0]['kind'].replace('_', ' ')}: {signals.press_mentions[0]['title'][:60]}")
    if signals.github_activity:
        facts.append("actively engineering (public GitHub org)")
    return "; ".join(facts) if facts else None


# Above this confidence, the LLM's intent verdict is allowed to change the buyer/unknown call
# (promote a keyword-thin UNKNOWN to BUYER, or demote a keyword-only BUYER with no real need).
# Below it, the verdict is recorded and lightly scored but does not flip the type.
INTENT_THRESHOLD = 0.6


def _discovery_relevance_filter(
    companies: list[DiscoveredCompany],
    offer_keywords: list[str],
    campaign_categories: set[str] | None = None,
    *,
    user_configured_categories: bool = False,
) -> tuple[list[DiscoveredCompany], int]:
    """Drop map-sourced companies whose name/category/address contain none of the offer keywords.

    Web-search results pass automatically (the query already targeted them).  Companies whose
    discovery category matches one the campaign explicitly requested also pass – but only when
    those categories were explicitly user-configured, not derived from a fallback sector.
    When no keywords are available (generic campaign), everything passes – the filter is a no-op.
    Returns (kept, dropped_count).
    """
    terms = [t.strip().lower() for t in offer_keywords if t and t.strip()]
    if not terms:
        return companies, 0
    cats = campaign_categories or set()
    kept: list[DiscoveredCompany] = []
    dropped = 0
    for c in companies:
        if c.source in ("web_search", "ppra", "kcci", "seed_csv"):
            kept.append(c)
            continue
        if user_configured_categories and c.category and c.category in cats:
            kept.append(c)
            continue
        # A missing category keeps the company: untagged OSM POIs are common and dropping them
        # here costs real recall (the full qualification pipeline still vets them downstream).
        # This is a deliberate recall-over-precision choice - see test_discovery_relevance_filter_passes_no_category.
        if not c.category:
            kept.append(c)
            continue
        haystack = " ".join(filter(None, [c.name, c.category, c.address])).lower()
        if any(t in haystack for t in terms):
            kept.append(c)
        else:
            dropped += 1
    if dropped:
        log.info("discovery relevance filter: kept %d, dropped %d (no offer keyword match)", len(kept), dropped)
    return kept, dropped


def _relevance_target_desc(target_industries: list[str], sectors: list[str]) -> str | None:
    """The business type the LLM relevance check should hold results to. Prefer the user's
    explicit target industries; otherwise fall back to the human term of each derived sector so
    an offer-only campaign (no industries given) still gets the strict type gate instead of
    skipping it. Returns None only when neither is available."""
    industries = [t.strip() for t in (target_industries or []) if t and t.strip()]
    if industries:
        return ", ".join(industries[:3])
    taxonomy = load_taxonomy()
    terms: list[str] = []
    for s in sectors or []:
        if s in ("_niche", "general_retail"):
            continue  # too broad to discriminate on – no useful target type
        match = (taxonomy.get(s) or {}).get("match", [])
        if match:
            terms.append(match[0])
    return ", ".join(terms[:3]) or None


# The area's tolerance is NOT a fixed radius – it is derived at run time from the geocoder's
# bounding box for each requested area, so a dense Lahore block stays tight and a wide Islamabad
# sector stays wide without any per-city tuning. These bounds only clamp pathological geocodes:
# a point-geocode that would be impossibly tight, and a vague match that would be city-wide.
AREA_MIN_HALF_KM = 0.7    # tightest half-extent (packed localities where addresses change fast)
AREA_MAX_HALF_KM = 10.0   # widest half-extent (large neighbourhoods); beyond this it's area-wide
AREA_MARGIN_KM = 0.2      # edge tolerance for a business geocoded just outside the boundary

# A Pakistani sector code: a letter, a hyphen, 1-2 digits, optional sub-sector ("G-11", "F-11",
# "I-10/4"). The hyphen is required so plain phrases ("a 5 star hotel") don't false-match.
_SECTOR_RE = re.compile(r"\b([A-Za-z])-(\d{1,2})(?:/\d)?\b")


def _sector_codes(text: str | None) -> set[str]:
    """Normalised sector codes found in text, e.g. {'G-11'} from 'Sachal Sarmast Road (G-11)'.
    A sub-sector like 'G-11/4' normalises to its parent 'G-11'."""
    return {f"{m.group(1).upper()}-{m.group(2)}" for m in _SECTOR_RE.finditer(text or "")}


@dataclass(frozen=True)
class _Geofence:
    """An axis-aligned lat/lon box, sized from an area's geocoded extent (not a fixed radius)."""
    south: float
    west: float
    north: float
    east: float
    clat: float
    clon: float

    def contains(self, lat: float, lon: float) -> bool:
        return self.south <= lat <= self.north and self.west <= lon <= self.east


def _fence_from_bbox(box) -> "_Geofence":
    """Turn a geocoded bounding box into a geofence whose size reflects the area itself,
    clamped so a point-geocode is never impossibly tight nor a vague match city-wide."""
    clat = (box.south + box.north) / 2
    clon = (box.west + box.east) / 2
    half_h = haversine_km(box.south, clon, box.north, clon) / 2      # km, N-S
    half_w = haversine_km(clat, box.west, clat, box.east) / 2        # km, E-W
    half_h = min(max(half_h, AREA_MIN_HALF_KM), AREA_MAX_HALF_KM) + AREA_MARGIN_KM
    half_w = min(max(half_w, AREA_MIN_HALF_KM), AREA_MAX_HALF_KM) + AREA_MARGIN_KM
    dlat = half_h / 111.0
    dlon = half_w / (111.0 * max(0.1, math.cos(math.radians(clat))))
    return _Geofence(clat - dlat, clon - dlon, clat + dlat, clon + dlon, clat, clon)


async def _area_proximity_filter(
    companies: list[DiscoveredCompany],
    areas: list[str],
    cities: list[str],
    fetcher: Fetcher,
    settings: EngineSettings,
) -> tuple[list[DiscoveredCompany], int]:
    """Drop companies that are not in the requested area(s), with the tolerance decided per run.

    Precedence, per company:
    1. Sector-code match (when the requested area is a sector like 'G-11'): if the company's
       name/address names a sector, keep it only when that sector is one of the requested ones.
       Exact – drops 'F-11 Markaz'/'G-12' even if their coordinates sit next to G-11.
    2. Geofence containment: companies with lat/lon are kept only inside a requested area's
       geofence – a box sized from that area's own geocoded bounding box (tight for a packed
       Lahore block, wide for a large sector), so no fixed radius has to be hand-tuned per city.
    3. Geocode fallback: coordinate-less companies are geocoded (name+address+city) and tested
       the same way; only a total geocode failure passes through.
    Also sets settings.anchor_lat/lon to the fences' centroid for downstream proximity scoring.
    """
    if not areas:
        return companies, 0

    from gtm_engine.discovery.geocode import Geocoder
    geocoder = Geocoder(fetcher, settings.db_path.parent / "geocode_cache.json")

    requested_sectors: set[str] = set()
    for area in areas:
        requested_sectors |= _sector_codes(area)

    fences: list[_Geofence] = []
    city_hint = cities[0] if cities else ""
    for area in areas:
        box = await geocoder.bbox(f"{area} {city_hint}".strip(), None)
        if box:
            fences.append(_fence_from_bbox(box))

    if not fences and not requested_sectors:
        return companies, 0

    if settings.anchor_lat is None and fences:
        settings.anchor_lat = sum(f.clat for f in fences) / len(fences)
        settings.anchor_lon = sum(f.clon for f in fences) / len(fences)

    def _in_any(lat: float, lon: float) -> bool:
        return any(f.contains(lat, lon) for f in fences)

    kept: list[DiscoveredCompany] = []
    dropped = 0
    for c in companies:
        # 1. Exact sector match wins when the company's text names a sector.
        if requested_sectors:
            found = _sector_codes(c.address) | _sector_codes(c.name)
            if found:
                if found & requested_sectors:
                    kept.append(c)
                else:
                    dropped += 1
                continue

        # 2. Geofence containment for companies that carry coordinates.
        clat = c.extra.get("lat") if c.extra else None
        clon = c.extra.get("lon") if c.extra else None
        if clat is not None and clon is not None:
            try:
                clat, clon = float(clat), float(clon)
            except (TypeError, ValueError):
                kept.append(c)
                continue
            if not fences:
                kept.append(c)            # sector-only request, no geofence to compare against
            elif _in_any(clat, clon):
                kept.append(c)
            else:
                dropped += 1
            continue

        # 3. Geocode the coordinate-less company and test it the same way.
        geocode_query = f"{c.name}, {c.address or ''}, {c.city or city_hint}".strip(", ")
        if fences and geocode_query:
            box = await geocoder.bbox(geocode_query, None)
            if box:
                glat = (box.south + box.north) / 2
                glon = (box.west + box.east) / 2
                if _in_any(glat, glon):
                    if c.extra is None:
                        c.extra = {}
                    c.extra["lat"] = glat
                    c.extra["lon"] = glon
                    kept.append(c)
                else:
                    dropped += 1
                continue

        kept.append(c)

    # Fail-safe: dropping EVERY discovered company is almost always a mis-parsed or mis-geocoded
    # area (e.g. "Wah Cantt" read as area "Cantt", or a motorway "M-2" read as a sector), not a
    # genuine "nothing here". Returning an empty run hides that completely. Keep the results
    # unfiltered and log loudly instead, so a geo glitch never silently zeroes a run.
    if companies and not kept:
        log.warning("area filter would drop ALL %d companies for areas=%s sectors=%s – treating "
                    "as a parse/geocode miss and keeping them unfiltered rather than returning an "
                    "empty run", len(companies), ", ".join(areas), ",".join(sorted(requested_sectors)) or "none")
        return companies, 0

    if dropped:
        log.info("area proximity filter: kept %d, dropped %d (areas=%s, sectors=%s)",
                 len(kept), dropped, ", ".join(areas), ",".join(sorted(requested_sectors)) or "none")
    return kept, dropped


def _round_robin(lists: list[list]) -> list:
    """Flatten several source lists by taking one from each in turn (source order preserved
    within a round). Keeps every item; only the order changes, so a downstream cap samples all
    sources instead of draining the first one."""
    out: list = []
    for i in range(max((len(lst) for lst in lists), default=0)):
        for lst in lists:
            if i < len(lst):
                out.append(lst[i])
    return out


def _intent_evidence(company, bundle, signals=None) -> str:
    """The company's own evidence the intent judge reasons over: name, category, description,
    the observed operating signals, and about/body copy. Feeding the signals (multiple outlets,
    ecommerce, hiring, tech, tenders) alongside the page text stops a real multi-outlet buyer
    with a sparse homepage from being under-rated on marketing copy alone. Still its own
    evidence, so the verdict is about evident need, not about our keywords."""
    parts = [
        company.name or "",
        f"Category: {company.category}" if company.category else "",
        bundle.description or "",
    ]
    if signals is not None:
        facts: list[str] = []
        for group in (signals.buying or {}).values():
            facts += list(group)
        for group in (signals.pain or {}).values():
            facts += list(group)
        if facts:
            parts.append("Observed signals: " + "; ".join(facts[:8]))
        if signals.technologies:
            parts.append("Technologies: " + ", ".join(signals.technologies[:8]))
        if signals.job_openings:
            titles = [j.get("title", "") for j in signals.job_openings[:5] if j.get("title")]
            if titles:
                parts.append("Hiring: " + ", ".join(titles))
        for s in (signals.intent or [])[:3]:
            parts.append(f"Intent signal ({s.get('kind')}): {s.get('text', '')[:120]}")
    parts.append((bundle.about_text or bundle.body_text or "")[:3500])
    return "\n".join(p for p in parts if p).strip()


def apply_intent_verdict(cls: Classification, verdict: dict, *, threshold: float = INTENT_THRESHOLD) -> str:
    """Fold an LLM intent verdict into a Classification (mutates it) and return a provenance
    note. A confident buyer promotes a keyword-thin UNKNOWN to BUYER; a confident non-buyer
    demotes a keyword-only BUYER to UNKNOWN. The verdict is always recorded on the
    classification even when it is not strong enough to flip the type. The caller must not pass
    a VENDOR here – that is a hard reject and is never changed by intent."""
    cls.intent_buyer = verdict["buyer"]
    cls.intent_confidence = verdict["confidence"]
    cls.intent_reason = verdict["reason"]
    strong = verdict["confidence"] >= threshold
    pct = f"{verdict['confidence']:.0%}"
    if verdict["buyer"] and strong and cls.company_type == CompanyType.UNKNOWN:
        cls.company_type = CompanyType.BUYER
        cls.confidence = max(cls.confidence, verdict["confidence"])
        cls.reasons.append(f"intent: needs the offer ({pct}) – {verdict['reason']}")
    elif not verdict["buyer"] and strong and cls.company_type == CompanyType.BUYER:
        cls.company_type = CompanyType.UNKNOWN
        cls.reasons.append(f"intent: no evident need for the offer ({pct}) – {verdict['reason']}")
    return f"{verdict['by']}: {'buyer' if verdict['buyer'] else 'not a buyer'} ({pct})"


class Pipeline:
    def __init__(self, settings: EngineSettings, defaults: DefaultRules, db: Database,
                 fetcher: Fetcher, mx: MXChecker | None = None, verifier: EmailVerifier | None = None,
                 resolver: HostResolver | None = None,
                 *, resolved_keys: dict[str, str | None] | None = None):
        self.settings = settings
        self.defaults = defaults
        self.db = db
        self._resolved_keys = resolved_keys or {}
        # The DB is one sync psycopg connection (not thread-safe). Run its calls in a worker
        # thread so a slow query does not freeze the event loop for every other concurrent
        # company, and serialise them with a lock so the single connection is only ever touched
        # by one thread at a time. Net effect: concurrency=N genuinely overlaps the network-bound
        # work (crawl, LLM) instead of stalling on each blocking DB call.
        self._db_lock = asyncio.Lock()
        self.fetcher = fetcher
        self.mx = mx if mx is not None else MXChecker(settings.dns_timeout_s)
        self.resolver = resolver if resolver is not None else HostResolver(settings.dns_timeout_s)
        self.verifier = verifier
        self.website_finder = WebsiteFinder(fetcher, settings,
                                            brave_api_key=self._resolved_keys.get("brave"))
        self.news = NewsChecker(fetcher)
        self._news_budget = settings.news_max_companies_per_run
        self._website_finder_budget = settings.website_finder_max_per_run
        self._job_board_budget = settings.job_board_max_companies_per_run
        self._github_budget = settings.github_max_companies_per_run
        self._press_budget = settings.press_max_companies_per_run
        self._places_budget = settings.places_max_companies_per_run
        self.ppra = PPRATenders(fetcher, settings)
        self.llm = build_llm(
            settings.llm_provider, settings.llm_model,
            groq_api_key=self._resolved_keys.get("groq"),
            gemini_api_key=self._resolved_keys.get("gemini"),
        ) if settings.enable_llm else None
        if self.llm:
            log.info("llm layer: %s", self.llm.name)
        # The need-terms a hiring/intent signal must mention to count for this campaign
        # (what we SELL, not the buyer's sector). Filled per run by _build_relevance_keywords.
        self._relevance_keywords: list[str] = []

    async def _build_relevance_keywords(self, campaign: CampaignConfig) -> list[str]:
        """Terms that make a hiring/intent signal relevant to this offer. The campaign's own
        intent_keywords, plus keywords the LLM derives from the offer (or a deterministic
        fallback of the offer's words). Deliberately excludes target_industries: the sector a
        company is in does not make its hiring relevant to what we sell - that is exactly the
        'Imtiaz was hiring, but for their own retail floor' false positive we are removing."""
        need = list(campaign.intent_keywords)
        try:
            need += await generate_keywords(self.llm, campaign.offer)  # industries omitted on purpose
        except Exception as exc:  # noqa: BLE001 - relevance is a filter, never fatal
            log.debug("keyword generation failed: %s", exc)
        seen: set[str] = set()
        out: list[str] = []
        for t in need:
            t = (t or "").strip().lower()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out

    async def _verifier(self) -> EmailVerifier:
        if self.verifier is None:
            self.verifier = await build_verifier(
                self.settings.email_verification, self.settings.reacher_url,
                hunter_api_key=self._resolved_keys.get("hunter"))
            log.info("email verifier: %s", self.verifier.name)
        return self.verifier

    # -- discovery ---------------------------------------------------------------

    async def _db_call(self, fn, *args, **kwargs):
        """Run a blocking DB method off the event loop, one at a time (the single connection is
        not thread-safe). Used for the per-company writes/reads that run under concurrency."""
        async with self._db_lock:
            return await asyncio.to_thread(fn, *args, **kwargs)

    async def discover(self, campaign: CampaignConfig, progress: ProgressFn | None = None) -> list[DiscoveredCompany]:
        sources = []
        if campaign.overture_categories and campaign.geography.search_areas():
            sources.append(OvertureDiscovery(self.fetcher, self.settings))
        if campaign.osm_categories and campaign.geography.search_areas():
            sources.append(OSMDiscovery(self.fetcher, self.settings))
        if self.settings.enable_web_search_discovery and campaign.search_queries:
            sources.append(WebSearchDiscovery(self.fetcher, self.settings,
                                             brave_api_key=self._resolved_keys.get("brave")))
        if "kcci" in campaign.chamber_sources:
            sources.append(KCCIDirectory(self.fetcher, self.settings))
        if "ppra" in campaign.intent_sources:
            sources.append(self.ppra)
        if campaign.seed_csv:
            sources.append(CSVSeedDiscovery(campaign.seed_csv))
        if not sources:
            log.warning("no discovery sources configured (need osm_categories+cities or seed_csv)")
        per_source: list[list[DiscoveredCompany]] = []
        for src in sources:
            items: list[DiscoveredCompany] = []
            async for company in src.discover(campaign):
                items.append(company)
            per_source.append(items)
            await _emit(progress, "discover", sum(len(s) for s in per_source), 0, f"{src.name}: {len(items)}")
        # Round-robin across sources so a small max_companies cap still samples every source. A
        # dense source (Overture/OSM returns thousands) would otherwise exhaust the cap before a
        # single web-search or chamber result is ever processed - which was exactly the case that
        # made web-search discovery contribute nothing on a real run.
        return _round_robin(per_source)

    # -- single company ----------------------------------------------------------

    async def process_company(self, company: DiscoveredCompany, campaign: CampaignConfig,
                              classifier: BuyerClassifier, run_id: str, stats: RunStats,
                              seen_keys: set[str] | None = None) -> Lead | None:
        """Returns None when the company turns out to duplicate one already processed
        in this run (its website, found by search, belongs to an earlier company)."""
        website = company.website
        if not website and self._website_finder_budget > 0:
            self._website_finder_budget -= 1
            website = await self.website_finder.find(company.name, company.city, company.country)
            if website:
                company = company.model_copy(update={"website": website, "domain": canonical_domain(website),
                                                     "source": f"{company.source}+search"})
        domain = company.domain or canonical_domain(website)
        key = company_key(domain, company.name, company.city)
        if seen_keys is not None:
            if key in seen_keys:
                stats.duplicates += 1
                log.info("skipping %s: %s already processed this run", company.name, key)
                return None
            seen_keys.add(key)
        await self._db_call(self.db.upsert_company, key, campaign.campaign_id, company.name, domain=domain,
                            website=website, country=company.country, city=company.city, source=company.source,
                            source_url=company.source_url, raw=company.model_dump(mode="json"))

        provenance: dict[str, str] = {"company": f"{company.source}: {company.source_url or 'record'}"}
        if not website:
            stats.no_website += 1
            snapshot = SiteSnapshot(website="", final_url=None, reachable=False, https=False, error="no_website")
        else:
            crawler = SiteCrawler(self.fetcher, campaign.max_pages_per_site,
                                  deadline_s=self.settings.per_company_timeout_s)
            snapshot = await crawler.crawl(website)
            if snapshot.redirected_to and snapshot.redirected_to != domain:
                # The site moved: the lead belongs to the domain that actually answers.
                provenance["domain"] = f"{domain or website} redirects to {snapshot.redirected_to}"
                domain = snapshot.redirected_to
                new_key = company_key(domain, company.name, company.city)
                if seen_keys is not None and new_key in seen_keys and new_key != key:
                    # Two records that redirect to the same site are one company.
                    stats.duplicates += 1
                    log.info("skipping %s: redirects to %s, already processed", company.name, domain)
                    return None
                if seen_keys is not None:
                    seen_keys.discard(key)
                    seen_keys.add(new_key)
                key = new_key
            if not snapshot.reachable:
                stats.unreachable += 1
                if snapshot.integrity_reason:
                    stats.rejected_sites += 1
                    provenance["website_rejected"] = f"{snapshot.integrity_reason}: {snapshot.integrity_detail}"
            for kind, page in snapshot.pages.items():
                await self._db_call(self.db.save_page, key, page.url, kind, 200, page.title, page.text[:5000])

        home = snapshot.pages.get("home")
        about = snapshot.pages.get("about")
        bundle = TextBundle(
            name=company.name,
            title=home.title if home else None,
            description=(home.description if home else None) or (about.description if about else None),
            about_text=(about.text if about else None) or (home.text if home else None),
            body_text=snapshot.all_text,
            category=company.category,
            site_reachable=snapshot.reachable,
            tender_terms=(company.extra.get("intent") or {}).get("matched_terms") or None,
        )
        cls = classifier.classify(bundle)
        quality = assess_quality(snapshot, company.name, domain)
        signals = detect_signals(snapshot, self.defaults) if snapshot.reachable else Signals()
        online_presence = audit_online_presence(snapshot, company.name, signals.technologies)

        # Opening hours from OSM discovery tags
        osm_hours = company.extra.get("opening_hours")
        if osm_hours:
            parsed = parse_opening_hours(osm_hours)
            if parsed:
                online_presence.opening_hours_raw = parsed.raw
                online_presence.opening_hours_days = parsed.days_open

        # Google Places enrichment (rating, review count, hours, reviews) – budget-limited
        places_key = self._resolved_keys.get("places") or self.settings.google_places_api_key
        if (self.settings.enable_places_enrichment
                and places_key
                and cls.company_type == CompanyType.BUYER
                and self._places_budget > 0):
            self._places_budget -= 1
            stats.places_api_calls += 1
            places = await places_enrichment(
                self.fetcher, company.name,
                company.city or "Pakistan",
                company.country or "Pakistan",
                places_key,
                include_reviews=self.settings.enable_review_text,
            )
            if places:
                online_presence.google_rating = places.rating
                online_presence.google_review_count = places.user_rating_count
                online_presence.google_place_id = places.place_id
                if places.opening_hours and not online_presence.opening_hours_raw:
                    online_presence.opening_hours_raw = str(places.opening_hours)
                provenance["google_places"] = f"place_id={places.place_id}, rating={places.rating}"
                if places.review_texts:
                    pains = await extract_review_pain(self.llm, company.name, places.review_texts)
                    if pains:
                        online_presence.pain_from_reviews = pains
                        provenance["review_pain"] = f"{len(pains)} pain(s) from {len(places.review_texts)} review(s)"

        # A site that does not belong to this company cannot supply its contact details:
        # its email, phone and staff names belong to somebody else.
        trust_site = snapshot.reachable and not quality.website_mismatch
        if snapshot.reachable and quality.website_mismatch:
            provenance["website_rejected"] = "website does not appear to belong to this company; its contact details were not used"
        if trust_site:
            contact = choose_contact(snapshot, campaign, self.defaults, domain)
            if contact.email:
                provenance["contact_email"] = contact.email_source or "website"
            if contact.name:
                provenance["contact_name"] = f"team/about page: {contact.source_url}"
            if contact.phone:
                provenance["phone"] = "website"
        else:
            src_phone = classify_phone(company.phone)
            contact = Contact(
                email=company.email, phone=company.phone,
                phone_type=src_phone.kind if src_phone else None,
                email_status=EmailStatus.UNVERIFIED if company.email else EmailStatus.NONE,
                email_source=f"{company.source} record" if company.email else None,
                evidence="from discovery source only" if not quality.website_mismatch
                         else "website looked like a different company; using the discovery record only",
            )
            if company.email:
                provenance["contact_email"] = f"{company.source} record"
        rep = company.extra.get("representative")
        if rep and not contact.name:
            contact.name, contact.role = rep, f"Member representative ({company.source.upper()})"
            contact.is_decision_maker = True
            contact.evidence = f"registered representative in the {company.source.upper()} member directory"
            provenance["contact_name"] = f"{company.source} directory: {company.source_url}"
        # Discovery-source contact details fill gaps the website did not (waterfall).
        if not contact.email and company.email:
            contact.email = company.email
            contact.email_source = f"{company.source} record"
            provenance["contact_email"] = f"{company.source} record"
        if not contact.phone and company.phone:
            p = classify_phone(company.phone)
            contact.phone, contact.phone_type = company.phone, (p.kind if p else None)
            provenance["phone"] = f"{company.source} record"

        if cls.company_type != CompanyType.VENDOR and contact.email:
            contact.email_status = await classify_email(contact.email, self.defaults.generic_email_prefixes, self.mx)

        # Decision-maker email discovery: a named person but only a generic mailbox (or none).
        if (cls.company_type == CompanyType.BUYER and self.settings.discover_decision_maker_email
                and contact.is_decision_maker and contact.name and domain
                and contact.email_status in (EmailStatus.GENERIC, EmailStatus.NONE, EmailStatus.INVALID)):
            verifier = await self._verifier()
            known = [e for e in snapshot.emails if e.endswith("@" + domain)
                     and not is_generic_mailbox(e, self.defaults.generic_email_prefixes)]
            known_pattern = next((p for e in known for p in [infer_pattern(e, contact.name)] if p), None)
            found = await discover(contact.name, domain, verifier, known_pattern=known_pattern,
                                   generic_prefixes=self.defaults.generic_email_prefixes)
            provenance["email_discovery"] = f"{verifier.name}: {found.reason}; tried {found.tried}"
            if found.status == VerifyStatus.DELIVERABLE and found.email:
                contact.email, contact.email_status = found.email, EmailStatus.DELIVERABLE
                contact.email_pattern = found.pattern
                contact.email_source = f"pattern {found.pattern}, confirmed by {verifier.name}"
                provenance["contact_email"] = contact.email_source
            elif found.email:
                # Keep the generic mailbox as the sendable address; surface the guess for the reviewer.
                contact.candidate_email = found.email
                contact.email_pattern = found.pattern

        # Intent: tenders naming this organisation, plus RFQ/hiring phrases on its own pages.
        if self.settings.enable_intent_signals and cls.company_type != CompanyType.VENDOR:
            intents = []
            if company.extra.get("intent"):
                intents.append(dict(company.extra["intent"]))
            if trust_site:
                # Strict relevance (P2): a hiring/RFQ phrase on the company's own pages only
                # counts if it mentions what we sell. With no relevance keywords (offer blank,
                # no LLM) the gate is a no-op, preserving the old behaviour.
                for s in intent_from_pages(snapshot, self.defaults):
                    rel = relevant_terms(s.text, self._relevance_keywords)
                    if self._relevance_keywords and not rel:
                        stats.intent_dropped_irrelevant += 1
                        continue
                    sig = s.model_dump(mode="json")
                    if rel:
                        sig["relevance"] = rel
                    intents.append(sig)
            if company.source != "ppra" and ("ppra" in campaign.intent_sources or self.settings.enable_intent_signals):
                try:
                    intents += [s.model_dump(mode="json") for s in await self.ppra.signals_for(company.name, campaign)]
                except Exception as exc:  # noqa: BLE001 - intent is additive, never blocking
                    log.debug("ppra match failed for %s: %s", company.name, exc)
            for sig in intents:
                if sig.get("kind") == "tender" and self.llm and not sig.get("extracted"):
                    sig["extracted"] = await extract_requirement(self.llm, sig.get("text", ""))
            if intents:
                signals.intent = intents
                signals.buying.setdefault("intent", []).extend(f"{s['kind']}: {s['text'][:60]}" for s in intents[:2])
                provenance["intent"] = "; ".join(f"{s['source']} {s['kind']}" + (f" ({s['source_url']})" if s.get('source_url') else "") for s in intents[:3])

        if cls.company_type == CompanyType.BUYER and snapshot.reachable:
            if self.settings.enable_domain_age and domain:
                age = await domain_age(self.fetcher, domain)
                if age:
                    signals.domain_age_years, signals.domain_age_note = age.years, age.note or None
                    provenance["domain_age"] = f"{age.source}: {age.note or f'registered {age.registered:%Y-%m-%d}'}"
            if self.settings.enable_news_signals and self._news_budget > 0:
                self._news_budget -= 1
                mentions = await self.news.mentions(company.name, company.country or "Pakistan")
                if mentions:
                    signals.news = [m.__dict__ for m in mentions]
                    signals.buying.setdefault("news_mention", []).extend(m.title for m in mentions[:2])
                    provenance["news"] = f"gdelt: {len(mentions)} article(s), latest {mentions[0].date}"

            # GTM intelligence: job-board postings, GitHub activity, press/RSS mentions.
            if self.settings.enable_job_board_signals and self._job_board_budget > 0:
                self._job_board_budget -= 1
                jb = await job_board_signals(self.fetcher, company.name, domain, self.defaults)
                if jb.postings:
                    signals.job_openings = [p.__dict__ for p in jb.postings]
                    growth = [p for p in jb.postings if p.growth_role]
                    label = "growth-role hiring" if growth else "hiring"
                    signals.buying.setdefault("job_openings", []).append(
                        f"{len(jb.postings)} open role(s) on {jb.board} ({label})")
                    provenance["job_openings"] = f"{jb.board}: {len(jb.postings)} posting(s), slug '{jb.slug}'"

            if self.settings.enable_github_signals and self._github_budget > 0:
                self._github_budget -= 1
                gh = await github_activity(self.fetcher, company.name, domain)
                if gh:
                    signals.github_activity = gh.__dict__
                    signals.buying.setdefault("github_activity", []).append(
                        f"{gh.public_repos} public repo(s), last pushed {gh.last_pushed_at}")
                    provenance["github_activity"] = f"github: org '{gh.org}', {gh.public_repos} repo(s)"

            if self.settings.enable_press_signals and self._press_budget > 0:
                self._press_budget -= 1
                press = await press_mentions(self.fetcher, snapshot.final_url or website, self.defaults)
                if press:
                    signals.press_mentions = [p.__dict__ for p in press]
                    signals.buying.setdefault("press_mention", []).extend(
                        f"{p.kind}: {p.title[:60]}" for p in press[:2])
                    provenance["press_mentions"] = f"rss: {len(press)} entr(y/ies), latest '{press[0].title[:60]}'"

        # Intent (CEO: qualify by NEED, not keywords). When the LLM is on, judge whether this
        # company plausibly needs the offer from its own text, and let that drive the type and
        # the score. A confident "not a buyer" demotes a keyword-only BUYER to UNKNOWN; a
        # confident buyer promotes an UNKNOWN. VENDOR (agency/competitor) is a hard reject and
        # is never promoted. Without the LLM this is skipped and the keyword path stands.
        if self.llm and cls.company_type != CompanyType.VENDOR:
            verdict = await judge_intent(self.llm, campaign.offer, _intent_evidence(company, bundle, signals))
            if verdict:
                provenance["intent_fit"] = apply_intent_verdict(cls, verdict)

        score = score_lead(ScoreInputs(company, cls, quality, contact, signals, online_presence, self.settings), campaign)
        ready = is_outreach_ready(cls, score, contact, campaign)
        suppressed = await self._db_call(self.db.is_suppressed, domain, contact.email,
                                         owner_id=getattr(self, "_owner_id", None))
        if suppressed:
            ready = False
            stats.suppressed += 1

        buying, pain = summarize(signals)
        gaps = online_gap_labels(online_presence)
        pitch = await generate_pitch_angle(
            self.llm, campaign.offer, company.name,
            online_gaps=gaps,
            pain_signals=list(signals.pain.keys()) if signals.pain else [],
            buying_signals=list(signals.buying.keys()) if signals.buying else [],
        )
        # Clean, human-shareable fields: a Latin-script name, a single-line address, and a
        # description that is real prose (meta or the grounded brief) – never a nav-menu scrape.
        display_name = prefer_latin_name(company.name, bundle.title)
        clean_addr = clean_address(company.address)
        clean_meta = clean_description(bundle.description, None)
        research = build_research_brief(company, cls, contact, signals, city=company.city,
                                        industry=company.category, description=clean_meta,
                                        quality=quality, online_presence=online_presence)
        description = clean_description(bundle.description, research)
        lead = Lead(
            campaign_id=campaign.campaign_id,
            company_name=display_name,
            domain=domain,
            website=snapshot.final_url or website,
            country=company.country,
            city=company.city,
            address=clean_addr,
            industry=company.category,
            company_description=description,
            company_type=cls.company_type,
            buyer_fit_score=score.review_band + score.rating_score,
            buyer_fit_reason="; ".join(cls.reasons),
            company_quality_score=score.proximity_tier,
            buying_signal_score=score.pain_evidence,
            total_score=score.total,
            score_reason="; ".join(score.reasons),
            contact_name=contact.name,
            contact_role=contact.role,
            contact_email=contact.email,
            email_status=contact.email_status,
            phone=contact.phone or company.phone,
            phone_type=contact.phone_type,
            candidate_email=contact.candidate_email,
            news_mentions=signals.news,
            domain_age_years=signals.domain_age_years,
            intent_signals=signals.intent,
            job_openings=signals.job_openings,
            github_activity=signals.github_activity,
            press_mentions=signals.press_mentions,
            provenance=provenance,
            linkedin_or_public_profile_url=contact.profile_url,
            pain_signal=pain,
            buying_signal=buying,
            personalization_hook=build_personalization_hook(company, cls, signals),
            pitch_angle=pitch,
            intent_fit=cls.intent_buyer,
            intent_confidence=cls.intent_confidence,
            intent_reason=cls.intent_reason,
            online_presence=online_presence.model_dump(mode="json"),
            research_brief=research,
            source=company.source,
            source_url=company.source_url,
            outreach_ready=ready,
            sequence_status=SequenceStatus.SUPPRESSED if suppressed else SequenceStatus.NOT_QUEUED,
            priority=score.priority,
            technologies=signals.technologies,
            evidence={
                "classification": cls.model_dump(mode="json"),
                "score": score.model_dump(mode="json"),
                "quality": quality.model_dump(mode="json"),
                "contact_evidence": contact.evidence,
                "pages": {k: p.url for k, p in snapshot.pages.items()},
                "crawl_error": snapshot.error,
            },
        )
        # One lead per company per campaign: reuse the id so re-runs update in place.
        previous = await self._db_call(self.db.lead_for_company, campaign.campaign_id, key)
        if previous:
            lead.lead_id = previous.lead_id
            lead.review_verdict, lead.reviewed_at = previous.review_verdict, previous.reviewed_at
            lead.sequence_status = previous.sequence_status if previous.sequence_status != SequenceStatus.NOT_QUEUED else lead.sequence_status
        await self._db_call(self.db.save_lead, lead, run_id, key)
        return lead

    async def _take_live(self, companies: list[DiscoveredCompany], limit: int | None,
                         progress: ProgressFn | None) -> tuple[list[DiscoveredCompany], int]:
        """Fill the run's company budget with domains that still resolve.

        The budget is spent before anything is fetched, so a dead domain costs a whole
        slot and yields nothing. On the first production run that was 60% of them - and
        discovery had found 3854 candidates behind a cap of 120, so the dead ones were
        being paid for while thousands of live ones went untouched.

        Screened in batches rather than all at once: we usually only need to look at a
        little more than `limit` before the budget is full, and resolving all 3854 would
        cost more than it saves.

        Companies with no website are kept as a tail - there is nothing to resolve yet,
        and WebsiteFinder may still turn one up during processing.
        """
        if not limit or limit <= 0:
            return companies, 0

        with_site = [c for c in companies if c.website]
        without_site = [c for c in companies if not c.website]

        # Screening only pays when there is a queue to promote from. With no more
        # candidates than slots, a dead domain's place cannot be refilled, so rejecting
        # it just shrinks the run - and a resolver wrong about one host would cost a
        # company for nothing. Also keeps small and offline runs off the network.
        if len(with_site) <= limit:
            return companies[:limit], 0
        live: list[DiscoveredCompany] = []
        dead = 0
        cursor = 0

        while len(live) < limit and cursor < len(with_site):
            batch = with_site[cursor:cursor + max(limit, 50)]
            cursor += len(batch)
            for company, alive in zip(batch, await asyncio.gather(
                    *(self.resolver.resolves(c.website) for c in batch))):
                if alive:
                    live.append(company)
                    if len(live) >= limit:
                        break
                else:
                    dead += 1
            await _emit(progress, "liveness", len(live), limit,
                        f"{len(live)} live, {dead} dead domains skipped")

        live.extend(without_site[: max(0, limit - len(live))])
        if dead:
            log.info("liveness: skipped %d dead domains to fill %d slots", dead, len(live))
        return live, dead

    # -- full run --------------------------------------------------------------------

    async def run(self, campaign: CampaignConfig, progress: ProgressFn | None = None) -> RunResult:
        run_id = new_id("run")
        stats = RunStats()
        self.db.upsert_campaign(campaign.campaign_id, campaign.name, campaign.model_dump(mode="json"))
        self.db.start_run(run_id, campaign.campaign_id)
        # The tenant this campaign belongs to, so the suppression check skips only this account's
        # do-not-contact list (plus the shared '' scope), never another tenant's. None for
        # file-based examples and legacy shared campaigns, which use the shared scope.
        self._owner_id = self.db.campaign_owner(campaign.campaign_id)
        self._relevance_keywords = await self._build_relevance_keywords(campaign)
        for ind in campaign.target_industries:
            for word in ind.lower().split():
                if word not in self._relevance_keywords and len(word) > 2:
                    self._relevance_keywords.append(word)
        stats.relevance_keywords = self._relevance_keywords
        # Feed the generated keywords into discovery itself, not just the relevance gate: a
        # deep copy (so the caller's config is untouched) whose intent_keywords carry the
        # offer's need-terms, so PPRA tender search/matching and the buyer classifier's
        # tender evidence all target what this campaign actually sells.
        if self._relevance_keywords:
            campaign = campaign.model_copy(deep=True)
            campaign.intent_keywords = self._relevance_keywords
        # Offer-driven discovery (E1 categories + E2 web-search queries). Derive from the offer
        # to fill map categories the user did not hand-pick and to produce web-search queries.
        # Explicit user map categories always win - deriving only fills the gap, never overrides.
        user_configured_categories = bool(campaign.osm_categories or campaign.overture_categories)
        needs_categories = not user_configured_categories
        if campaign.offer and (needs_categories or not campaign.search_queries):
            targets = await derive_discovery_targets(
                campaign.offer, campaign.target_industries, self.llm,
                cities=campaign.geography.cities, countries=campaign.geography.countries,
                areas=campaign.geography.areas)
            if targets.osm_categories or targets.overture_categories or targets.search_queries:
                # Copy before mutating, unless the relevance step already made a private copy.
                if not self._relevance_keywords:
                    campaign = campaign.model_copy(deep=True)
                if needs_categories:
                    campaign.osm_categories = targets.osm_categories
                    campaign.overture_categories = targets.overture_categories
                    stats.discovery_sectors = targets.sectors
                if not campaign.search_queries:
                    campaign.search_queries = targets.search_queries
        log.info("run %s started for campaign %s; relevance keywords: %s; sectors: %s",
                 run_id, campaign.campaign_id, self._relevance_keywords[:12], stats.discovery_sectors)
        try:
            discovered = await self.discover(campaign, progress)
            stats.discovered = len(discovered)
            if campaign.exclude_chains:
                before = len(discovered)
                discovered = [c for c in discovered if not c.extra.get("brand")]
                stats.chains_excluded = before - len(discovered)
            if campaign.geography.areas:
                discovered, stats.area_proximity_dropped = await _area_proximity_filter(
                    discovered, campaign.geography.areas, campaign.geography.cities,
                    self.fetcher, self.settings)
            campaign_cats = set(campaign.osm_categories) | {f"overture={c}" for c in campaign.overture_categories}
            discovered, stats.discovery_relevance_dropped = _discovery_relevance_filter(
                discovered, self._relevance_keywords, campaign_cats,
                user_configured_categories=user_configured_categories)
            target_desc = _relevance_target_desc(campaign.target_industries, stats.discovery_sectors)
            if self.llm and target_desc and discovered:
                map_sourced = [c for c in discovered if c.source not in ("web_search", "ppra", "kcci", "seed_csv")]
                if map_sourced:
                    batch_dicts = [{"name": c.name, "category": c.category, "address": c.address} for c in map_sourced]
                    verdicts = await check_discovery_relevance(self.llm, target_desc, batch_dicts)
                    rejected = {id(map_sourced[i]) for i, v in enumerate(verdicts) if not v}
                    if rejected:
                        # Quality over quantity: a company the LLM judges is NOT the requested
                        # business type is dropped outright, not just demoted. This is what keeps
                        # a "dentists" search from returning pharmacies and hospitals.
                        stats.llm_relevance_demoted = len(rejected)
                        discovered = [c for c in discovered if id(c) not in rejected]
                        log.info("llm relevance check: dropped %d/%d off-type map-sourced companies",
                                 len(rejected), len(map_sourced))
            companies = dedupe_companies(discovered)
            # Companies that already carry a website are cheaper and better documented; process them first.
            companies.sort(key=lambda c: 0 if c.website else 1)
            companies, stats.dead_websites = await self._take_live(
                companies, campaign.max_companies, progress)
            stats.after_dedupe = len(companies)
            dedupe_msg = f"{len(companies)} unique companies"
            if stats.discovery_relevance_dropped:
                dedupe_msg += f" ({stats.discovery_relevance_dropped} irrelevant dropped)"
            await _emit(progress, "dedupe", len(companies), len(companies), dedupe_msg)

            classifier = BuyerClassifier(campaign, self.defaults)
            leads: list[Lead] = []
            sem = asyncio.Semaphore(self.settings.concurrency)
            # Claimed as each company is processed. Companies that already carry a website are
            # sorted first, so a search- or redirect-found domain can never steal the key of a
            # company that genuinely owns it.
            seen_keys: set[str] = set()

            async def worker(c: DiscoveredCompany) -> None:
                async with sem:
                    try:
                        lead = await self.process_company(c, campaign, classifier, run_id, stats, seen_keys)
                        if lead is None:
                            return
                        leads.append(lead)
                        _tally(stats, lead)
                    except Exception:  # noqa: BLE001 - one company must not kill the batch
                        stats.errors += 1
                        log.exception("failed processing %s", c.name)
                    stats.processed += 1
                    await _emit(progress, "process", stats.processed, len(companies), c.name)

            await asyncio.gather(*(worker(c) for c in companies))
            leads.sort(key=lambda l: l.total_score, reverse=True)
            if campaign.hard_filters:
                before = len(leads)
                leads = _apply_hard_filters(leads, campaign.hard_filters)
                stats.hard_filtered = before - len(leads)
            self.db.finish_run(run_id, "completed", stats.as_dict())
            if stats.places_api_calls:
                log.info("Google Places API: %d calls this run (budget remaining: %d/%d)",
                         stats.places_api_calls, self._places_budget, self.settings.places_max_companies_per_run)
            log.info("run %s finished: %s", run_id, stats.as_dict())
            return RunResult(run_id=run_id, campaign_id=campaign.campaign_id, stats=stats, leads=leads)
        except Exception:
            self.db.finish_run(run_id, "failed", stats.as_dict())
            raise


def _tally(stats: RunStats, lead: Lead) -> None:
    if lead.company_type == CompanyType.BUYER:
        stats.buyer += 1
    elif lead.company_type == CompanyType.VENDOR:
        stats.vendor += 1
    else:
        stats.unknown += 1
    if lead.priority.value in ("high_priority", "qualified") and lead.company_type == CompanyType.BUYER:
        stats.qualified += 1
    if lead.outreach_ready:
        stats.outreach_ready += 1


def _apply_hard_filters(leads: list[Lead], filters: dict) -> list[Lead]:
    """Post-scoring hard filters extracted from NL campaign descriptions."""
    min_reviews = filters.get("min_google_reviews")
    max_tier = filters.get("max_proximity_tier")
    require_gaps = set(filters.get("require_online_gap") or [])

    # Map gap label names to OnlinePresence boolean fields.
    _GAP_CHECKS: dict[str, str] = {
        "no_website": "has_ecommerce_site",
        "no_app": "has_mobile_app",
        "no_whatsapp": "has_whatsapp_ordering",
        "no_facebook": "has_facebook",
        "no_instagram": "has_instagram",
    }

    def _passes(lead: Lead) -> bool:
        op = lead.online_presence or {}
        if min_reviews is not None:
            if (op.get("google_review_count") or 0) < min_reviews:
                return False
        if max_tier is not None:
            score = (lead.evidence or {}).get("score", {})
            tier_pts = score.get("proximity_tier", 0)
            tier_map = {15: 1, 12: 2, 8: 3}
            tier = tier_map.get(tier_pts, 3)
            if tier > max_tier:
                return False
        for gap_label in require_gaps:
            field_name = _GAP_CHECKS.get(gap_label)
            if field_name and op.get(field_name):
                return False  # they HAVE what the filter says should be missing
        return True

    return [l for l in leads if _passes(l)]


async def _emit(progress: ProgressFn | None, stage: str, done: int, total: int, message: str) -> None:
    if progress is None:
        return
    result = progress(stage, done, total, message)
    if asyncio.iscoroutine(result):
        await result
