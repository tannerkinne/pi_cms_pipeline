"""
Thin wrapper around the Apollo.io People Search API for decision-maker lookup.

Auth: requires APOLLO_API_KEY env var.
Endpoint: POST https://api.apollo.io/api/v1/mixed_people/api_search

Notes on the endpoint (these were the source of earlier HTTP 422/403 errors):
  - The path is /api/v1/mixed_people/api_search — NOT /v1/mixed_people/search.
    The plain /search variant 403s on Basic plans; api_search is the
    net-new-prospecting endpoint available to API plans.
  - Filters (person_titles[], q_organization_domains_list[]) are passed as
    QUERY-STRING params with bracket array notation, not in the JSON body.
  - This endpoint returns names + titles (what we need) but not emails/phones.

Returns the best-matching decision-maker (Managing Partner, Owner, COO, etc.)
for a given firm domain. Returns None if no match or on any error.
"""
import os
from typing import Optional

import requests

from . import cost_tracker

APOLLO_PEOPLE_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
# People Match — used for email enrichment from a known name + firm domain.
# Apollo's real path is /api/v1/people/match (the documented /v1/people/match in
# the brief omits the /api segment; the /api/v1 form is what actually resolves,
# consistent with the search endpoint above).
APOLLO_PEOPLE_MATCH_URL = "https://api.apollo.io/api/v1/people/match"

DECISION_MAKER_TITLES = [
    "managing partner",
    "owner",
    "founding partner",
    "director of operations",
    "chief operating officer",
    "coo",
    "partner",
    "principal",
]

TITLE_PRIORITY = {t: i for i, t in enumerate(DECISION_MAKER_TITLES)}


def _api_key() -> str:
    key = os.environ.get("APOLLO_API_KEY")
    if not key:
        raise RuntimeError("APOLLO_API_KEY environment variable is not set.")
    return key


def find_decision_maker(domain: str) -> Optional[dict]:
    """
    Search Apollo for decision-maker contacts at the given firm domain.

    Returns a dict with keys: decision_maker_name, decision_maker_title, decision_maker_note
    Returns None if no results or on any error.
    """
    try:
        resp = requests.post(
            APOLLO_PEOPLE_SEARCH_URL,
            headers={
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
                "X-Api-Key": _api_key(),
            },
            # Apollo expects these as query-string params with bracket array
            # notation, NOT in the JSON body. requests encodes list values as
            # repeated keys: person_titles[]=managing+partner&person_titles[]=owner...
            params={
                "q_organization_domains_list[]": [domain],
                "person_titles[]": DECISION_MAKER_TITLES,
                "per_page": 10,
                "page": 1,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"  [apollo] search failed for {domain}: HTTP {resp.status_code} {resp.text[:160]}")
            cost_tracker.record("apollo", "mixed_people/api_search", success=False,
                                domain=domain, error_message=f"HTTP {resp.status_code}",
                                credits_consumed=0)
            return None
        data = resp.json()
        people = data.get("people", [])
        # A returned search page consumes credits; an empty result still billed
        # the query but yielded nothing usable.
        cost_tracker.record("apollo", "mixed_people/api_search", success=True,
                            domain=domain, credits_consumed=1,
                            results_returned=len(people))
        if not people:
            return None

        # Pick the person whose title best matches our priority list.
        def _rank(person):
            title = (person.get("title") or "").lower()
            for priority_title, rank in TITLE_PRIORITY.items():
                if priority_title in title:
                    return rank
            return len(DECISION_MAKER_TITLES)

        best = min(people, key=_rank)
        name = f"{best.get('first_name', '')} {best.get('last_name', '')}".strip()
        title = best.get("title", "")
        if not name:
            return None
        return {
            "decision_maker_name": name,
            "decision_maker_title": title,
            "decision_maker_note": "apollo_people_search",
        }
    except Exception as e:
        print(f"  [apollo] error for {domain}: {e}")
        cost_tracker.record("apollo", "mixed_people/api_search", success=False,
                            domain=domain, error_message=str(e), credits_consumed=0)
        return None


def enrich_email(first_name: str, last_name: str, domain: str) -> Optional[dict]:
    """
    Look up a verified email for a known person at a firm via Apollo People Match.

    POST https://api.apollo.io/api/v1/people/match

    All inputs — the person identity (first_name/last_name/domain) AND the
    reveal flag — are passed as QUERY-STRING params, not the JSON body. Apollo
    silently ignores body params here: sending them in the body returns a person
    match with email=null, while the same call as query params returns the
    verified email (and the key/domain must be `domain`, not `organization_domain`).
    reveal_personal_emails=true is required to unlock the email and consumes an
    Apollo credit.

    Returns {"email": <str>, "email_status": <str>} ONLY when Apollo returns a
    real, VERIFIED email. Returns None on no match, an unverified/locked email,
    or any error — callers must never guess or infer an address from a None.
    """
    if not (first_name or last_name) or not domain:
        return None
    try:
        resp = requests.post(
            APOLLO_PEOPLE_MATCH_URL,
            headers={
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
                "accept": "application/json",
                "X-Api-Key": _api_key(),
            },
            params={
                "first_name": first_name,
                "last_name": last_name,
                "domain": domain,
                "reveal_personal_emails": "true",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"  [apollo] match failed for {first_name} {last_name} @ {domain}: "
                  f"HTTP {resp.status_code} {resp.text[:160]}")
            cost_tracker.record("apollo", "people/match", success=False, domain=domain,
                                error_message=f"HTTP {resp.status_code}", credits_consumed=0)
            return None

        person = resp.json().get("person") or {}
        email = (person.get("email") or "").strip()
        email_status = (person.get("email_status") or "").strip().lower()

        # A returned person record (with a revealed email) consumes a credit.
        revealed = bool(email) and "email_not_unlocked" not in email
        cost_tracker.record("apollo", "people/match", success=True, domain=domain,
                            credits_consumed=1 if revealed else 0,
                            results_returned=1 if person else 0)

        # Only trust a genuinely verified address. Apollo marks unrevealed
        # addresses with a placeholder like "email_not_unlocked@domain.com".
        if revealed and email_status == "verified":
            return {"email": email, "email_status": email_status}
        return None
    except Exception as e:
        print(f"  [apollo] match error for {first_name} {last_name} @ {domain}: {e}")
        cost_tracker.record("apollo", "people/match", success=False, domain=domain,
                            error_message=str(e), credits_consumed=0)
        return None
