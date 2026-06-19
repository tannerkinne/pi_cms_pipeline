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
import json
import time
import requests
from typing import Optional

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


# Site-builder boilerplate that pollutes the visible text of some firm sites
# (notably Yext-built sites, which render every "Knowledge Tag" placeholder into
# the static HTML). Left in place it repeats many times and crowds out the real
# attorney content within the char cap, which is exactly why est_attorneys came
# back 0 for firms like jcinjurylaw.com. We strip it before counting/sending.
_BOILERPLATE_PATTERNS = [
    # Yext Knowledge Tags placeholder. The live HTML often misspells "Knowledge"
    # (e.g. "Knolwedge"), so match the placeholder loosely from "This is a
    # placeholder for the Yext" through "added to the website."
    re.compile(
        r"This is a placeholder for the Yext\b.*?added to the website\.",
        re.I | re.S,
    ),
]


def _strip_boilerplate(text: str) -> str:
    """Remove known site-builder placeholder noise from extracted visible text
    so it doesn't crowd out real content within the char cap."""
    for pat in _BOILERPLATE_PATTERNS:
        text = pat.sub(" ", text)
    return " ".join(text.split())


# Honorific/role markers that, when attached to a capitalized name, strongly
# indicate an actual attorney rather than incidental Title-Case page text.
# A name is 2-3 Title-case tokens (allowing a middle initial like "B."); kept
# tight on purpose so the regex doesn't swallow preceding heading words like
# "About Lawyers <Name>".
# A name token is either a single-letter initial ("B.", "J.") or a capitalized
# word with a lowercase tail ("Jacobs", "O'Brien", "Smith-Jones"). Crucially it
# does NOT allow an embedded period inside a word, so a capture can't run across
# a sentence boundary like "...Levin. Thank you" into a non-name word.
_NAME_TOKEN = r"(?:[A-Z]\.|[A-Z][a-z]+(?:['\-][A-Za-z]+)*)"
_NAME_CORE = _NAME_TOKEN + r"(?:\s+" + _NAME_TOKEN + r"){1,2}"
_ESQ_NAME_RE = re.compile(r"(" + _NAME_CORE + r"),?\s+Esq\b\.?")
_ATTORNEY_PREFIX_RE = re.compile(r"\b(?:Attorney|Atty\.?)\s+(" + _NAME_CORE + r")")
# Names rendered as a card title immediately followed by a profile CTA — the
# common pattern on JS-built team pages whose names lack an "Esq." suffix
# (e.g. "Gabriel Levin View Full Profile").
_PROFILE_CTA_RE = re.compile(
    r"(" + _NAME_CORE + r")\s+View\s+(?:Full\s+)?(?:Profile|Bio)", re.I
)

# Tokens that signal a capture grabbed page furniture, not a person.
_NAME_STOPWORDS = {
    "about", "lawyers", "attorneys", "attorney", "lawyer", "team", "our",
    "meet", "the", "esq", "law", "firm", "free", "consultation", "menu",
    "home", "contact", "practice", "areas", "results", "reviews", "blog",
    "injury", "personal", "accident", "view", "profile", "bio", "rest",
}


def _extract_jsonld_names(html: str) -> list:
    """Parse <script type="application/ld+json"> blocks and pull the `name` of
    any Person / Attorney / Lawyer entities. Many site-builder sites embed team
    data as JSON-LD even when the visible cards are rendered client-side, so this
    recovers names that aren't otherwise in the static visible text. Never raises."""
    names = []
    try:
        blocks = re.findall(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.I | re.S,
        )
    except Exception:
        return names

    def _walk(node):
        if isinstance(node, dict):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if any(str(x).lower() in ("person", "attorney", "lawyer") for x in types):
                nm = node.get("name")
                if isinstance(nm, str) and nm.strip():
                    names.append(" ".join(nm.split()))
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    for block in blocks:
        try:
            _walk(json.loads(block))
        except Exception:
            continue
    return names


