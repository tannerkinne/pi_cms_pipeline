"""
Thin wrapper around the Apollo.io API endpoints this pipeline uses.

Auth: Apollo uses an `x-api-key` header (not Bearer), set via the
APOLLO_API_KEY environment variable.

Endpoints used (verified against docs.apollo.io, mid-2026):
  - POST /api/v1/mixed_companies/search   -> organization search (costs credits)
  - GET  /api/v1/organizations/{id}/job_postings -> job postings (costs credits)
  - POST /api/v1/mixed_people/api_search  -> people search (credit-free, but
        requires a "master" API key tier — gracefully degrades if 403)
  - GET  /v1/auth/supported_technologies_csv -> list of tracked tech UIDs

All functions return plain dicts/lists and raise no exceptions on expected
failure modes (auth tier issues, no results) — callers check for None/empty.
"""
import os
import time
import csv
import io
import requests

APOLLO_BASE = "https://api.apollo.io/api/v1"
APOLLO_AUTH_BASE = "https://api.apollo.io/v1"  # supported_technologies_csv lives under the older /v1 path


def _api_key() -> str:
    key = os.environ.get("APOLLO_API_KEY")
    if not key:
        raise RuntimeError("APOLLO_API_KEY environment variable is not set.")
    return key


def _headers() -> dict:
    return {
        "x-api-key": _api_key(),
        "Content-Type": "application/json",
        "accept": "application/json",
    }


def get_supported_technology_uids(timeout=15) -> set:
    """
    Download Apollo's list of tracked technology UIDs once per run.
    Used to check whether any of our target CMS platforms are even
    trackable via the `currently_using_any_of_technology_uids` filter.
    Returns an empty set on any failure (caller should treat tech-based
    detection as unavailable, not crash).
    """
    url = f"{APOLLO_AUTH_BASE}/auth/supported_technologies_csv"
    try:
        resp = requests.get(url, headers=_headers(), timeout=timeout)
        if resp.status_code != 200:
            return set()
        reader = csv.reader(io.StringIO(resp.text))
        uids = set()
        for row in reader:
            if row:
                uids.add(row[0].strip().lower())
        return uids
    except requests.RequestException:
        return set()


def search_organizations(keyword_tags, locations, employee_ranges, page=1, per_page=25, timeout=20) -> dict:
    """
    Organization Search (costs Apollo credits). Returns the raw JSON response,
    or {"organizations": [], "pagination": {}} on failure.
    """
    payload = {
        "q_organization_keyword_tags": keyword_tags,
        "organization_locations": locations,
        "organization_num_employees_ranges": employee_ranges,
        "page": page,
        "per_page": per_page,
    }
    url = f"{APOLLO_BASE}/mixed_companies/search"
    try:
        resp = requests.post(url, headers=_headers(), json=payload, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
        print(f"  [apollo] organization search failed: HTTP {resp.status_code} {resp.text[:200]}")
        return {"organizations": [], "pagination": {}}
    except requests.RequestException as e:
        print(f"  [apollo] organization search error: {e}")
        return {"organizations": [], "pagination": {}}


def get_job_postings(organization_id, per_page=20, timeout=20) -> list:
    """
    Organization Job Postings (costs Apollo credits). Returns a list of
    job posting dicts (fields vary; we defensively pull whatever's present),
    or [] on failure / no postings.
    """
    url = f"{APOLLO_BASE}/organizations/{organization_id}/job_postings"
    try:
        resp = requests.get(url, headers=_headers(), params={"per_page": per_page}, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            # Response key naming isn't fully documented publicly; check both.
            return data.get("job_postings") or data.get("organization_job_postings") or []
        return []
    except requests.RequestException:
        return []


def search_people_at_organization(organization_id, titles, per_page=5, timeout=20):
    """
    People API Search (credit-free, but requires a master API key).
    Returns None on 403 (master key required) so callers can distinguish that
    from a legitimate empty result set. Returns [] on any other failure or
    when the search succeeds but finds no matching people.
    """
    payload = {
        "organization_ids": [organization_id],
        "person_titles": titles,
        "per_page": per_page,
    }
    url = f"{APOLLO_BASE}/mixed_people/api_search"
    try:
        resp = requests.post(url, headers=_headers(), json=payload, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("people", [])
        if resp.status_code == 403:
            # Expected on non-master keys — caller checks for None to detect this.
            return None
        return []
    except requests.RequestException:
        return []


def polite_sleep(seconds=0.4):
    time.sleep(seconds)
