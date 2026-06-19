"""
Thin wrapper around the Apollo.io People Search API for decision-maker lookup.

Auth: requires APOLLO_API_KEY env var.
Endpoint: POST https://api.apollo.io/v1/mixed_people/search

Returns the best-matching decision-maker (Managing Partner, Owner, COO, etc.)
for a given firm domain. Returns None if no match or on any error.
"""
import os
from typing import Optional

import requests

APOLLO_PEOPLE_SEARCH_URL = "https://api.apollo.io/v1/mixed_people/search"

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
            json={
                "q_organization_domains": domain,
                "person_titles": DECISION_MAKER_TITLES,
                "per_page": 10,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"  [apollo] search failed for {domain}: HTTP {resp.status_code}")
            return None
        data = resp.json()
        people = data.get("people", [])
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
        return None
