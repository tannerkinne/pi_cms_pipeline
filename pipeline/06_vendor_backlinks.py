"""
Stage 6 — CMS vendor reverse-lookup ("back-tracking").

The other direction of detection. Instead of asking each firm's own site what
CMS it runs, this asks each CMS VENDOR's marketing site which firms it brags
about. Vendors (Filevine, CloudLex, Litify, SmartAdvocate, CASEpeer) publish
customer lists, testimonials and case studies; if one of our sourced firms turns
up there, that's strong third-party evidence the firm runs that CMS.

Pure scraping — no API credits. Two match types, strongest first:
  1. domain_match  — the firm's domain appears on a vendor page (as an outbound
     "visit website" link or as plain text). Unambiguous → High confidence.
  2. name_mention  — the firm's distinctive name core (surnames, stopwords
     stripped) appears in a vendor page's visible text → Medium confidence.

Outputs data/vendor_mentions.csv (every match, with the vendor page + snippet).
With --apply it ALSO back-tracks the matches into data/cms_results.csv: it fills
in firms currently classified 'Unknown' (and, with --include-names, applies
name_mention matches too), so a re-run of Stage 4 promotes them. cms_results.csv
is backed up to cms_results.bak.csv before any write (the upsert is not
crash-safe — see project memory).

Examples:
  python pipeline/06_vendor_backlinks.py                       # scrape + report only
  python pipeline/06_vendor_backlinks.py --apply               # + patch Unknown firms (domain matches)
  python pipeline/06_vendor_backlinks.py --apply --include-names  # also apply name matches
  python pipeline/06_vendor_backlinks.py --only Filevine,Litify   # just these vendors
"""
import argparse
import os
import re
import sys
import time
import warnings
from urllib.parse import urljoin, urlparse

warnings.filterwarnings("ignore", message=r".*OpenSSL.*", module="urllib3")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

import site_fingerprint
from config import (
    CMS_VENDOR_HOMEPAGES,
    CMS_VENDOR_SEED_PATHS,
    CMS_VENDOR_PAGE_HINTS,
    CMS_VENDOR_MAX_PAGES,
    FIRM_NAME_STOPWORDS,
    FIRM_NAME_GENERIC_TOKENS,
    SITE_FETCH_TIMEOUT_SECONDS,
    SITE_FETCH_DELAY_SECONDS,
    USER_AGENT,
)
from utils import DATA_DIR, read_csv_rows, write_csv_rows, CallCounter

FIRMS_PATH = os.path.join(DATA_DIR, "firms_raw.csv")
CMS_PATH = os.path.join(DATA_DIR, "cms_results.csv")
MENTIONS_PATH = os.path.join(DATA_DIR, "vendor_mentions.csv")

MENTIONS_FIELDS = [
    "domain", "firm_name", "cms", "match_type", "confidence",
    "vendor_page", "snippet",
]

# Keep cms_results.csv's exact schema when we patch it (Stage 2's FIELDNAMES).
CMS_FIELDS = [
    "domain", "firm_name", "cms_detected", "cms_confidence", "cms_evidence",
    "hiring_signal", "hiring_signal_evidence", "est_attorneys", "plaintiff_pi_focus",
    "plaintiff_pi_focus_note", "trial_focused", "site_pages_checked", "site_errors",
]


def _norm_domain(domain: str) -> str:
    """Bare registrable host, no scheme / www / path. Mirrors firms_raw domains."""
    d = (domain or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0]
    if d.startswith("www."):
        d = d[4:]
    return d


def _name_core(firm_name: str) -> str:
    """Distinctive core of a firm name: lowercase, punctuation→space, drop the
    boilerplate law-firm words. 'Kwartler Manus, LLC' -> 'kwartler manus'."""
    cleaned = re.sub(r"[^a-z0-9\s]", " ", (firm_name or "").lower())
    tokens = [t for t in cleaned.split() if t and t not in FIRM_NAME_STOPWORDS]
    return " ".join(tokens)


def _is_distinctive(core: str) -> bool:
    """Is a firm-name core specific enough to trust a bare text match on a vendor
    page? Requires at least one token that's a real proper-noun-ish surname —
    i.e. not a geographic / practice-area / marketing word — of length >= 4. This
    drops cores that reduce to generic phrases ('medical malpractice', 'new york',
    'win big') which would otherwise match boilerplate text on every vendor page."""
    distinctive = [t for t in core.split()
                   if t not in FIRM_NAME_GENERIC_TOKENS and len(t) >= 4]
    return bool(distinctive)


