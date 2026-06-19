"""
Free CMS detection via direct site fingerprinting. For each firm we:
  1. Fetch the homepage + common subpages and scan HTML/URLs for CMS signature
     strings and vendor portal domains (branding, Salesforce markers for Litify).
  2. Find the firm's "Client Login" / portal links and follow them one hop —
     the redirect target host (e.g. firmname.cloudlex.com) is the strongest
     web-detectable CMS signal there is.
  3. Scrape attorney-listing pages so the downstream classifier has named-
     attorney content to estimate firm headcount (est_attorneys).

This is the only fully free signal in the pipeline (no API credits),
so it always runs regardless of API budget and should be checked first.
"""
import re
import time
import requests

from config import (
    CMS_SITE_SIGNATURES,
    CMS_SECONDARY_SIGNATURES,
    CMS_PORTAL_DOMAINS,
    PORTAL_LINK_HINTS,
    SITE_PATHS_TO_CHECK,
    ATTORNEY_LISTING_PATHS,
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


def _extract_job_titles(html: str) -> list:
    """Scan careers page HTML for job title patterns from HIRING_SIGNAL_TITLES."""
    from config import HIRING_SIGNAL_TITLES
    text_lower = html.lower()
    return [title for title in HIRING_SIGNAL_TITLES if title in text_lower]


def _scan_for_cms(text_or_url: str, hits: dict):
    """Scan a chunk of lowercased text (HTML body or a URL) for CMS signatures
    and portal domains, recording the strongest signal type per CMS into `hits`.
    Signal precedence (strongest first): portal_link > primary > secondary."""
    blob = text_or_url.lower()
    for cms, domains in CMS_PORTAL_DOMAINS.items():
        if any(d in blob for d in domains):
            hits.setdefault(cms, set()).add("portal_link")
    for cms, signatures in CMS_SITE_SIGNATURES.items():
        if any(sig in blob for sig in signatures):
            hits.setdefault(cms, set()).add("primary")
    for cms, signatures in CMS_SECONDARY_SIGNATURES.items():
        if any(sig in blob for sig in signatures):
            hits.setdefault(cms, set()).add("secondary")


def _find_portal_links(html: str, base_domain: str) -> list:
    """Return absolute URLs of anchors whose visible text looks like a client
    portal / login link (per PORTAL_LINK_HINTS). These get followed one hop so
    we can see what CMS host they land on."""
    found = []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        anchors = [(" ".join(a.get_text().split()).lower(), a.get("href", "")) for a in soup.find_all("a", href=True)]
    except Exception:
        anchors = []
        for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
            href = m.group(1)
            label = " ".join(re.sub(r"<[^>]+>", " ", m.group(2)).split()).lower()
            anchors.append((label, href))

    for label, href in anchors:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        if any(hint in label for hint in PORTAL_LINK_HINTS):
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = f"https://{base_domain}{href}"
            elif not href.startswith("http"):
                href = f"https://{base_domain}/{href.lstrip('/')}"
            found.append(href)
    # De-dupe, keep order, cap to avoid runaway follow chains.
    seen, out = set(), []
    for u in found:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:3]


def _follow_portal_links(links: list, hits: dict, pages_checked: list, errors: list):
    """Fetch each candidate portal link one hop and scan its final (post-redirect)
    URL + body for CMS portal-domain signals. The redirect target host is the
    strongest possible CMS signal — e.g. a 'Client Login' that lands on
    firmname.cloudlex.com."""
    for url in links:
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=SITE_FETCH_TIMEOUT_SECONDS,
                allow_redirects=True,
            )
            time.sleep(SITE_FETCH_DELAY_SECONDS)
            # The resolved URL after redirects is the high-value signal.
            _scan_for_cms(str(resp.url), hits)
            if resp.status_code < 400:
                _scan_for_cms(resp.text, hits)
                pages_checked.append(f"portal:{resp.url}")
        except requests.RequestException as e:
            errors.append(f"portal-follow {url} -> {type(e).__name__}")
            continue


def _scrape_attorney_pages(domain: str) -> str:
    """Fetch attorney-listing pages and return concatenated visible text (capped),
    giving Claude enough named-attorney content to estimate firm headcount."""
    combined = []
    total = 0
    for path in ATTORNEY_LISTING_PATHS:
        if total >= 4000:
            break
        url = f"https://{domain}{path}"
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=SITE_FETCH_TIMEOUT_SECONDS,
            )
            if resp.status_code < 400:
                text = _extract_visible_text(resp.text, max_chars=1500)
                if text:
                    chunk = f"[{path}]: {text}"
                    combined.append(chunk)
                    total += len(chunk)
                time.sleep(SITE_FETCH_DELAY_SECONDS)
        except requests.RequestException:
            continue
    return "\n".join(combined)[:4000]


def fingerprint_site(domain: str) -> dict:
    """
    Returns: {
        "hits": {cms_name: ["portal_link"|"primary"|"secondary", ...]},  # signal types fired
        "pages_checked": [urls actually fetched successfully],
        "errors": [paths that failed],
        "homepage_text_snippet": str,  # for plaintiff-PI-focus judgment downstream
        "job_titles_found": [title strings found on /careers page],
        "attorney_page_text": str,  # concatenated attorney-listing text for headcount
    }
    Never raises — a firm with an unreachable site just gets empty hits.
    """
    domain = _normalize_domain(domain)
    hits = {}
    pages_checked = []
    errors = []
    homepage_text_snippet = ""
    job_titles_found = []
    portal_links = []

    for path in SITE_PATHS_TO_CHECK:
        url = f"https://{domain}{path}"
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=SITE_FETCH_TIMEOUT_SECONDS,
            )
            if resp.status_code >= 400:
                errors.append(f"{path} -> HTTP {resp.status_code}")
                continue
            time.sleep(SITE_FETCH_DELAY_SECONDS)
            pages_checked.append(url)

            if path == "" and not homepage_text_snippet:
                homepage_text_snippet = _extract_visible_text(resp.text)

            if path == "/careers" and not job_titles_found:
                job_titles_found = _extract_job_titles(resp.text)

            # Collect candidate client-portal links from the homepage to follow later.
            if path == "":
                portal_links = _find_portal_links(resp.text, domain)

            # Scan body text + the resolved URL for CMS signatures / portal domains.
            _scan_for_cms(resp.text, hits)
            _scan_for_cms(str(resp.url), hits)

        except requests.RequestException as e:
            errors.append(f"{path} -> {type(e).__name__}")
            continue

    # Follow client-login links one hop — the redirect target host is the
    # strongest CMS signal (e.g. a login that lands on firm.cloudlex.com).
    if portal_links:
        _follow_portal_links(portal_links, hits, pages_checked, errors)

    # Scrape attorney-listing pages so Claude can estimate headcount downstream.
    attorney_page_text = _scrape_attorney_pages(domain)

    # Convert sets to sorted lists for clean JSON/CSV serialization downstream.
    hits = {cms: sorted(kinds) for cms, kinds in hits.items()}
    return {
        "hits": hits,
        "pages_checked": pages_checked,
        "errors": errors,
        "homepage_text_snippet": homepage_text_snippet,
        "job_titles_found": job_titles_found,
        "attorney_page_text": attorney_page_text,
    }
