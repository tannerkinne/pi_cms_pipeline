"""
Shared configuration: CMS detection signatures, fit-score rules, target states.
Edit this file to tune scope without touching pipeline logic.
"""

# --- Geography & sourcing scope ---
TARGET_STATES = ["new jersey", "new york", "pennsylvania", "connecticut"]

# --- Google Places API — firm sourcing configuration ---
# Bounding boxes (SW lat/lng → NE lat/lng) for each target state. Each state maps
# to a LIST of sub-region boxes rather than one big box: Places (New) returns at
# most ~60 results (3 pages) per query+box, so one box per state caps recall hard.
# Splitting each state into metro/region boxes lets the same query surface fresh
# firms in each area. Boxes may overlap — duplicates are deduped by domain.
GOOGLE_PLACES_STATE_BBOXES = {
    "new jersey": [
        {"sw": (40.5, -74.7), "ne": (41.4, -73.9)},  # North NJ (Newark/Jersey City/Paterson)
        {"sw": (40.0, -75.2), "ne": (40.6, -74.0)},  # Central NJ (Trenton/New Brunswick)
        {"sw": (38.9, -75.6), "ne": (40.1, -74.0)},  # South NJ (Camden/Atlantic City)
    ],
    "new york": [
        {"sw": (40.5, -74.3), "ne": (41.1, -71.9)},  # NYC metro + Long Island
        {"sw": (41.0, -74.3), "ne": (42.5, -73.3)},  # Hudson Valley
        {"sw": (42.0, -76.5), "ne": (43.6, -73.3)},  # Capital / Central (Albany/Syracuse)
        {"sw": (42.0, -79.8), "ne": (43.6, -76.3)},  # Western NY (Buffalo/Rochester)
    ],
    "pennsylvania": [
        {"sw": (39.7, -76.0), "ne": (40.6, -74.7)},  # Southeast / Philadelphia
        {"sw": (40.5, -76.6), "ne": (41.5, -74.7)},  # Northeast (Scranton/Allentown)
        {"sw": (39.7, -78.6), "ne": (41.5, -76.0)},  # Central (Harrisburg)
        {"sw": (39.7, -80.5), "ne": (42.3, -78.0)},  # Western (Pittsburgh/Erie)
    ],
    "connecticut": [
        {"sw": (40.9, -73.7), "ne": (41.5, -72.7)},  # Southwest (Stamford/Bridgeport)
        {"sw": (41.0, -73.0), "ne": (42.1, -72.3)},  # Central / South (New Haven/Hartford)
        {"sw": (41.3, -72.5), "ne": (42.1, -71.8)},  # East (New London)
    ],
}

# Search queries run per (state, sub-region box). More queries improve recall
# across the different ways people describe PI firms and the practice areas they
# handle — each term surfaces firms the others miss. Cross-query/box duplicates
# are deduped by domain.
GOOGLE_PLACES_QUERIES = [
    "personal injury law firm",
    "plaintiff personal injury attorney",
    "car accident attorney",
    "car crash lawyer",
    "auto accident law firm",
    "truck accident lawyer",
    "motorcycle accident lawyer",
    "pedestrian accident attorney",
    "slip and fall attorney",
    "premises liability lawyer",
    "construction accident lawyer",
    "medical malpractice attorney",
    "wrongful death attorney",
    "nursing home abuse lawyer",
    "dog bite attorney",
    "brain injury attorney",
    "injury lawyer",
]

