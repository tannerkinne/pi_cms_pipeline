"""
Stage 1 — Source candidate firms via Apollo Organization Search.

Costs Apollo credits per page fetched (not per result), so pagination is
capped by --limit. Run with --dry-run to validate the pipeline wiring and
CSV schema without spending any credits at all.

Output: data/firms_raw.csv
Columns: apollo_org_id, firm_name, website, domain, city, state,
         est_employees, source
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api_wrappers import apollo_client
from config import TARGET_STATES, TARGET_KEYWORDS, EMPLOYEE_RANGE
from utils import DATA_DIR, write_csv_rows, already_processed_keys, CallCounter

FIELDNAMES = ["apollo_org_id", "firm_name", "website", "domain", "city", "state", "est_employees", "source"]
OUTPUT_PATH = os.path.join(DATA_DIR, "firms_raw.csv")
RAW_JSONL_PATH = os.path.join(DATA_DIR, "firms_raw_full.jsonl")  # full Apollo org objects, incl. technologies if present

# Defense / insurance-side keywords that sometimes get swept in under a
# generic "personal injury" keyword tag. Light filtering only — this is a
# coarse pre-filter, not a substitute for the plaintiff_pi_focus judgment
# Claude makes later in Stage 2 using actual homepage content.
EXCLUDE_NAME_HINTS = ["insurance", "defense", "law enforcement"]


def _domain_from_website(website: str) -> str:
    if not website:
        return ""
    d = website.strip().lower().replace("http://", "").replace("https://", "")
    return d.split("/")[0]


def _mock_organizations(n: int) -> list:
    """Stub data for --dry-run so the pipeline can be exercised with zero
    API spend. Clearly fake values so they're never mistaken for real rows."""
    return [
        {
            "id": f"mock_org_{i}",
            "name": f"Mock Injury Law Group {i}",
            "website_url": f"https://mockinjurylaw{i}.example.com",
            "city": "Newark",
            "state": "New Jersey",
            "estimated_num_employees": 22,
        }
        for i in range(n)
    ]


def main():
    parser = argparse.ArgumentParser(description="Source candidate PI firms via Apollo.")
    parser.add_argument("--limit", type=int, default=10, help="Max firms to source (default 10 — keep this small while testing).")
    parser.add_argument("--per-page", type=int, default=25, help="Apollo results per page (credits are spent per page, not per result).")
    parser.add_argument("--dry-run", action="store_true", help="Skip real Apollo calls; use mock data to validate wiring for free.")
    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    existing_domains = already_processed_keys(OUTPUT_PATH, "domain")
    counter = CallCounter()

    collected = []
    page = 1
    per_page = min(args.per_page, args.limit) if args.limit else args.per_page

    if args.dry_run:
        orgs = _mock_organizations(args.limit)
        counter.tick("apollo_organization_search (DRY RUN - not actually called)", 1)
    else:
        orgs = []
        while len(orgs) < args.limit:
            resp = apollo_client.search_organizations(
                keyword_tags=TARGET_KEYWORDS,
                locations=TARGET_STATES,
                employee_ranges=[EMPLOYEE_RANGE],
                page=page,
                per_page=per_page,
            )
            counter.tick("apollo_organization_search (page)", 1)
            batch = resp.get("organizations") or resp.get("accounts") or []
            if not batch:
                break
            orgs.extend(batch)
            page += 1
            if page > 20:  # hard safety cap regardless of --limit
                break
        orgs = orgs[: args.limit]

    for org in orgs:
        domain = _domain_from_website(org.get("website_url", ""))
        if not domain or domain in existing_domains:
            continue
        name_lower = (org.get("name") or "").lower()
        if any(hint in name_lower for hint in EXCLUDE_NAME_HINTS):
            continue
        collected.append({
            "apollo_org_id": org.get("id", ""),
            "firm_name": org.get("name", ""),
            "website": org.get("website_url", ""),
            "domain": domain,
            "city": org.get("city", ""),
            "state": org.get("state", ""),
            "est_employees": org.get("estimated_num_employees", ""),
            "source": "apollo_organization_search",
        })
        with open(RAW_JSONL_PATH, "a", encoding="utf-8") as jf:
            jf.write(json.dumps({"domain": domain, "raw": org}) + "\n")

    write_csv_rows(OUTPUT_PATH, collected, FIELDNAMES, mode="a" if existing_domains else "w")

    print(f"Stage 1 complete: {len(collected)} new firms written to {OUTPUT_PATH}")
    print(f"  (skipped {len(orgs) - len(collected)} as duplicates/excluded)")
    print(f"  API usage: {counter.summary()}")
    if args.dry_run:
        print("  NOTE: this was a --dry-run. No Apollo credits were spent; data is mock data.")


if __name__ == "__main__":
    main()
