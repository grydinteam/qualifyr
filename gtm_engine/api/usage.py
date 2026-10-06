"""Daily per-user usage limits for metered APIs.

Default limits are conservative for free-tier sustainability.
Override per resource via GTM_DAILY_LIMIT_{RESOURCE} env vars.
"""

from __future__ import annotations

import os

from gtm_engine.storage.database import Database

DEFAULT_LIMITS: dict[str, int] = {
    "brave": 50,
    "groq": 200,
    "hunter": 10,
    "places": 20,
    # Campaign-run dispatches per day. Each run is also bounded by the per-run lead cap and the
    # one-run-at-a-time guard. On grydinteam (open-source) all runs share the repo's GitHub
    # Actions minutes, so this is tight by design.
    "runs": 3,
    # Per-user daily caps on the endpoints that each cost something on every call - an LLM parse,
    # an outbound SMTP connection, an outbound API probe - so none can be hammered unbounded.
    "nl": 20,
    "smtp_test": 30,
    "key_test": 30,
}


MAX_LIMITS: dict[str, int] = {k: v * 10 for k, v in DEFAULT_LIMITS.items()}


def _limit(resource: str, db: Database | None = None, user_id: str | None = None) -> int:
    if db and user_id:
        prefs = {r["pref_key"]: r["pref_value"] for r in db.get_preferences(user_id)}
        user_val = prefs.get(f"daily_limit_{resource}")
        if user_val and user_val.isdigit():
            cap = MAX_LIMITS.get(resource, 1000)
            return max(1, min(int(user_val), cap))
    env = os.environ.get(f"GTM_DAILY_LIMIT_{resource.upper()}")
    if env and env.isdigit():
        return int(env)
    return DEFAULT_LIMITS.get(resource, 100)


def check_usage(db: Database, user_id: str | None, resource: str) -> bool:
    """Check if the user is under the daily limit and increment. Returns True if allowed."""
    if not user_id:
        return True
    return db.check_and_increment_usage(user_id, resource, _limit(resource, db, user_id))


def get_all_usage(db: Database, user_id: str) -> dict[str, dict]:
    """Current usage for all resources, with limits."""
    rows = db.get_usage(user_id)
    usage_map = {r["resource"]: r["daily_count"] for r in rows}
    return {
        resource: {
            "count": usage_map.get(resource, 0),
            "limit": _limit(resource, db, user_id),
            "default_limit": DEFAULT_LIMITS.get(resource, 100),
            "max_limit": MAX_LIMITS.get(resource, 1000),
        }
        for resource in DEFAULT_LIMITS
    }