# Max Places API pages to fetch per (sub-region box, query) combination.
# Each page = 1 API call ($17/1000). Places (New) usually returns ≤3 pages/query,
# so the practical ceiling is ~(#boxes × #queries × 3) calls — e.g. 14 boxes ×
# 17 queries × 3 ≈ 714 calls (~$12) for a full sweep. Stage 1 stops early once
# --limit unique firms are collected, so the real cost is bounded by --limit.
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
# above), treat as Low confidence only, never High. Litify is Salesforce-native,
# so a Salesforce *app* host can hint at it — but the bare strings we used before
# were dangerously noisy and produced confirmed false positives:
#   - "salesforce" also matches Font Awesome's ".fa-salesforce" icon class
#     (mydelawarelawyer.com) and Web-to-Lead marketing-form JS (ethenostrofflaw.com).
#   - "force.com" is a SUBSTRING of "salesforce.com", so it fired on every
#     *.salesforce.com URL (e.g. webto.salesforce.com Web-to-Lead endpoints),
#     none of which imply the firm runs Litify.
# Keep only "lightning.force.com" — the actual Salesforce Lightning app domain,
# which at least indicates a real Salesforce org rather than a marketing embed.
# Even this stays a Low-confidence SECONDARY signal, and a secondary-ONLY hit is
# suppressed to Unknown downstream (see claude_classifier._suppress_secondary_only).
CMS_SECONDARY_SIGNATURES = {
    "Litify": ["lightning.force.com"],
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

# Anchor text / URL fragments that signal an internal "how we work / what
# software we run" page — e.g. an About > "Case Management System" page or a
# "Our Technology" page. Some firms name their CMS only on a page like this,
# one click off the homepage and outside SITE_PATHS_TO_CHECK (the mr.law case:
# Filevine named on /about-us/case-management-system/). The fingerprinter finds
# these same-domain links on any fetched page and follows them one hop, scanning
# the destination body for CMS signatures. Matched against the link's visible
# text AND its href (with -/_/ normalized to spaces), so "case-management-system"
# in a URL matches "case management".
CMS_INFO_LINK_HINTS = [
    "case management", "case management system", "case management software",
    "our technology", "legal technology", "technology we use", "our software",
]

# Common subpages worth checking in addition to the homepage. Many firms
# expose client-portal links from contact/intake pages, not the homepage.
SITE_PATHS_TO_CHECK = ["", "/contact", "/contact-us", "/careers", "/intake", "/client-portal", "/login", "/client-login"]

# Pages most likely to list individual attorneys by name. Scraped during
# Stage 2 specifically so Claude has content to estimate attorney headcount.
ATTORNEY_LISTING_PATHS = ["/attorneys", "/our-team", "/team", "/our-attorneys", "/about-us", "/about", "/lawyers", "/people"]

# Pages to check when scraping for decision-maker contacts.
CONTACT_PATHS_TO_CHECK = ["/attorneys", "/our-team", "/team", "/our-attorneys", "/about-us", "/about"]

# Firm-wide pages worth scanning for a published attorney email when the team
# page and the decision-maker's own bio page didn't yield one (Stage 3 email
# scrape fallback). Kept small to bound extra fetches.
EMAIL_SCRAPE_FIRMWIDE_PATHS = ["", "/contact", "/contact-us"]

# Email domains to ignore when scraping firm websites for a decision-maker's
# email (Stage 3). The firm-domain + name-match gate already excludes almost all
# noise, but this is a cheap early filter for embeds, trackers, asset hosts, and
# site-builder boilerplate that occasionally appear in mailto:/page text.
# When no NAME-matched email is found on the firm's own domain, Stage 3 may fall
# back to a generic firm inbox on that domain (still never off-domain or another
# person's address). Only email local-parts in this set qualify — genuine firm
# contact inboxes, not a different attorney's personal address. Ordered by
# preference (the first match wins). Matched against the local-part lowercased
# with punctuation stripped (so "contact-us" -> "contactus").
EMAIL_GENERIC_LOCALPARTS = (
    "info", "contact", "contactus", "intake", "newcase", "newcases", "newclients",
    "clientservices", "office", "reception", "frontdesk", "admin", "inquiries",
    "inquiry", "hello", "mail", "email", "firm", "legal", "help", "support",
)

EMAIL_SCRAPE_DENYLIST_DOMAINS = {
    "example.com", "example.org", "email.com", "domain.com", "yourdomain.com",
    "sentry.io", "sentry-cdn.com", "wix.com", "wixpress.com", "squarespace.com",
    "godaddy.com", "secureserver.net", "schema.org", "w3.org", "sentry.wixpress.com",
    "googleapis.com", "gstatic.com", "google.com", "cloudflare.com", "jsdelivr.net",
    "fontawesome.com", "cloudflareinsights.com", "ggpht.com", "sentry-next.wixpress.com",
}

# --- CMS vendor reverse-lookup (Stage 6: vendor backlinks) -----------------
# CMS vendors publish their own customers / testimonials / case studies. If one
# of our sourced firms shows up on a vendor's customer page — either as an
# outbound link to (or plain-text mention of) the firm's domain (strongest), or
# by firm name (weaker) — that's high-quality evidence the firm runs that CMS.
# Stage 6 scrapes these pages and back-tracks the mentions onto our firm list.
# Pure scraping, no APIs.
CMS_VENDOR_HOMEPAGES = {
    "CloudLex": "https://www.cloudlex.com",
    "Filevine": "https://www.filevine.com",
    "Litify": "https://www.litify.com",
    "SmartAdvocate": "https://www.smartadvocate.com",
    "CASEpeer": "https://www.casepeer.com",
}

# Best-known direct customer-story / testimonial / case-study paths per vendor,
# fetched directly in addition to any pages discovered by crawling one hop from
# the homepage. A 404 here is harmless (it's just skipped). Update as vendor
# sites move these pages around.
CMS_VENDOR_SEED_PATHS = {
    "CloudLex": ["/testimonials/", "/case-studies/", "/customers/", "/reviews/"],
    "Filevine": ["/customers/", "/case-studies/", "/testimonials/", "/reviews/"],
    "Litify": ["/customers/", "/case-studies/", "/customer-stories/"],
    "SmartAdvocate": ["/testimonials/", "/case-studies/", "/clients/", "/reviews/"],
    "CASEpeer": ["/testimonials/", "/case-studies/", "/customers/", "/reviews/"],
}

# Anchor-text / href fragments that signal a customer-story / testimonial /
# case-study page. Stage 6 follows same-domain links matching any of these one
# hop from each vendor homepage, so discovery adapts when vendors relocate
# these pages. Matched against link href and visible text (lowercased).
CMS_VENDOR_PAGE_HINTS = [
    "testimonial", "customer", "case-study", "case-studies", "casestudies",
    "case study", "success-story", "success-stories", "success story",
    "client-story", "client-stories", "customer-story", "customer-stories",
    "review", "results", "who-we-serve", "clients",
]

# Polite ceiling on vendor pages fetched per vendor (homepage + seeds +
# discovered links), so a vendor that links hundreds of individual stories
# doesn't balloon a single run.
CMS_VENDOR_MAX_PAGES = 40

# Tokens stripped from a firm name before name-matching it against vendor page
# text, leaving the distinctive (usually surname) core. Lowercased, punctuation
# removed. Keeps name matching from firing on generic words every PI firm shares.
FIRM_NAME_STOPWORDS = {
    "law", "laws", "firm", "firms", "llc", "llp", "pllc", "pc", "pa", "ltd",
    "co", "attorney", "attorneys", "lawyer", "lawyers", "associates", "associate",
    "group", "office", "offices", "the", "and", "of", "a", "esq", "esquire",
    "injury", "personal", "accident", "accidents", "trial", "legal", "justice",
    "counsel", "partners", "plc", "p", "c", "l",
}

# Geographic + practice-area + marketing words that are NOT distinctive enough to
# match a firm on a vendor page by themselves. A firm whose name core reduces to
# only these (e.g. "PA Medical Malpractice Lawyers" -> "medical malpractice",
# "New York Injury Law" -> "new york", "Win Big Law" -> "win big") is skipped for
# name matching, because the core appears in generic page text everywhere. The
# full core is still used as the search phrase — these only gate whether a name
# match is distinctive enough to trust (firm-surname firms are unaffected).
FIRM_NAME_GENERIC_TOKENS = {
    # geography (our target states + their metros/boroughs + common qualifiers)
    "new", "york", "jersey", "pennsylvania", "connecticut", "philadelphia",
    "philly", "pittsburgh", "newark", "brooklyn", "bronx", "manhattan", "queens",
    "staten", "island", "long", "hartford", "harrisburg", "allentown", "scranton",
    "buffalo", "rochester", "syracuse", "albany", "trenton", "camden", "stamford",
    "bridgeport", "haven", "city", "county", "state", "metro", "tri", "valley",
    "american", "america", "national", "northeast", "eastern", "western",
    "central", "downtown", "uptown", "greater", "regional",
    # practice areas / case types
    "medical", "malpractice", "car", "auto", "truck", "motorcycle", "vehicle",
    "motor", "pedestrian", "bicycle", "slip", "fall", "premises", "liability",
    "construction", "wrongful", "death", "nursing", "abuse", "dog", "bite",
    "brain", "spinal", "burn", "workers", "compensation", "comp", "disability",
    "mass", "tort", "products", "product", "defective", "rideshare", "uber",
    "lyft", "rideshare", "catastrophic", "victims", "victim", "claims", "claim",
    # marketing / superlatives
    "win", "big", "best", "top", "super", "elite", "premier", "trusted", "strong",
    "fighter", "fighters", "hero", "heroes", "champion", "champions", "results",
    "winning", "aggressive", "fierce", "serious", "help", "helpline", "now",
}

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


# --- Attorney-headcount vetting window ------------------------------------
# A firm must have an estimated attorney count within this inclusive range to
# (a) pass the Stage 1b headcount vet gate and proceed through the paid
# enrichment stages, and (b) appear in final_output.csv. Stage 4 enforces it
# authoritatively, so the deliverable only ever contains in-range firms.
ATTORNEY_MIN_COUNT = 5
ATTORNEY_MAX_COUNT = 100


def attorney_count_in_range(est_attorneys) -> bool:
    """True iff est_attorneys parses to an int within [MIN, MAX] inclusive.
    Blank / unparseable / 0 counts as OUT of range — we never confirmed the
    firm has at least ATTORNEY_MIN_COUNT attorneys, so it's excluded."""
    try:
        n = int(float(est_attorneys))
    except (TypeError, ValueError):
        return False
    return ATTORNEY_MIN_COUNT <= n <= ATTORNEY_MAX_COUNT


# --- Claude classification model ---
# Cheapest current model — good enough for the structured classification task.
# Bump to claude-sonnet-4-6 for a small batch if Haiku's calls look unreliable.
CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# --- Politeness / rate limiting for direct site fetches ---
# Timeout bumped to 15s: large firm sites (often the ones we most want — they
# have the attorneys) are slower and were timing out at 8s, yielding a false
# est_attorneys=0 that wrongly dropped them as "too small".
SITE_FETCH_TIMEOUT_SECONDS = 15
SITE_FETCH_DELAY_SECONDS = 1.0
# A realistic desktop-browser User-Agent. A bot-flavored UA gets HTTP 403'd by
# the WAFs (Cloudflare etc.) that bigger firms run, so the fetch returned no
# attorney content and the firm was undercounted to ~0. This alone doesn't beat
# JS-challenge WAFs (those need the Playwright render fallback), but it recovers
# the many sites that block on the UA string only.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
# Sent alongside the UA on every site fetch so requests look like a real browser
# navigation, not a bare programmatic GET (another common block trigger).
SITE_FETCH_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
}

