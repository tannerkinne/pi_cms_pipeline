"""
Shared configuration: CMS detection signatures, fit-score rules, target states.
Edit this file to tune scope without touching pipeline logic.
"""

# --- Geography & sourcing scope (from project brief) ---
TARGET_STATES = ["new jersey", "new york", "pennsylvania"]
TARGET_KEYWORDS = ["personal injury", "plaintiff personal injury"]
EMPLOYEE_RANGE = "5,50"  # Apollo organization_num_employees_ranges format: "min,max"

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

# Common subpages worth checking in addition to the homepage. Many firms
# expose client-portal links from contact/intake pages, not the homepage.
SITE_PATHS_TO_CHECK = ["", "/contact", "/contact-us", "/careers", "/intake", "/client-portal"]

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

# Apollo technology UIDs to probe for (underscores replace spaces/periods).
# These are guesses — Apollo's 1,500+ tracked technologies skew toward
# broad B2B SaaS, so niche legal CMS platforms may not be tracked at all.
# The pipeline checks this list against Apollo's actual supported-technologies
# export at runtime and only uses what's really there.
CMS_APOLLO_TECH_UID_GUESSES = {
    "CloudLex": ["cloudlex"],
    "Filevine": ["filevine"],
    "Litify": ["litify", "salesforce"],
    "SmartAdvocate": ["smartadvocate"],
    "CASEpeer": ["casepeer"],
}


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
