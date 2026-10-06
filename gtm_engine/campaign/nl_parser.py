"""Natural-language campaign parser.

Accepts free-form text like "find grocery stores in Islamabad that need inventory
software" and produces a CampaignConfig ready to run.  Two stages:

  1. Deterministic extraction – regex/keyword scans, always runs, no LLM needed.
  2. LLM refinement – optional, merges with stage 1 (deterministic wins on conflict).

After extraction the existing derive_discovery_targets() and generate_keywords()
fill in OSM/Overture categories and buyer keywords, so the NL layer only needs to
pull out what the *user* said, not what the taxonomy knows."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from gtm_engine.config.loader import DEFAULTS_DIR, slugify_campaign_id
from gtm_engine.config.schema import CampaignConfig, GeographyConfig
from gtm_engine.discovery.targeting import load_taxonomy, match_sectors

log = logging.getLogger(__name__)

_GEO_PATH = DEFAULTS_DIR / "pk_cities.yaml"

_CITIES: list[str] = []
_PROVINCES: list[str] = []


def _load_geo() -> None:
    global _CITIES, _PROVINCES  # noqa: PLW0603
    if _CITIES:
        return
    try:
        data = yaml.safe_load(_GEO_PATH.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        log.warning("pk_cities.yaml not found at %s", _GEO_PATH)
        return
    _CITIES = [c.strip() for c in data.get("cities", []) if c and c.strip()]
    _PROVINCES = [p.strip() for p in data.get("provinces", []) if p and p.strip()]


@dataclass
class HardFilters:
    min_google_reviews: int | None = None
    max_proximity_tier: int | None = None
    require_online_gap: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d: dict = {}
        if self.min_google_reviews is not None:
            d["min_google_reviews"] = self.min_google_reviews
        if self.max_proximity_tier is not None:
            d["max_proximity_tier"] = self.max_proximity_tier
        if self.require_online_gap:
            d["require_online_gap"] = self.require_online_gap
        return d


@dataclass
class CampaignDraft:
    """Partial campaign extracted from natural language.  Every field is optional;
    the assembler fills defaults for anything the user did not mention."""

    name: str | None = None
    offer: str | None = None
    cities: list[str] = field(default_factory=list)
    areas: list[str] = field(default_factory=list)
    provinces: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=lambda: ["Pakistan"])
    target_industries: list[str] = field(default_factory=list)
    buyer_keywords: list[str] = field(default_factory=list)
    negative_keywords: list[str] = field(default_factory=list)
    sectors: list[str] = field(default_factory=list)
    search_queries: list[str] = field(default_factory=list)
    exclude_chains: bool = False
    max_companies: int | None = None
    min_score: int | None = None
    enable_places: bool = False
    enable_review_text: bool = False
    anchor_lat: float | None = None
    anchor_lon: float | None = None
    hard_filters: HardFilters = field(default_factory=HardFilters)
    raw_text: str = ""


# ---------------------------------------------------------------------------
# Stage 1: deterministic extraction
# ---------------------------------------------------------------------------

_OFFER_PATTERNS = [
    re.compile(r"(?:i|we)\s+(?:sell|offer|provide|make|build|have)\s+(.+?)(?:\.|,|$)", re.I),
    re.compile(r"(?:my|our)\s+(?:product|software|platform|solution|service|tool)\s+(?:is|does|provides)\s+(.+?)(?:\.|,|$)", re.I),
    re.compile(r"(?:selling|offering|providing)\s+(.+?)(?:\.|,|to\s|for\s|$)", re.I),
]

_SUBJECT_PATTERNS = [
    re.compile(r"(?:find|search|look\s+for|show|get|list)\s+(.+?)\s+(?:near|close\s+to|around|in\s+the\s+area\s+of)\b", re.I),
    re.compile(r"(?:find|search|look\s+for|show|get|list)\s+(.+?)\s+in\s+\w", re.I),
    re.compile(r"(?:find|search|look\s+for|show|get|list)\s+(.+?)(?:\.|,|$)", re.I),
]

_STRIP_GEOGRAPHY = re.compile(
    r"\s*\b(?:in|near|around|close\s+to|from)\s+.*$", re.I)
_STRIP_QUANTIFIER = re.compile(
    r"^\s*(?:all|top|best|some|any|the|me)\s+", re.I)

_EXCLUDE_PATTERNS = [
    re.compile(r"(?:ignore|exclude|skip|remove|drop|no)\s+(?:chains?\s+(?:like|such as)\s+)?(.+?)(?:\.|,|$)", re.I),
    re.compile(r"(?:don'?t|do not)\s+(?:include|want|need)\s+(.+?)(?:\.|,|$)", re.I),
]

_MAX_COMPANIES_RE = re.compile(r"(?:top|find|get|show|give)\s+(\d+)\b", re.I)
_REVIEW_INTEREST = re.compile(
    r"(?:review|rating|star|rated|popular|busy|high.?traffic|well.?known|lots?\s+of\s+(?:reviews|customers))", re.I)
_ONLINE_GAP_INTEREST = re.compile(
    r"(?:no\s+(?:website|app|online|digital|ecommerce|e-commerce)"
    r"|don'?t\s+have\s+(?:a\s+)?(?:website|app|online)"
    r"|not\s+(?:digital|online)"
    r"|without\s+(?:a\s+)?(?:website|app|online|complete\s+online)"
    r"|lack(?:s|ing)?\s+(?:a\s+)?(?:complete\s+|proper\s+|strong\s+)?online"
    r"|lack(?:s|ing)?\s+(?:a\s+)?(?:website|digital|ecommerce|e-commerce|online\s+presence|online\s+store)"
    r"|(?:weak|poor|limited|incomplete|missing)\s+online"
    r"|not\s+(?:fully\s+)?online)", re.I)
_NO_APP = re.compile(r"no\s+(?:mobile\s+)?app|without\s+(?:an?\s+)?app", re.I)
_CHAIN_RE = re.compile(r"(?:no|ignore|exclude|skip|drop)\s+(?:big\s+)?chains?\b", re.I)
_TIER_RE = re.compile(r"tier\s*([123])\s*only", re.I)
_MIN_REVIEWS_RE = re.compile(r"(?:more\s+than|at\s+least|minimum|min)\s+(\d+)\s+reviews?", re.I)
_NEAR_RE = re.compile(r"(?:near|close\s+to|around|in\s+the\s+area\s+of)\s+(.+?)(?:\.|,|$)", re.I)
# Residential sector codes use letters A-J (Islamabad/Rawalpindi). The letter is deliberately
# NOT [A-Z]: that matched motorways (M-1, M-2, M-9), national highways (N-5, N-25) and things
# like "T-20" / "G-20" – all read as bogus areas that then geofenced a run down to nothing.
_AREA_RE = re.compile(
    r"\b([A-J]-\d{1,2}(?:/\d)?|[EFGHI]-\d{1,2}|DHA(?:\s+Phase\s*\d+)?|Gulberg|Saddar|Blue\s*Area|Bahria\s*Town"
    r"|Model\s*Town|Garden\s*Town|Johar\s*Town|Cantt|Clifton|Defence|PECHS|Gulshan"
    r"|Askari\s*\d+|Wapda\s*Town|Valencia|Lake\s*City|EME\s*Society|Cavalry\s*Ground"
    r"|Satellite\s*Town|PWD|CDA\s*Sector\s*\w+)\b", re.I)


def _extract_areas(text: str) -> list[str]:
    found: list[str] = []
    for m in _AREA_RE.finditer(text):
        area = m.group(0).strip()
        if area.upper() not in [a.upper() for a in found]:
            found.append(area)
    return found


def _extract_cities(text: str) -> list[str]:
    _load_geo()
    found: list[str] = []
    text_lower = text.lower()
    for city in _CITIES:
        # Whole-word match, not bare substring: "Hub" (a real city) must not fire on "hubs" /
        # "innovation hub"-type phrasing, and short names must not match inside longer words.
        if re.search(rf"\b{re.escape(city.lower())}\b", text_lower):
            if city not in found:
                found.append(city)
    return found


def _extract_provinces(text: str) -> list[str]:
    _load_geo()
    found: list[str] = []
    text_lower = text.lower()
    for prov in _PROVINCES:
        if prov.lower() in text_lower:
            if prov not in found:
                found.append(prov)
    return found


def _extract_offer(text: str) -> str | None:
    for pat in _OFFER_PATTERNS:
        m = pat.search(text)
        if m:
            offer = m.group(1).strip().rstrip(".")
            if len(offer) > 5:
                return offer
    return None


_GENERIC_SUBJECT_WORDS = {
    "companies", "company", "businesses", "business", "individuals", "individual",
    "people", "clients", "customers", "firms", "firm", "organizations",
    "organisations", "organization", "organisation", "ones", "those", "places",
    "vendors", "entities", "players", "owners", "someone", "anyone", "who",
}
# A fragment that STARTS with one of these is a verb/clause ("might need …",
# "looking for …"), not a noun phrase naming an industry – drop it entirely.
_VERB_LEAD_WORDS = {
    "might", "need", "needing", "want", "wanting", "require", "requiring",
    "looking", "seeking", "may", "would", "could", "should", "will", "can",
    "are", "is", "operating", "running", "that", "which", "whose",
}
# Trailing participles that dangle off a noun phrase ("NGOs operating") add noise.
_TRAILING_NOISE_WORDS = {"operating", "running", "based", "located", "situated"}
_LEADING_NOISE_WORDS = {"and", "or", "the", "a", "an"}


def _extract_subject(text: str) -> list[str]:
    """Extract the business types the user is looking for (e.g. "travel agencies, hotels").

    Verbose queries describe buyers in full sentences ("companies and individuals
    who might need vehicle rentals – corporate offices, ..."). Keep only concise
    noun-phrase fragments (<=4 words), dropping generic head-nouns and verb clauses,
    so the result is usable as both discovery terms and a campaign title.
    """
    for pat in _SUBJECT_PATTERNS:
        m = pat.search(text)
        if m:
            raw = m.group(1).strip()
            raw = _STRIP_GEOGRAPHY.sub("", raw).strip()
            raw = _STRIP_QUANTIFIER.sub("", raw).strip()
            if len(raw) < 2:
                continue
            parts = re.split(r"\s+and\s+|\s*,\s*|\s*[––-]\s+|\s+who\s+|\s+that\s+", raw)
            cleaned: list[str] = []
            for p in parts:
                words = p.strip().rstrip(".").split()
                while words and words[0].lower() in _LEADING_NOISE_WORDS:
                    words.pop(0)
                while words and words[-1].lower() in _TRAILING_NOISE_WORDS:
                    words.pop()
                if not words or words[0].lower() in _VERB_LEAD_WORDS:
                    continue
                term = " ".join(words)
                if not (2 <= len(term) and 1 <= len(words) <= 4):
                    continue
                if term.lower() in _GENERIC_SUBJECT_WORDS:
                    continue
                if term.lower() not in [c.lower() for c in cleaned]:
                    cleaned.append(term)
            if cleaned:
                return cleaned
    return []


def _extract_exclusions(text: str) -> tuple[list[str], bool]:
    terms: list[str] = []
    chains = bool(_CHAIN_RE.search(text))
    for pat in _EXCLUDE_PATTERNS:
        for m in pat.finditer(text):
            raw = m.group(1).strip().rstrip(".")
            for part in re.split(r"\s+and\s+|\s*,\s*", raw):
                part = part.strip()
                if part and len(part) > 1:
                    terms.append(part.lower())
    return terms, chains


def _extract_max_companies(text: str) -> int | None:
    m = _MAX_COMPANIES_RE.search(text)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 5000:
            return n
    return None


def _extract_hard_filters(text: str) -> HardFilters:
    hf = HardFilters()
    m = _MIN_REVIEWS_RE.search(text)
    if m:
        hf.min_google_reviews = int(m.group(1))

    m = _TIER_RE.search(text)
    if m:
        hf.max_proximity_tier = int(m.group(1))

    gaps: list[str] = []
    if _ONLINE_GAP_INTEREST.search(text):
        gaps.append("no_website")
    if _NO_APP.search(text):
        gaps.append("no_app")
    hf.require_online_gap = gaps
    return hf


def parse_intent(text: str) -> CampaignDraft:
    """Stage 1: deterministic extraction from free-form text."""
    draft = CampaignDraft(raw_text=text)

    draft.cities = _extract_cities(text)
    draft.provinces = _extract_provinces(text)
    draft.areas = _extract_areas(text)
    # Don't treat a word that is part of a recognised city name as a separate area: "Wah Cantt"
    # is a city, so its "Cantt" token is not an area – otherwise the area geofence drops every
    # result (a bogus "Cantt" location) and the run returns nothing.
    _city_words = {w.lower() for c in draft.cities for w in re.split(r"[\s\-]+", c)}
    draft.areas = [a for a in draft.areas if a.lower() not in _city_words]
    draft.offer = _extract_offer(text)
    draft.target_industries = _extract_subject(text)

    taxonomy = load_taxonomy()
    full_text = text
    if draft.offer:
        full_text = f"{text} {draft.offer}"
    draft.sectors = match_sectors(full_text, draft.target_industries or None, taxonomy)

    neg, chains = _extract_exclusions(text)
    draft.negative_keywords = neg
    draft.exclude_chains = chains

    draft.max_companies = _extract_max_companies(text)

    if _REVIEW_INTEREST.search(text):
        draft.enable_places = True
        draft.enable_review_text = True

    draft.hard_filters = _extract_hard_filters(text)

    return draft


# ---------------------------------------------------------------------------
# Stage 3: assemble a full CampaignConfig
# ---------------------------------------------------------------------------

def _name_from_draft(draft: CampaignDraft) -> str:
    """A short, human-readable campaign title: '<subject> – <area/city>'.

    Never echoes the raw query sentence. Priority for the subject: an LLM-supplied
    name, then concise industry terms, then matched sectors, then the offer; the
    location suffix comes from areas (preferred) or cities.
    """
    if draft.name:
        return " ".join(draft.name.strip().split()[:8])[:60]

    subject = ""
    short_industries = [i for i in draft.target_industries if len(i.split()) <= 4]
    if short_industries:
        subject = ", ".join(s.title() for s in short_industries[:2])
    elif draft.sectors:
        subject = ", ".join(s.replace("_", " ").title() for s in draft.sectors[:2])
    elif draft.offer:
        off = _STRIP_GEOGRAPHY.sub("", draft.offer).strip()
        subject = " ".join(off.split()[:5]).title()

    def _fmt_area(a: str) -> str:
        # Sector codes (g-6, f-11, i-10) read best fully uppercased.
        return a.upper() if re.match(r"^[a-z]-?\d", a, re.I) else a

    loc = ""
    if draft.areas:
        loc = ", ".join(_fmt_area(a) for a in draft.areas[:2])
        if draft.cities:
            loc += f" {draft.cities[0]}"
    elif draft.cities:
        loc = ", ".join(draft.cities[:2])

    if subject and loc:
        name = f"{subject} - {loc}"
    elif subject:
        name = subject
    elif loc:
        name = f"Businesses in {loc}"
    else:
        name = ""
    name = name.strip(" -,")
    if name:
        return name[:60]

    raw = draft.raw_text.strip()
    if len(raw) <= 60:
        return raw
    return raw[:57].rsplit(" ", 1)[0] + "..."


def build_campaign_config(
    draft: CampaignDraft,
    existing_ids: set[str] | None = None,
) -> CampaignConfig:
    """Assemble a valid CampaignConfig from a CampaignDraft.

    The caller is responsible for running derive_discovery_targets() and
    generate_keywords() on the returned config if desired – this function
    only fills in what the NL parser extracted, plus sane defaults.
    """
    name = _name_from_draft(draft)
    cid = slugify_campaign_id(name[:60], existing_ids)

    if draft.offer:
        offer = draft.offer
    elif draft.target_industries:
        offer = (
            f"Find {', '.join(draft.target_industries[:3])} "
            f"in {', '.join(draft.cities) or 'Pakistan'}"
        )
    elif draft.sectors:
        sector_label = ", ".join(s.replace("_", " ") for s in draft.sectors[:3])
        location = ", ".join(draft.cities) or "Pakistan"
        offer = f"Find {sector_label} businesses in {location}"
    else:
        offer = draft.raw_text.strip()[:200]

    search_queries = list(draft.search_queries)
    subject = draft.target_industries[0] if draft.target_industries else None
    if draft.areas and draft.cities:
        for area in draft.areas:
            for city in draft.cities[:1]:
                q = f"{subject or 'stores'} {area} {city}"
                if q not in search_queries:
                    search_queries.append(q)
    elif subject and draft.cities:
        for city in draft.cities[:2]:
            q = f"{subject} {city}"
            if q not in search_queries:
                search_queries.append(q)

    geo = GeographyConfig(
        countries=draft.countries or ["Pakistan"],
        provinces=draft.provinces,
        cities=draft.cities,
        areas=draft.areas,
    )

    hard_filters = draft.hard_filters.as_dict() if draft.hard_filters else {}

    cfg = CampaignConfig(
        campaign_id=cid,
        name=name,
        offer=offer,
        target_industries=draft.target_industries,
        geography=geo,
        buyer_keywords=draft.buyer_keywords,
        negative_keywords=draft.negative_keywords,
        search_queries=search_queries,
        exclude_chains=draft.exclude_chains,
        min_score=draft.min_score or 40,
        max_companies=draft.max_companies or 30,
        hard_filters=hard_filters,
    )
    return cfg


def build_explanation(draft: CampaignDraft, cfg: CampaignConfig) -> dict:
    """Human-readable breakdown of what was interpreted from the NL input."""
    return {
        "name": cfg.name,
        "offer_detected": draft.offer,
        "cities": draft.cities,
        "areas": draft.areas,
        "provinces": draft.provinces,
        "target_industries": draft.target_industries,
        "buyer_keywords": draft.buyer_keywords,
        "sectors_matched": draft.sectors,
        "search_queries": cfg.search_queries,
        "exclusions": draft.negative_keywords,
        "exclude_chains": draft.exclude_chains,
        "max_companies": cfg.max_companies,
        "enable_places": draft.enable_places,
        "enable_review_text": draft.enable_review_text,
        "hard_filters": draft.hard_filters.as_dict(),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def build_campaign_from_nl(
    text: str,
    llm=None,
    existing_ids: set[str] | None = None,
) -> tuple[CampaignConfig, dict]:
    """Full NL->CampaignConfig pipeline.

    Returns (config, explanation).  The config is ready to pass to Pipeline.run()
    after the caller optionally runs derive_discovery_targets().
    """
    from gtm_engine.llm.tasks import parse_campaign_nl

    draft = parse_intent(text)

    if llm is not None:
        taxonomy = load_taxonomy()
        llm_fields = await parse_campaign_nl(llm, text, list(taxonomy.keys()))
        _merge_llm(draft, llm_fields)

    cfg = build_campaign_config(draft, existing_ids)
    explanation = build_explanation(draft, cfg)
    return cfg, explanation


def _merge_llm(draft: CampaignDraft, llm_fields: dict) -> None:
    """Merge LLM-extracted fields into the draft.  Deterministic extractions win
    on conflict (the LLM only fills gaps)."""
    if not llm_fields:
        return

    if not draft.name and llm_fields.get("name"):
        draft.name = llm_fields["name"]

    if not draft.offer and llm_fields.get("offer"):
        draft.offer = llm_fields["offer"]

    if not draft.cities and llm_fields.get("cities"):
        draft.cities = llm_fields["cities"]

    llm_areas = llm_fields.get("areas") or []
    for area in llm_areas:
        if area.upper() not in [a.upper() for a in draft.areas]:
            draft.areas.append(area)

    llm_industries = llm_fields.get("target_industries") or []
    for ind in llm_industries:
        if ind.lower() not in [i.lower() for i in draft.target_industries]:
            draft.target_industries.append(ind)

    llm_keywords = llm_fields.get("buyer_keywords") or []
    for kw in llm_keywords:
        if kw.lower() not in [k.lower() for k in draft.buyer_keywords]:
            draft.buyer_keywords.append(kw)

    llm_neg = llm_fields.get("negative_keywords") or []
    for kw in llm_neg:
        if kw.lower() not in [k.lower() for k in draft.negative_keywords]:
            draft.negative_keywords.append(kw)

    llm_sectors = llm_fields.get("sectors") or []
    for s in llm_sectors:
        if s not in draft.sectors:
            draft.sectors.append(s)

    llm_queries = llm_fields.get("search_queries") or []
    for q in llm_queries:
        if q not in draft.search_queries:
            draft.search_queries.append(q)

    if llm_fields.get("exclude_chains") and not draft.exclude_chains:
        draft.exclude_chains = True

    if not draft.max_companies and llm_fields.get("max_companies"):
        try:
            # Clamp the LLM-supplied value: an unbounded int() lets a hallucinated 999999 drive
            # the crawl into resource exhaustion. 5000 matches the deterministic path's ceiling.
            draft.max_companies = max(1, min(int(llm_fields["max_companies"]), 5000))
        except (ValueError, TypeError):
            pass
