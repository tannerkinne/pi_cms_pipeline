"""
Thin wrapper around Exa.ai search for gathering CMS-detection evidence
that site fingerprinting and Apollo data miss — job-board mentions,
vendor case studies, G2/Capterra reviews, etc.

Deliberately uses ONE combined query per firm (rather than one query per
CMS platform) to keep credit usage down, since Exa is a neural/semantic
search and can match relevant pages without exact keyword repetition.
"""
import os

CMS_NAMES = ["CloudLex", "Filevine", "Litify", "SmartAdvocate", "CASEpeer"]


def _client():
    from exa_py import Exa
    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        raise RuntimeError("EXA_API_KEY environment variable is not set.")
    return Exa(api_key=api_key)


def search_cms_evidence(firm_name: str, city: str = "", state: str = "", num_results: int = 4) -> list:
    """
    Runs a single Exa search_and_contents call per firm looking for any
    mention of the target CMS platforms in connection with this firm
    (job postings, case studies, reviews, staff LinkedIn mentions, etc).

    Returns a list of dicts: {title, url, text} truncated to keep token
    usage down in the downstream Claude classification step. Returns []
    on any failure rather than raising, so one bad firm doesn't kill a
    150-firm batch run.
    """
    location_str = f"{city} {state}".strip()
    query = (
        f'"{firm_name}" {location_str} law firm case management software '
        f'CloudLex Filevine Litify SmartAdvocate CASEpeer client portal job posting'
    ).strip()
    try:
        exa = _client()
        result = exa.search(
            query,
            type="auto",
            num_results=num_results,
            contents={"text": {"max_characters": 1200}},
        )
        evidence = []
        for r in result.results:
            evidence.append({
                "title": getattr(r, "title", "") or "",
                "url": getattr(r, "url", "") or "",
                "text": (getattr(r, "text", "") or "")[:1200],
            })
        return evidence
    except Exception as e:
        print(f"  [exa] search failed for {firm_name}: {e}")
        return []
