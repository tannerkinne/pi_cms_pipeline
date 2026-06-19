"""
Shared configuration: CMS detection signatures, fit-score rules, target states.
Edit this file to tune scope without touching pipeline logic.
"""

# --- Geography & sourcing scope ---
TARGET_STATES = ["new jersey", "new york", "pennsylvania", "connecticut"]

# --- Google Places API — firm sourcing configuration ---
# Bounding boxes (SW lat/lng → NE lat/lng) for each target state.
GOOGLE_PLACES_STATE_BBOXES = {
    "new jersey":   {"sw": (38.9, -75.6), "ne": (41.4, -73.9)},
    "new york":     {"sw": (40.5, -79.8), "ne": (45.1, -71.9)},
    "pennsylvania": {"sw": (39.7, -80.5), "ne": (42.3, -74.7)},
    "connecticut":  {"sw": (40.9, -73.7), "ne": (42.1, -71.8)},
}

# Search queries run per state. Multiple queries improve recall across the
# different ways people describe PI firms and the cases they handle.
GOOGLE_PLACES_QUERIES = [
    "personal injury law firm",
    "plaintiff personal injury attorney",
    "car accident attorney",
    "car crash lawyer",
    "auto accident law firm",
    "slip and fall attorney",
    "injury lawyer",
]

# Max Places API pages to fetch per (state, query) combination.
# Each page = 1 API call ($17/1000). 4 states × 7 queries × 5 pages = 140 calls max.
# Cross-query duplicates are deduped by domain before writing to CSV.
GOOGLE_PLACES_MAX_PAGES = 5

# --- CMS systems we care about, ranked by value to us ---
# "Hot" = systems we already have integrations for.
# "Warm" = systems worth pursuing but no integration yet.
CMS_TIERS = {
    "CloudLex": "Hot",
    "Filevine": "Hot",
    "Litify": "Warm",
    "SmartAdvocate": "Warm",
    "CASEpeer": "Warm",
}

# Substrings to look for when scanning raw HTML / page source for each CMS.
# Lowercase, matched against lowercased page text and href attributes.
# Litify is built on Salesforce, so force.com / lightning.force.com is a
# secondary (lower-confidence) signal that should be combined with the
# "litify" string itself before trusting it.
CMS_SITE_SIGNATURES = {
    "CloudLex": ["cloudlex"],
    "Filevine": ["filevine"],
    "Litify": ["litify"],
    "SmartAdvocate": ["smartadvocate", "smart advocate"],
    "CASEpeer": ["casepeer", "case peer"],
}

# Secondary/weaker signals — if these fire ALONE (without the primary string
# above), treat as Low confidence only, never High.
CMS_SECONDARY_SIGNATURES = {
    "Litify": ["force.com", "lightning.force.com", "salesforce"],
}

# Domains that CMS vendors host client portals / client logins on. Finding any
# of these in a link href — or, better, as the redirect target of a firm's
# "Client Login" link — is the single strongest web-detectable CMS signal,
# because it means the firm is actively running that CMS's client portal.
# Treated as a "portal_link" hit (higher confidence than a body-text mention).
CMS_PORTAL_DOMAINS = {
    "CloudLex": ["cloudlex.com"],
    "Filevine": ["filevine.com", "filevineapp.com"],
    "Litify": ["litify.com", "litify.io", "litify.force.com"],
    "SmartAdvocate": ["smartadvocate.com", "smartadvocate.net"],
    "CASEpeer": ["casepeer.com", "casepeer.io"],
}

# Anchor text that signals a client-portal / case-status login link. The
# fingerprinter finds these links on the homepage and follows them one hop;
# the redirect destination's host is checked against CMS_PORTAL_DOMAINS.
PORTAL_LINK_HINTS = [
    "client login", "client portal", "client access", "case status",
    "secure login", "portal login", "make a payment", "pay online",
    "login", "portal",
]

# Common subpages worth checking in addition to the homepage. Many firms
# expose client-portal links from contact/intake pages, not the homepage.
SITE_PATHS_TO_CHECK = ["", "/contact", "/contact-us", "/careers", "/intake", "/client-portal", "/login", "/client-login"]

# Pages most likely to list individual attorneys by name. Scraped during
# Stage 2 specifically so Claude has content to estimate attorney headcount.
ATTORNEY_LISTING_PATHS = ["/attorneys", "/our-team", "/team", "/our-attorneys", "/about-us", "/about", "/lawyers", "/people"]

# Pages to check when scraping for decision-maker contacts.
CONTACT_PATHS_TO_CHECK = ["/attorneys", "/our-team", "/team", "/our-attorneys", "/about-us", "/about"]

# Job titles that signal the intake/records/case-manager hiring pattern
# called out in the brief as the most reliable CMS-detection signal.
HIRING_SIGNAL_TITLES = [
    "case manager",
    "intake specialist",
    "litigation paralegal",
    "paralegal",
    "medical records",
    "case coordinator",
]


def fit_score(cms_detected: str) -> str:
    """Apply the brief's fit-score rule."""
    if cms_detected in ("CloudLex", "Filevine"):
        return "Hot"
    if cms_detected in ("Litify", "SmartAdvocate", "CASEpeer"):
        return "Warm"
    return "Cold/Unknown"


# --- Claude classification model ---
# Cheapest current model — good enough for the structured classification task.
# Bump to claude-sonnet-4-6 for a small batch if Haiku's calls look unreliable.
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# --- Politeness / rate limiting for direct site fetches ---
SITE_FETCH_TIMEOUT_SECONDS = 8
SITE_FETCH_DELAY_SECONDS = 1.0
USER_AGENT = "Mozilla/5.0 (compatible; ResearchBot/1.0; +internal lead research)"
