"""
Stage 3 — Decision-maker lookup via Apollo's People API Search.

IMPORTANT CAVEAT: this endpoint is credit-free but requires a "master"
API key tier in Apollo. If your key isn't a master key, every call here
returns 403 and this stage will mark every firm "Unknown / requires
Apollo master API key" rather than crash. Check your Apollo plan/key
settings if you see that on every row — see README for details.

Output: data/contacts.csv
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import apollo_client
from utils import DATA_DIR, read_csv_rows, append_csv_row, already_processed_keys, CallCounter

INPUT_PATH = os.path.join(DATA_DIR, "firms_raw.csv")
OUTPUT_PATH = os.path.join(DATA_DIR, "contacts.csv")

FIELDNAMES = ["domain", "decision_maker_name", "decision_maker_title", "decision_maker_note"]

DECISION_MAKER_TITLES = [
    "managing partner", "chief operating officer", "coo",
    "director of operations", "owner", "founding partner",
]

PREFERRED_ORDER = ["director of operations", "coo", "chief operating officer", "managing partner", "owner", "founding partner"]


def _pick_best(people: list) -> dict:
    """Apollo's title matching is fuzzy/inclusive, so rank by the brief's
    stated preference (COO / Director of Ops first) rather than trusting
    result order."""
    def rank(p):
        title = (p.get("title") or "").lower()
        for i, pref in enumerate(PREFERRED_ORDER):
            if pref in title:
                return i
        return len(PREFERRED_ORDER)
    return sorted(people, key=rank)[0] if people else {}


def main():
    parser = argparse.ArgumentParser(description="Look up decision-maker contacts via Apollo.")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    firms = read_csv_rows(INPUT_PATH)
    if not firms:
        print(f"No firms found in {INPUT_PATH} — run Stage 1 first.")
        return

    done = already_processed_keys(OUTPUT_PATH, "domain")
    todo = [f for f in firms if f["domain"] not in done][: args.limit]
    counter = CallCounter()
    saw_master_key_block = False

    print(f"Stage 3: processing {len(todo)} firms ({len(done)} already done, skipped).")

    for i, firm in enumerate(todo, 1):
        domain = firm["domain"]
        org_id = firm.get("apollo_org_id", "").strip()
        print(f"[{i}/{len(todo)}] {firm['firm_name']} ({domain})")

        if args.dry_run or not org_id:
            row = {
                "domain": domain,
                "decision_maker_name": "Mock Person" if args.dry_run else "",
                "decision_maker_title": "Managing Partner" if args.dry_run else "",
                "decision_maker_note": "dry-run mock" if args.dry_run else "no apollo_org_id on file",
            }
        else:
            people = apollo_client.search_people_at_organization(org_id, DECISION_MAKER_TITLES)
            counter.tick("apollo_people_search", 1)
            if people is None:
                # 403 — master key required; distinct from a legitimate empty search result
                saw_master_key_block = True
                people = []
            best = _pick_best(people)
            if best:
                row = {
                    "domain": domain,
                    "decision_maker_name": best.get("name", ""),
                    "decision_maker_title": best.get("title", ""),
                    "decision_maker_note": "apollo_people_search",
                }
            else:
                row = {
                    "domain": domain,
                    "decision_maker_name": "",
                    "decision_maker_title": "",
                    "decision_maker_note": "Unknown / no match found" + (" (master API key required — see README)" if saw_master_key_block else ""),
                }

        append_csv_row(OUTPUT_PATH, row, FIELDNAMES)

    print(f"\nStage 3 complete. Results in {OUTPUT_PATH}")
    print(f"API usage this run: {counter.summary()}")
    if saw_master_key_block and not args.dry_run:
        print("NOTE: Apollo returned 403 for people lookups — this endpoint requires a 'master' API key tier. "
              "Check Settings → API Keys in your Apollo dashboard. See README.")


if __name__ == "__main__":
    main()
