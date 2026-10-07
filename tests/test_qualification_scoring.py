from conftest import fixture
from gtm_engine.enrichment.contacts import choose_contact, role_rank
from gtm_engine.enrichment.signals import assess_quality, detect_signals
from gtm_engine.models import (
    Classification, CompanyQuality, CompanyType, Contact, DiscoveredCompany, EmailStatus, Priority, Signals,
)
from gtm_engine.qualification.buyer_classifier import BuyerClassifier, TextBundle
from gtm_engine.scoring.scoring import ScoreInputs, is_outreach_ready, score_lead
from gtm_engine.scraping.parsers import parse_page
from gtm_engine.scraping.site_crawler import SiteSnapshot


def _snapshot() -> SiteSnapshot:
    snap = SiteSnapshot(website="https://www.zarafabrics.pk", final_url="https://www.zarafabrics.pk/", reachable=True, https=True)
    for kind, name in (("home", "retailer_home.html"), ("about", "retailer_about.html"), ("contact", "retailer_contact.html")):
        html = fixture(name)
        snap.pages[kind] = parse_page(f"https://www.zarafabrics.pk/{kind}", html)
        snap.raw_html[kind] = html
    return snap


def _bundle_from(snap: SiteSnapshot, name: str, category: str | None) -> TextBundle:
    home, about = snap.pages.get("home"), snap.pages.get("about")
    return TextBundle(name=name, title=home.title if home else None,
                      description=home.description if home else None,
                      about_text=about.text if about else (home.text if home else None),
                      body_text=snap.all_text, category=category)


# --- classifier ------------------------------------------------------------------

def test_retailer_is_buyer_despite_agency_footer_credit(campaign, defaults):
    cls = BuyerClassifier(campaign, defaults).classify(_bundle_from(_snapshot(), "Zara Fabrics", "shop=clothes"))
    assert cls.company_type == CompanyType.BUYER
    assert cls.confidence >= 0.7
    assert any("buyer terms in company identity" in r for r in cls.reasons)
    assert "digital marketing" in cls.vendor_hits  # footer credit seen but outweighed


def test_agency_is_vendor(campaign, defaults):
    home = parse_page("https://pixeldigital.pk/", fixture("agency_home.html"))
    bundle = TextBundle(name="Pixel Digital", title=home.title, description=home.description,
                        about_text=home.text, body_text=home.text, category=None)
    cls = BuyerClassifier(campaign, defaults).classify(bundle)
    assert cls.company_type == CompanyType.VENDOR
    assert "agency" in cls.vendor_hits and "software house" in cls.vendor_hits


def test_thin_site_is_unknown(campaign, defaults):
    home = parse_page("https://comingsoon-traders.pk/", fixture("thin_home.html"))
    bundle = TextBundle(name="Comingsoon Traders", title=home.title, description=None,
                        about_text=home.text, body_text=home.text, category=None)
    cls = BuyerClassifier(campaign, defaults).classify(bundle)
    assert cls.company_type == CompanyType.UNKNOWN


def test_category_match_with_reachable_site_gives_buyer(campaign, defaults):
    bundle = TextBundle(name="Nameless Boutique", title="Nameless Boutique", description=None, about_text="Welcome",
                        body_text="welcome", category="shop=clothes", site_reachable=True)
    cls = BuyerClassifier(campaign, defaults).classify(bundle)
    assert cls.company_type == CompanyType.BUYER
    assert 0.5 <= cls.confidence <= 1.0


def test_category_match_without_site_is_unknown(campaign, defaults):
    bundle = TextBundle(name="Nameless Boutique", title=None, description=None, about_text=None, body_text="",
                        category="shop=clothes", site_reachable=False)
    cls = BuyerClassifier(campaign, defaults).classify(bundle)
    assert cls.company_type == CompanyType.UNKNOWN
    assert any("insufficient evidence" in r for r in cls.reasons)


def test_vendor_category_is_vendor_even_if_campaign_queried_it(campaign, defaults):
    campaign.osm_categories = ["office=it"]
    bundle = TextBundle(name="Code Ripples", title="Code Ripples", description=None, about_text="We build software",
                        body_text="we build software for retail stores and brands", category="office=it")
    cls = BuyerClassifier(campaign, defaults).classify(bundle)
    assert cls.company_type == CompanyType.VENDOR and "office=it" in cls.vendor_hits
    campaign.allowed_vendor_keywords = ["office=it"]
    cls = BuyerClassifier(campaign, defaults).classify(bundle)
    assert cls.company_type != CompanyType.VENDOR