def _clean_name(raw: str):
    """Normalize a captured name and reject non-name junk. Trims leading/trailing
    stopword tokens (e.g. 'About Lawyers Rand Spear' -> 'Rand Spear'); returns
    None if nothing name-like remains."""
    tokens = raw.split()
    while tokens and tokens[0].strip(".'-").lower() in _NAME_STOPWORDS:
        tokens.pop(0)
    while tokens and tokens[-1].strip(".'-").lower() in _NAME_STOPWORDS:
        tokens.pop()
    if not (2 <= len(tokens) <= 3):
        return None
    name = " ".join(tokens)
    if name.isupper():
        return None
    return name


def _extract_attorney_names(text: str) -> list:
    """Pull likely attorney names out of visible text via honorific/role markers
    ('Jane Q. Smith, Esq.', 'Attorney John Doe') and profile-card CTAs
    ('Gabriel Levin View Full Profile'). De-duped, order-preserving. This gives
    the classifier clean named-attorney content to count even when the real names
    are surrounded by navigation/marketing noise."""
    found = []
    for rx in (_ESQ_NAME_RE, _ATTORNEY_PREFIX_RE, _PROFILE_CTA_RE):
        for m in rx.finditer(text):
            found.append(" ".join(m.group(1).split()))
    seen, out = set(), []
    for raw in found:
        n = _clean_name(raw)
        if not n:
            continue
        key = n.lower()
        if key not in seen:
            seen.add(key)
            out.append(n)
    return out


def _visible_text_full(html: str, drop_chrome: bool = True) -> str:
    """Return the full (uncapped) de-boilerplated visible text of a page.

    drop_chrome: when True, strips nav/footer (good for homepage focus
    judgment). Attorney-listing pages set it False because some site builders
    render the team list inside nav/footer-like containers, and dropping them
    would discard the very names we're trying to count.
    """
    strip_tags = ["script", "style"]
    if drop_chrome:
        strip_tags += ["nav", "footer"]
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(strip_tags):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ").split())
        return _strip_boilerplate(text)
    except Exception:
        import re as _re
        text = _re.sub(r"<[^>]+>", " ", html)
        text = " ".join(text.split())
        return _strip_boilerplate(text)


def _extract_visible_text(html: str, max_chars: int = 900, drop_chrome: bool = True) -> str:
    """Capped visible-text extraction for feeding the classifier a snippet it can
    use to judge plaintiff-PI focus. Falls back to a crude tag strip if
    BeautifulSoup isn't available for any reason."""
    return _visible_text_full(html, drop_chrome=drop_chrome)[:max_chars]


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


def _scrape_attorney_pages(domain: str) -> dict:
    """Fetch attorney-listing pages and return both concatenated visible text and
    a de-duped list of attorney names, giving Claude clean named-attorney content
    to estimate firm headcount.

    Names are recovered two ways so JS-heavy site builders don't defeat us:
      1. JSON-LD Person/Attorney/Lawyer entities embedded in the static HTML
         (present even when the visible cards are rendered client-side).
      2. Honorific/role markers ("Jane Smith, Esq.", "Attorney John Doe") in the
         de-boilerplated visible text.
    Returns {"text": str, "names": [str]}. Never raises (caller's try/except aside,
    every page fetch is individually guarded)."""
    combined = []
    total = 0
    names = []
    seen_names = set()

    def _add_names(candidates):
        for n in candidates:
            key = n.lower()
            if key not in seen_names:
                seen_names.add(key)
                names.append(n)

    for path in ATTORNEY_LISTING_PATHS:
        if total >= 6000:
            break
        url = f"https://{domain}{path}"
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=SITE_FETCH_TIMEOUT_SECONDS,
            )
            if resp.status_code < 400:
                # Keep nav/footer here: some builders render the team grid inside
                # those containers, and the boilerplate strip + name extraction
                # handle the resulting noise.
                full_text = _visible_text_full(resp.text, drop_chrome=False)
                # Extract names from the FULL page text, not the truncated snippet
                # below — attorney cards often sit past the char cap (this is the
                # jcinjurylaw.com failure mode: real names buried after boilerplate).
                _add_names(_extract_jsonld_names(resp.text))
                _add_names(_extract_attorney_names(full_text))
                if full_text:
                    chunk = f"[{path}]: {full_text[:2500]}"
                    combined.append(chunk)
                    total += len(chunk)
                time.sleep(SITE_FETCH_DELAY_SECONDS)
        except requests.RequestException:
            continue

    return {"text": "\n".join(combined)[:6000], "names": names}