# --- Apollo rate limiting ---
# Politeness delay between successive Apollo API calls (people/match email
# enrichment + people search), mirroring SITE_FETCH_DELAY_SECONDS. Apollo
# enforces per-minute limits that vary by plan; 1.0s keeps a single-threaded
# run comfortably under most tiers. Raise if you see HTTP 429s.
APOLLO_RATE_LIMIT_DELAY_SECONDS = 1.0

# --- API cost rates (USD) -------------------------------------------------
# Per-unit rates used by api_wrappers/cost_tracker.py to estimate spend on
# every logged API call. Claude rates are per MILLION tokens (MTok); cache_read
# is ~0.1x input and cache_write (5-minute TTL) is ~1.25x input, per Anthropic
# pricing. Exa/Google Places/Apollo are flat per-call/per-credit. Edit these to
# match your actual contracted rates — they only affect the *estimate*, never
# the calls themselves.
COST_RATES = {
    "claude": {
        # Haiku 4.5 — the default CLAUDE_MODEL (both the dated ID and the alias).
        "claude-haiku-4-5-20251001": {
            "input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25,
        },
        "claude-haiku-4-5": {
            "input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25,
        },
        # Sonnet 4.6 — the documented fallback if Haiku looks unreliable.
        "claude-sonnet-4-6": {
            "input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75,
        },
    },
    # Exa neural search — flat per search call (each query the Stage 2 wrapper
    # runs is one search; ~$5 / 1000 searches on typical neural-search pricing).
    "exa": {"per_search": 0.005},
    # Google Places API (New) Text Search — $17 / 1000 requests.
    "google_places": {"per_request": 0.017},
    # Apollo — billed per credit consumed (a people/match that reveals an email
    # or a people-search page). Placeholder rate; set to your plan's credit cost.
    "apollo": {"per_credit": 0.05},
}