def test_vendor_term_in_name_beats_weak_buyer_body(campaign, defaults):
    bundle = TextBundle(name="Retail Growth Consultancy", title="Consultancy for retailers", description=None,
                        about_text="We help retail brands and stores grow.", body_text="retail store brand", category=None)
    cls = BuyerClassifier(campaign, defaults).classify(bundle)
    assert cls.company_type == CompanyType.VENDOR


def test_allowed_vendor_keyword_is_not_negative(campaign, defaults):
    campaign.allowed_vendor_keywords = ["consultancy"]
    bundle = TextBundle(name="Retail Consultancy Store", title=None, description=None, about_text=None,
                        body_text="", category="shop=clothes")
    cls = BuyerClassifier(campaign, defaults).classify(bundle)
    assert cls.company_type == CompanyType.BUYER


# --- enrichment ------------------------------------------------------------------

def test_role_rank_blacklists_sales(campaign, defaults):
    assert role_rank("Sales Executive", campaign, defaults) == -1
    assert role_rank("Founder & CEO", campaign, defaults) > role_rank("Operations Manager", campaign, defaults)
    assert role_rank("Head of Ecommerce", campaign, defaults) > 0


def test_choose_contact_picks_decision_maker_with_personal_email(campaign, defaults):
    contact = choose_contact(_snapshot(), campaign, defaults)
    assert contact.is_decision_maker
    assert contact.name == "Ahmed Raza" and "CEO" in contact.role
    assert contact.email == "ahmed.raza@zarafabrics.pk"
    assert contact.profile_url == "https://www.linkedin.com/company/zara-fabrics"
    assert "Ahmed Raza" in contact.evidence


def test_choose_contact_falls_back_to_business_mailbox(campaign, defaults):
    snap = _snapshot()
    snap.pages.pop("about")
    contact = choose_contact(snap, campaign, defaults)
    assert not contact.is_decision_maker and contact.name is None
    assert contact.email == "info@zarafabrics.pk"


def test_signals_and_quality(defaults):
    snap = _snapshot()
    sig = detect_signals(snap, defaults)
    assert "hiring" in sig.buying and "ecommerce_active" in sig.buying
    assert "shopify" in sig.technologies
    q = assess_quality(snap)
    assert q.reachable and q.https and q.has_contact_page and q.has_about_page and q.has_public_email


# --- scoring ----------------------------------------------------------------------

def _company(**kw) -> DiscoveredCompany:
    base = dict(name="Zara Fabrics", website="https://www.zarafabrics.pk", city="Islamabad", country="Pakistan", source="osm")
    base.update(kw)
    return DiscoveredCompany(**base)


def _good_inputs(cls_type=CompanyType.BUYER, email_status=EmailStatus.MX_VALID) -> ScoreInputs:
    return ScoreInputs(
        company=_company(),
        classification=Classification(company_type=cls_type, confidence=0.9, buyer_hits=["retailer", "store"]),
        quality=CompanyQuality(reachable=True, https=True, has_contact_page=True, has_about_page=True, has_public_email=True, page_count=3),
        contact=Contact(name="Ahmed Raza", role="CEO", email="ahmed@zarafabrics.pk", email_status=email_status,
                        profile_url="https://linkedin.com/company/x", is_decision_maker=True),
        signals=Signals(buying={"hiring": ["careers"], "ecommerce_active": ["shop now"]}, technologies=["shopify"]),
    )


def test_strong_buyer_scores_high_and_is_outreach_ready(campaign):
    from gtm_engine.models import OnlinePresence
    inputs = _good_inputs()
    inputs.online_presence = OnlinePresence(
        google_review_count=150, google_rating=4.5,
        online_gap_score=18, pain_from_reviews=["stockouts"],
    )
    score = score_lead(inputs, campaign)
    assert score.total >= campaign.min_score and score.priority in (Priority.HIGH, Priority.QUALIFIED)
    assert score.total == score.review_band + score.rating_score + score.proximity_tier + score.online_gap + score.pain_evidence
    assert any("decision-maker" in r for r in score.reasons)
    assert is_outreach_ready(inputs.classification, score, inputs.contact, campaign)


def test_vendor_is_rejected_regardless_of_score(campaign):
    inputs = _good_inputs(cls_type=CompanyType.VENDOR)
    score = score_lead(inputs, campaign)
    assert score.priority == Priority.REJECT
    assert score.reasons[0].startswith("classified as VENDOR")
    assert not is_outreach_ready(inputs.classification, score, inputs.contact, campaign)


def test_unknown_never_exceeds_review(campaign):
    inputs = _good_inputs(cls_type=CompanyType.UNKNOWN)
    score = score_lead(inputs, campaign)
    assert score.priority in (Priority.REVIEW, Priority.REJECT)
    assert not is_outreach_ready(inputs.classification, score, inputs.contact, campaign)


