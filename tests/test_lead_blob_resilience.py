"""M7: a stored lead blob that can no longer be deserialized (schema drift, corruption) must not
take down the whole leads list/export. The LIST read paths skip-and-log such a row; a single-row
get still surfaces the error to its caller."""

from gtm_engine.models import Lead, CompanyType, Priority, SequenceStatus
from gtm_engine.storage.database import Database


def _lead(name: str, score: int = 80) -> Lead:
    return Lead(
        lead_id=f"lead_{name.lower()}",
        campaign_id="blob-test",
        company_name=name,
        company_type=CompanyType.BUYER,
        total_score=score,
        priority=Priority.QUALIFIED,
        sequence_status=SequenceStatus.NOT_QUEUED,
    )


def test_corrupt_blob_is_skipped_not_fatal(settings):
    db = Database(settings.database_url)
    db.upsert_campaign("blob-test", "Blob test", {"campaign_id": "blob-test", "name": "Blob test", "offer": "x"})
    db.save_lead(_lead("GoodCo", 90), "run1", "goodco")

    # Inject a row whose data_json can never deserialize into a Lead (as if written by a future
    # schema and read back by older code, or simply corrupted at rest).
    db._execute(
        "INSERT INTO leads (lead_id, campaign_id, run_id, company_key, company_type, total_score, "
        "priority, outreach_ready, sequence_status, contact_email, data_json, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        ("lead_corrupt", "blob-test", "run1", "badco", "BUYER", 85, "qualified", 0,
         "not_queued", None, '{"not":"a valid lead"}', "2026-01-01T00:00:00+00:00"),
    )
    db._commit()

    # The list path returns the good lead and silently drops the unreadable one.
    leads = db.list_leads("blob-test")
    names = [l.company_name for l in leads]
    assert "GoodCo" in names
    assert len(leads) == 1  # corrupt row skipped, not raised

    # count_leads counts rows in SQL (it never deserializes), so it still sees both.
    assert db.count_leads("blob-test") == 2
    db.close()
