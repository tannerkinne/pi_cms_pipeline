"""
Free CMS detection via direct site fingerprinting: fetch the firm's
homepage and a few common subpages, scan the raw HTML for known CMS
signature strings (portal links, branding, Salesforce markers for Litify).

This is the only fully free signal in the pipeline (no API credits),
so it always runs regardless of API budget and should be checked first.
"""
import time
import requests

from config import (
    CMS_SITE_SIGNATURES,
    CMS_SECONDARY_SIGNATURES,
    SITE_PATHS_TO_CHECK,
    SITE_FETCH_TIMEOUT_SECONDS,
    SITE_FETCH_DELAY_SECONDS,
    USER_AGENT,
)


def _normalize_domain(domain: str) -> str:
    domain = domain.strip().lower()
    domain = domain.replace("http://", "").replace("https://", "")
    domain = domain.split("/")[0]
    return domain


def _extract_visible_text(html: str, max_chars: int = 900) -> str:
    """Best-effort visible-text extraction for feeding the classifier a
    snippet it can use to judge plaintiff-PI focus. Falls back to a crude
    tag strip if BeautifulSoup isn't available for any reason."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ").split())
        return text[:max_chars]
    except Exception:
        import re as _re
        text = _re.sub(r"<[^>]+>", " ", html)
        text = " ".join(text.split())
        return text[:max_chars]


def fingerprint_site(domain: str) -> dict:
    """
    Returns: {
        "hits": {cms_name: ["primary"|"secondary", ...]},  # which signal types fired
        "pages_checked": [urls actually fetched successfully],
        "errors": [paths that failed],
        "homepage_text_snippet": str,  # for plaintiff-PI-focus judgment downstream
    }
    Never raises — a firm with an unreachable site just gets empty hits.
    """
    domain = _normalize_domain(domain)
    hits = {}
    pages_checked = []
    errors = []
    homepage_text_snippet = ""

    for path in SITE_PATHS_TO_CHECK:
        url = f"https://{domain}{path}"
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=SITE_FETCH_TIMEOUT_SECONDS,
            )
            time.sleep(SITE_FETCH_DELAY_SECONDS)
            if resp.status_code >= 400:
                errors.append(f"{path} -> HTTP {resp.status_code}")
                continue
            pages_checked.append(url)
            text_lower = resp.text.lower()

            if path == "" and not homepage_text_snippet:
                homepage_text_snippet = _extract_visible_text(resp.text)

            for cms, signatures in CMS_SITE_SIGNATURES.items():
                if any(sig in text_lower for sig in signatures):
                    hits.setdefault(cms, set()).add("primary")

            for cms, signatures in CMS_SECONDARY_SIGNATURES.items():
                if any(sig in text_lower for sig in signatures):
                    hits.setdefault(cms, set()).add("secondary")

        except requests.RequestException as e:
            errors.append(f"{path} -> {type(e).__name__}")
            continue

    # Convert sets to sorted lists for clean JSON/CSV serialization downstream.
    hits = {cms: sorted(kinds) for cms, kinds in hits.items()}
    return {
        "hits": hits,
        "pages_checked": pages_checked,
        "errors": errors,
        "homepage_text_snippet": homepage_text_snippet,
    }