def test_unverified_email_blocks_outreach(campaign):
    # Unverified email AND no phone -> unreachable.
    inputs = _good_inputs(email_status=EmailStatus.UNVERIFIED)
    score = score_lead(inputs, campaign)
    assert not is_outreach_ready(inputs.classification, score, inputs.contact, campaign)


def test_phone_only_buyer_is_outreach_ready(campaign):
    # Most SMBs publish a phone but no scrapeable email; a qualified buyer reachable by phone
    # (call / WhatsApp) must count as outreach-ready even without a usable email.
    from gtm_engine.models import OnlinePresence
    inputs = _good_inputs(email_status=EmailStatus.NONE)
    inputs.online_presence = OnlinePresence(google_review_count=150, google_rating=4.5,
                                            online_gap_score=18, pain_from_reviews=["stockouts"])
    inputs.contact.email = None
    inputs.contact.phone = "+92 300 1234567"
    score = score_lead(inputs, campaign)
    assert score.priority in (Priority.HIGH, Priority.QUALIFIED)
    assert is_outreach_ready(inputs.classification, score, inputs.contact, campaign)


def test_no_email_and_no_phone_is_not_outreach_ready(campaign):
    inputs = _good_inputs(email_status=EmailStatus.NONE)
    inputs.contact.email = None
    inputs.contact.phone = None
    score = score_lead(inputs, campaign)
    assert not is_outreach_ready(inputs.classification, score, inputs.contact, campaign)


def test_proximity_tier_scores(campaign):
    inputs = _good_inputs()
    score = score_lead(inputs, campaign)
    assert score.proximity_tier > 0
    assert any("Tier" in r for r in score.reasons)


def test_weights_rescale(campaign):
    campaign.weights.review_band = 20
    campaign.weights.pain_evidence = 30
    score = score_lead(_good_inputs(), campaign)
    assert score.review_band <= 20 and score.pain_evidence <= 30


def test_third_party_emails_are_not_the_company_email(campaign, defaults):
    from gtm_engine.enrichment.contacts import usable_emails
    emails = ["team@serpnames.com", "sharif.intl@gmail.com", "info@sharifinternational.net"]
    assert usable_emails(emails, "sharifinternational.net") == ["info@sharifinternational.net", "sharif.intl@gmail.com"]
    assert usable_emails(["team@serpnames.com"], "sharifinternational.net") == []
    snap = _snapshot()
    snap.pages.pop("about")
    contact = choose_contact(snap, campaign, defaults, company_domain="other.pk")
    assert contact.email is None and contact.email_status == EmailStatus.NONE


def test_freemail_only_from_visible_text_and_same_label_domain():
    from gtm_engine.enrichment.contacts import usable_emails
    # font-licence gmail hidden in HTML source is never used; parent-brand domain is
    assert usable_emails([], "ismbuilders.com", source_only=["impallari@gmail.com"]) == []
    assert usable_emails(["owner@gmail.com"], "ismbuilders.com") == ["owner@gmail.com"]
    assert usable_emails(["customercare.pk@bata.com"], "bata.com.pk") == ["customercare.pk@bata.com"]
    assert usable_emails([], "bata.com.pk", source_only=["x@bata.com"]) == ["x@bata.com"]


def test_wrong_website_is_held_for_review(campaign):
    from gtm_engine.enrichment.signals import assess_quality
    from gtm_engine.models import OnlinePresence
    snap = _snapshot()  # Zara Fabrics pages
    q = assess_quality(snap, "XS Mobile", "zarafabrics.pk")
    assert q.website_mismatch and any("belong" in n for n in q.notes)
    inputs = _good_inputs()
    inputs.quality = q
    inputs.online_presence = OnlinePresence(
        google_review_count=200, google_rating=4.5,
        online_gap_score=20, pain_from_reviews=["stockouts"],
    )
    score = score_lead(inputs, campaign)
    assert score.priority == Priority.REVIEW
    assert not is_outreach_ready(inputs.classification, score, inputs.contact, campaign)
    assert not assess_quality(snap, "Zara Fabrics", "zarafabrics.pk").website_mismatch


def test_overture_category_counts_as_campaign_match(campaign, defaults):
    campaign.overture_categories = ["clothing", "shoe_store"]
    bundle = TextBundle(name="Tailor and Cobbler", title="Tailor and Cobbler", description=None,
                        about_text="Handmade shoes", body_text="handmade shoes", category="overture=shoe_store")
    cls = BuyerClassifier(campaign, defaults).classify(bundle)
    assert cls.company_type == CompanyType.BUYER
    assert any("matches campaign target" in r for r in cls.reasons)