def _fetch(url: str, render: str = "fallback"):
    """Fetch a vendor page; return (html, final_url) or (None, None).

    Vendor marketing sites are JS-heavy, so by default we render with Playwright
    when the static fetch looks empty/low-signal ('fallback'), reusing the same
    headless path Stage 2/3 use. 'off' = static only, 'always' = always render.
    """
    html, final_url = None, url
    if render != "always":
        try:
            resp = requests.get(
                url, headers={"User-Agent": USER_AGENT},
                timeout=SITE_FETCH_TIMEOUT_SECONDS, allow_redirects=True,
            )
            final_url = resp.url
            if resp.status_code == 200 and resp.text:
                html = resp.text
        except requests.RequestException:
            html = None

    needs_render = render == "always" or (
        render == "fallback" and (not html or len(_visible(html)) < 200)
    )
    if needs_render:
        rendered = site_fingerprint._render_html(url)
        if rendered:
            html, final_url = rendered, url
    return html, final_url


def _visible(html: str) -> str:
    if not html:
        return ""
    return site_fingerprint._visible_text_full(html, drop_chrome=False)


def _discover_pages(cms: str, render: str, counter: CallCounter) -> list:
    """Return [(url, html, text, is_home)] for a vendor: homepage + seed paths +
    any same-domain links whose href/text hints at a customer/testimonial page.
    is_home flags the vendor homepage, which is excluded from name-matching (its
    generic UI text drives false positives) but still used for domain matches."""
    home = CMS_VENDOR_HOMEPAGES[cms].rstrip("/")
    host = urlparse(home).netloc.lower()

    candidates = [home + "/"]
    candidates += [home + p for p in CMS_VENDOR_SEED_PATHS.get(cms, [])]

    pages = []           # (url, html, text, is_home)
    seen = set()
    discovered_from_home = []

    def _take(url, is_home=False):
        u = url.split("#")[0].rstrip("/") or url
        if u in seen:
            return
        seen.add(u)
        if len(pages) >= CMS_VENDOR_MAX_PAGES:
            return
        html, final = _fetch(url, render=render)
        counter.tick("vendor_page_fetch (free)", 1)
        time.sleep(SITE_FETCH_DELAY_SECONDS)
        if not html:
            return
        text = _visible(html)
        pages.append((final, html, text, is_home))
        return html

    # Homepage first, then mine it for customer-story links.
    home_html = _take(candidates[0], is_home=True)
    if home_html:
        for href in _links(home_html, home):
            if urlparse(href).netloc.lower() != host:
                continue
            blob = href.lower()
            if any(h in blob for h in CMS_VENDOR_PAGE_HINTS):
                discovered_from_home.append(href)

    for url in candidates[1:] + discovered_from_home:
        if len(pages) >= CMS_VENDOR_MAX_PAGES:
            break
        _take(url)

    return pages


def _links(html: str, base: str) -> list:
    """Absolute hrefs of all <a> tags on a page."""
    out = []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            out.append(urljoin(base, a["href"]))
    except Exception:
        for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.I):
            out.append(urljoin(base, m.group(1)))
    return out


