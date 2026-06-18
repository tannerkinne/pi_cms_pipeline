"""
Stage 1 — Source candidate firms via Google Places Text Search API.

Searches each target state (NJ/NY/PA/CT) with multiple query strings covering
different ways people describe PI firms, then deduplicates by domain. This gives
broader local coverage than a B2B company database — small regional firms are on
Google Maps long before they appear in Apollo or similar products.

Costs: ~$17/1000 Places API calls. Each page of 20 results = 1 call.
Run with --dry-run to validate pipeline wiring with zero API spend.

Output: data/firms_raw.csv
Columns: place_id, firm_name, website, domain, city, state, source
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api_wrappers import google_places_client
from config import TARGET_STATES, GOOGLE_PLACES_STATE_BBOXES, GOOGLE_PLACES_QUERIES, GOOGLE_PLACES_MAX_PAGES
from utils import DATA_DIR, write_csv_rows, already_processed_keys, CallCounter

FIELDNAMES = ["place_id", "firm_name", "website", "domain", "city", "state", "source"]
OUTPUT_PATH = os.path.join(DATA_DIR, "firms_raw.csv")

EXCLUDE_NAME_HINTS = ["insurance", "defense", "law enforcement"]


def _domain_from_website(website: str) -> str:
    if not website:
        return ""
    d = website.strip().lower().replace("http://", "").replace("https://", "")
    return d.split("/")[0]


def _mock_places(n: int) -> list:
    """Stub data for --dry-run. Clearly fake values so they're never mistaken for real rows."""
    return [
        {
            "place_id": f"mock_place_{i}",
            "firm_name": f"Mock Injury Law Group {i}",
            "website": f"https://mockinjurylaw{i}.example.com",
            "city": "Newark",
            "state": "new jersey",
        }
        for i in range(n)
    ]


def main():
    parser = argparse.ArgumentParser(description="Source candidate PI firms via Google Places.")
    parser.add_argument("--limit", type=int, default=10,
                        help="Max new firms to add this run (default 10; raise to ~200 for a full run).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip real API calls; use mock data to validate wiring for free.")
    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    existing_domains = already_processed_keys(OUTPUT_PATH, "domain")
    counter = CallCounter()

    collected = []
    seen_domains = set(existing_domains)
    no_website_skipped = 0

    if args.dry_run:
        mock_rows = _mock_places(args.limit)
        counter.tick("google_places_search (DRY RUN - not actually called)", 1)
        for m in mock_rows:
            domain = _domain_from_website(m["website"])
            if domain and domain not in seen_domains:
                collected.append({**m, "domain": domain, "source": "google_places_text_search"})
                seen_domains.add(domain)
    else:
        for state_name in TARGET_STATES:
            if len(collected) >= args.limit:
                break
            bbox = GOOGLE_PLACES_STATE_BBOXES.get(state_name)
            if not bbox:
                print(f"  [!] No bounding box configured for '{state_name}' — skipping.")
                continue

            for query in GOOGLE_PLACES_QUERIES:
                if len(collected) >= args.limit:
                    break
                page_token = None
                for page_num in range(1, GOOGLE_PLACES_MAX_PAGES + 1):
                    if len(collected) >= args.limit:
                        break
                    result = google_places_client.search_pi_firms(
                        state_name=state_name,
                        bbox=bbox,
                        text_query=query,
                        page_token=page_token,
                    )
                    counter.tick(f"google_places_text_search ({state_name} / {query})", 1)

                    for place in result.get("places", []):
                        if len(collected) >= args.limit:
                            break
                        fields = google_places_client.extract_firm_fields(place)
                        if fields is None:
                            no_website_skipped += 1
                            continue
                        domain = _domain_from_website(fields["website"])
                        if not domain or domain in seen_domains:
                            continue
                        name_lower = fields["firm_name"].lower()
                        if any(hint in name_lower for hint in EXCLUDE_NAME_HINTS):
                            continue
                        row = {**fields, "domain": domain, "source": "google_places_text_search"}
                        collected.append(row)
                        seen_domains.add(domain)

                    page_token = result.get("next_page_token")
                    if not page_token:
                        break
                    google_places_client.polite_sleep()

    write_csv_rows(OUTPUT_PATH, collected, FIELDNAMES, mode="a" if existing_domains else "w")

    print(f"Stage 1 complete: {len(collected)} new firms written to {OUTPUT_PATH}")
    if no_website_skipped:
        print(f"  (skipped {no_website_skipped} Places results with no website URL)")
    print(f"  API usage: {counter.summary()}")
    if args.dry_run:
        print("  NOTE: this was a --dry-run. No API credits were spent; data is mock data.")


if __name__ == "__main__":
    main()