def _render_html(url: str, timeout_ms: int = 12000) -> Optional[str]:
    """Return fully-rendered HTML for `url` via Playwright/headless Chromium, or
    None on ANY failure (Playwright not installed, navigation timeout, crash...).

    Playwright is imported lazily so the dependency is only needed when the
    render path is actually enabled — the static pipeline keeps zero new required
    deps. This never raises: same contract as the rest of the module, so a render
    that fails simply contributes no extra signal and the caller falls back to the
    static result. The browser is always closed (try/finally) even on timeout so
    we don't leak Chromium processes across a batch."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=USER_AGENT)
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                return page.content()
            finally:
                browser.close()
    except Exception:
        return None  # never raise — same contract as the rest of the module


def fingerprint_site(domain: str, render: str = "off") -> dict:
    """
    render controls the optional headless-browser fallback (Playwright/Chromium):
      "off"      — static path only; byte-for-byte the original behavior. Default.
      "fallback" — after the static pass, render at most two pages (homepage +
                   first attorney path) ONLY when the static result was
                   low-signal (no portal hits AND < 2 attorney names found), then
                   re-run the existing parse helpers over the rendered HTML.
      "always"   — always render those two pages regardless of static signal
                   (manual debugging option).
    Rendering is purely additive: any portal/name signal it recovers is merged
    into the static result; if it fails or finds nothing the static result stands.

    Returns: {
        "hits": {cms_name: ["portal_link"|"primary"|"secondary", ...]},  # signal types fired
        "pages_checked": [urls actually fetched successfully],  # rendered pages tagged "rendered:<url>"
        "errors": [paths that failed],
        "homepage_text_snippet": str,  # for plaintiff-PI-focus judgment downstream
        "job_titles_found": [title strings found on /careers page],
        "attorney_page_text": str,  # concatenated attorney-listing text for headcount
        "attorney_names": [str],    # names parsed from JSON-LD + honorific markers
        "rendered": bool,           # whether a headless render actually happened
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
    attorney_data = _scrape_attorney_pages(domain)
    attorney_names = attorney_data["names"]

    # --- Optional headless-browser fallback (Playwright/Chromium) ---------------
    # Render at most TWO pages and only when it can add signal the static fetch
    # missed: JS-injected client-portal links and client-side attorney cards.
    rendered = False
    has_portal_hit = any("portal_link" in kinds for kinds in hits.values())
    low_signal = (not has_portal_hit) and len(attorney_names) < 2
    should_render = render == "always" or (render == "fallback" and low_signal)

    if should_render:
        # (1) Homepage — recover JS-injected portal links, then follow them one
        #     hop exactly like the static path does.
        home_html = _render_html(f"https://{domain}")
        if home_html is not None:
            rendered = True
            pages_checked.append(f"rendered:https://{domain}")
            _scan_for_cms(home_html, hits)
            rendered_links = _find_portal_links(home_html, domain)
            if rendered_links:
                _follow_portal_links(rendered_links, hits, pages_checked, errors)

        # (2) First reachable attorney-listing path — recover client-side cards.
        #     Reuse the existing JSON-LD + name extraction; merge de-duped,
        #     order-preserving into the names we already have.
        seen_names = {n.lower() for n in attorney_names}
        for path in ATTORNEY_LISTING_PATHS:
            att_url = f"https://{domain}{path}"
            att_html = _render_html(att_url)
            if att_html is None:
                continue
            rendered = True
            pages_checked.append(f"rendered:{att_url}")
            full_text = _visible_text_full(att_html, drop_chrome=False)
            for n in _extract_jsonld_names(att_html) + _extract_attorney_names(full_text):
                key = n.lower()
                if key not in seen_names:
                    seen_names.add(key)
                    attorney_names.append(n)
            # Only the first reachable attorney path, to bound render cost.
            break

    # Convert sets to sorted lists for clean JSON/CSV serialization downstream.
    hits = {cms: sorted(kinds) for cms, kinds in hits.items()}
    return {
        "hits": hits,
        "pages_checked": pages_checked,
        "errors": errors,
        "homepage_text_snippet": homepage_text_snippet,
        "job_titles_found": job_titles_found,
        "attorney_page_text": attorney_data["text"],
        "attorney_names": attorney_names,
        "rendered": rendered,
    }
