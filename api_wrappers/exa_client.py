"""
Thin wrapper around Exa.ai search for gathering CMS-detection evidence
that site fingerprinting and Apollo data miss — job-board mentions,
vendor case studies, G2/Capterra reviews, etc.

Runs two complementary neural queries per firm and merges them:
  1. A CMS-named query  — biases toward pages that name a target platform
     ("proficiency with Filevine required").
  2. A job-postings query — finds the firm's *active* intake/case-manager/
     paralegal openings regardless of whether a CMS is named, which is the
     brief's single most reliable hiring signal and often the page where a
     CMS requirement shows up even when query (1) misses it.
Both stay neural/semantic so we keep credit usage low (no per-platform fan-out).
"""
import os

from . import cost_tracker


def _client():
    from exa_py import Exa
    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        raise RuntimeError("EXA_API_KEY environment variable is not set.")
    return Exa(api_key=api_key)


def _run_query(exa, query: str, num_results: int, max_characters: int = 1200) -> list:
    """Single Exa call → list of {title, url, text}. Caller handles failures."""
    result = exa.search(
        query,
        type="auto",
        num_results=num_results,
        contents={"text": {"max_characters": max_characters}},
    )
    out = []
    for r in result.results:
        out.append({
            "title": getattr(r, "title", "") or "",
            "url": getattr(r, "url", "") or "",
            "text": (getattr(r, "text", "") or "")[:max_characters],
        })
    return out


def _cms_query(firm_name: str, location_str: str) -> str:
    # Phrase as a job posting for a case-manager/paralegal role at this firm —
    # that's where CMS platform names actually appear verbatim in text. CMS names
    # are appended so the neural search biases toward pages that name a platform
    # rather than generic firm marketing pages.
    return (
        f'{firm_name} {location_str} case manager OR intake specialist OR paralegal '
        f'job posting "case management software" '
        f'(CloudLex OR Filevine OR Litify OR SmartAdvocate OR CASEpeer)'
    ).strip()


def _job_postings_query(firm_name: str, location_str: str) -> str:
    # Dedicated job-postings angle: find the firm's actively-advertised openings
    # for the intake/records/case-management roles called out in the brief, on the
    # job boards where they're posted. No CMS names here on purpose — this surfaces
    # the hiring signal even when no platform is mentioned, and frequently lands on
    # the exact job description where a CMS requirement is buried.
    return (
        f'{firm_name} {location_str} careers OR "now hiring" OR job opening '
        f'(case manager OR intake specialist OR legal intake OR paralegal OR '
        f'medical records OR case coordinator) '
        f'(Indeed OR LinkedIn OR ZipRecruiter OR Glassdoor)'
    ).strip()


def search_cms_evidence(firm_name: str, city: str = "", state: str = "", num_results: int = 4) -> list:
    """
    Runs two Exa searches per firm (CMS-named + job-postings) and merges the
    results, deduped by URL. `num_results` is the *total* evidence budget; it is
    split across the two queries so this stays roughly cost-neutral vs. the old
    single-query version.

    Returns a list of dicts: {title, url, text} truncated to keep token usage
    down in the downstream Claude classification step. Returns [] on any failure
    rather than raising, so one bad firm doesn't kill a 150-firm batch run.
    """
    location_str = f"{city} {state}".strip()
    # Split the budget across the two angles (CMS-named gets the rounding-up half).
    cms_n = max(1, (num_results + 1) // 2)
    jobs_n = max(1, num_results // 2)

    queries = [
        (_cms_query(firm_name, location_str), cms_n),
        (_job_postings_query(firm_name, location_str), jobs_n),
    ]

    merged, seen = [], set()
    for query, n in queries:
        try:
            exa = _client()
            results = _run_query(exa, query, n)
            cost_tracker.record("exa", "search", success=True, results_returned=len(results))
            for item in results:
                key = item["url"] or item["title"]
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                merged.append(item)
        except Exception as e:
            print(f"  [exa] search failed for {firm_name}: {e}")
            cost_tracker.record("exa", "search", success=False, error_message=str(e),
                                results_returned=0)
            continue
    return merged