def _snippet(text: str, needle: str, width: int = 90) -> str:
    i = text.lower().find(needle.lower())
    if i < 0:
        return ""
    start = max(0, i - width)
    end = min(len(text), i + len(needle) + width)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def main():
    parser = argparse.ArgumentParser(description="Reverse-lookup firms from CMS vendor pages.")
    parser.add_argument("--only", default="",
                        help="Comma-separated vendor names to scrape (default: all in config).")
    parser.add_argument("--render", choices=["off", "fallback", "always"], default="fallback",
                        help="Headless render for JS-heavy vendor pages (default fallback).")
    parser.add_argument("--apply", action="store_true",
                        help="Back-track matches into cms_results.csv (fills Unknown firms). "
                             "Backs the file up to cms_results.bak.csv first.")
    parser.add_argument("--include-names", action="store_true",
                        help="With --apply, also apply name_mention matches (Medium confidence), "
                             "not just domain matches. Off by default to avoid false positives.")
    parser.add_argument("--overwrite-detected", action="store_true",
                        help="With --apply, also overwrite firms already classified with a "
                             "DIFFERENT CMS (default: only fill Unknown firms).")
    args = parser.parse_args()

    vendors = [v.strip() for v in args.only.split(",") if v.strip()] or list(CMS_VENDOR_HOMEPAGES)
    bad = [v for v in vendors if v not in CMS_VENDOR_HOMEPAGES]
    if bad:
        print(f"Unknown vendor(s): {', '.join(bad)}. Known: {', '.join(CMS_VENDOR_HOMEPAGES)}")
        return

    firms = read_csv_rows(FIRMS_PATH)
    if not firms:
        print(f"No firms in {FIRMS_PATH} — run Stage 1 first.")
        return

    # Index firms for matching.
    by_domain = {}
    name_index = []  # (core, firm)
    for f in firms:
        dom = _norm_domain(f.get("domain") or f.get("website", ""))
        if dom:
            by_domain[dom] = f
        core = _name_core(f.get("firm_name", ""))
        if core:
            name_index.append((core, f))

    counter = CallCounter()
    matches = {}  # (domain, cms) -> match row (best per firm+cms, domain beats name)

    def _record(firm, cms, match_type, confidence, page, snippet):
        dom = _norm_domain(firm.get("domain") or firm.get("website", ""))
        key = (dom, cms)
        prev = matches.get(key)
        # domain_match always wins over name_mention for the same firm+cms.
        if prev and prev["match_type"] == "domain_match" and match_type != "domain_match":
            return
        matches[key] = {
            "domain": dom, "firm_name": firm.get("firm_name", ""), "cms": cms,
            "match_type": match_type, "confidence": confidence,
            "vendor_page": page, "snippet": snippet,
        }

    for cms in vendors:
        print(f"\n=== {cms} — scraping vendor site ===")
        pages = _discover_pages(cms, args.render, counter)
        print(f"  fetched {len(pages)} page(s)")
        for url, html, text, is_home in pages:
            blob = (html + " " + text).lower()
            # 1) domain matches — firm domain present as link or plain text.
            for dom, firm in by_domain.items():
                if dom in blob:
                    _record(firm, cms, "domain_match", "High", url, _snippet(text, dom))
            # 2) name matches against visible text only (avoid HTML attr noise),
            #    and never on the homepage (its generic UI text is noisy).
            if is_home:
                continue
            tlow = text.lower()
            for core, firm in name_index:
                if len(core) < 6 or not _is_distinctive(core):
                    continue
                if core in tlow:
                    conf = "Medium" if len(core.split()) >= 2 else "Low"
                    _record(firm, cms, "name_mention", conf, url, _snippet(text, core))

    rows = sorted(
        matches.values(),
        key=lambda r: (r["match_type"] != "domain_match", r["cms"], r["firm_name"]),
    )
    write_csv_rows(MENTIONS_PATH, rows, MENTIONS_FIELDS, mode="w")

    dom_hits = [r for r in rows if r["match_type"] == "domain_match"]
    name_hits = [r for r in rows if r["match_type"] == "name_mention"]
    print(f"\nScraped {len(vendors)} vendor(s). Matches: "
          f"{len(dom_hits)} domain, {len(name_hits)} name -> {MENTIONS_PATH}")
    for r in rows:
        print(f"  [{r['match_type']:12} {r['confidence']:6}] {r['firm_name']} -> {r['cms']}  ({r['vendor_page']})")
    print(f"\nFetch summary: {counter.summary()}")

    if not args.apply:
        print("\n(dry run — pass --apply to back-track these into cms_results.csv)")
        return

    _apply(rows, args)


def _apply(rows, args):
    """Back-track matches into cms_results.csv. Fills Unknown firms by default;
    name matches require --include-names; a firm already on a different CMS is
    left alone unless --overwrite-detected."""
    existing = read_csv_rows(CMS_PATH)
    if not existing:
        print(f"\nNothing to patch — {CMS_PATH} is empty (run Stage 2 first).")
        return

    # Back up first — the upsert rewrite is not crash-safe (project memory).
    backup = os.path.join(DATA_DIR, "cms_results.bak.csv")
    write_csv_rows(backup, existing, CMS_FIELDS, mode="w")

    by_domain = {r["domain"]: r for r in existing}
    # domain matches always apply; name matches only with --include-names, and
    # never the Low-confidence ones (single common surnames — prone to wrong-firm
    # collisions, e.g. "Pollack Law" vs "Pollack, Pollack, Issac & DeCiccio").
    # Low name matches stay in vendor_mentions.csv for manual review.
    applicable = [r for r in rows if r["match_type"] == "domain_match"
                  or (args.include_names and r["match_type"] == "name_mention"
                      and r["confidence"] != "Low")]

    filled, conflicts, skipped, missing = 0, 0, 0, 0
    for m in applicable:
        cur = by_domain.get(m["domain"])
        if not cur:
            missing += 1
            continue
        detected = (cur.get("cms_detected") or "").strip()
        if detected == m["cms"]:
            skipped += 1
            continue
        if detected and detected != "Unknown":
            if not args.overwrite_detected:
                conflicts += 1
                print(f"  conflict (kept): {m['firm_name']} is '{detected}', vendor says '{m['cms']}'")
                continue
        note = (f"Listed as a {m['cms']} customer on vendor site "
                f"({m['match_type']}, {m['confidence']} conf): {m['vendor_page']}")
        prior = (cur.get("cms_evidence") or "").strip()
        cur["cms_detected"] = m["cms"]
        cur["cms_confidence"] = m["confidence"]
        cur["cms_evidence"] = f"{prior} | {note}" if prior and prior != "Unknown" else note
        filled += 1

    write_csv_rows(CMS_PATH, list(by_domain.values()), CMS_FIELDS, mode="w")
    print(f"\nApplied to {CMS_PATH} (backup: {backup}):")
    print(f"  filled/updated: {filled} | already correct: {skipped} | "
          f"conflicts kept: {conflicts} | not in cms_results: {missing}")
    print("  Re-run Stage 4 (pipeline/04_score_and_export.py) to refresh final_output.csv.")


if __name__ == "__main__":
    main()
