"""
Stage 2 — CMS detection. This is the core of the whole pipeline.

For each firm sourced in Stage 1, combines four evidence sources:
  1. Site fingerprinting (free — direct HTTP fetch + regex, no API cost)
  2. Apollo job postings + technology tags (uses Apollo credits)
  3. Exa neural search (one combined query per firm — uses Exa credits)
  4. Claude classification synthesizing all of the above (uses Anthropic credits)

Resumable: firms already present in data/cms_results.csv are skipped, so
a crash or an extended --limit doesn't redo paid calls on firms already done.

Run with --dry-run to validate wiring using mock evidence at zero cost, or
--skip-exa / --skip-apollo-jobs to cut costs further once you've confirmed
which signal is actually paying off for your sample (see README).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import site_fingerprint
from api_wrappers import claude_classifier, apollo_client, exa_client
from config import CMS_APOLLO_TECH_UID_GUESSES
from utils import DATA_DIR, read_csv_rows, append_csv_row, already_processed_keys, CallCounter

INPUT_PATH = os.path.join(DATA_DIR, "firms_raw.csv")
RAW_JSONL_PATH = os.path.join(DATA_DIR, "firms_raw_full.jsonl")
OUTPUT_PATH = os.path.join(DATA_DIR, "cms_results.csv")

FIELDNAMES = [
    "domain", "firm_name", "cms_detected", "cms_confidence", "cms_evidence",
    "hiring_signal", "hiring_signal_evidence", "plaintiff_pi_focus",
    "plaintiff_pi_focus_note", "site_pages_checked", "site_errors",
]


def _load_raw_org_by_domain() -> dict:
    by_domain = {}
    if not os.path.exists(RAW_JSONL_PATH):
        return by_domain
    with open(RAW_JSONL_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                by_domain[rec["domain"]] = rec.get("raw", {})
            except json.JSONDecodeError:
                continue
    return by_domain


def _extract_technologies(raw_org: dict, tracked_uids: set) -> list:
    """Pull whatever technology field Apollo's org object actually has,
    filtered down to ones Apollo confirms it tracks (if we could fetch the
    tracked list at all — if not, just pass through whatever's there)."""
    for key in ("technologies", "current_technologies", "technology_names"):
        if raw_org.get(key):
            techs = raw_org[key]
            if isinstance(techs, list):
                names = [t.get("name", t) if isinstance(t, dict) else str(t) for t in techs]
                if tracked_uids:
                    return [n for n in names if n.lower().replace(" ", "_") in tracked_uids] or names
                return names
    return []


def _mock_evidence(firm_name: str) -> dict:
    """Deterministic-ish mock evidence for --dry-run wiring checks."""
    return {
        "fingerprint": {"hits": {}, "pages_checked": [], "errors": [], "homepage_text_snippet": f"{firm_name} fights for injury victims on a contingency-fee basis."},
        "job_postings": [],
        "technologies": [],
        "exa_evidence": [],
    }


def main():
    parser = argparse.ArgumentParser(description="Detect CMS for sourced firms.")
    parser.add_argument("--limit", type=int, default=10, help="Max firms to process this run (default 10).")
    parser.add_argument("--dry-run", action="store_true", help="Use mock evidence; no paid API calls at all.")
    parser.add_argument("--skip-exa", action="store_true", help="Skip Exa search step (saves Exa credits; relies on fingerprint+Apollo only).")
    parser.add_argument("--skip-apollo-jobs", action="store_true", help="Skip Apollo job-postings lookup (saves Apollo credits).")
    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    firms = read_csv_rows(INPUT_PATH)
    if not firms:
        print(f"No firms found in {INPUT_PATH} — run Stage 1 first.")
        return

    done_domains = already_processed_keys(OUTPUT_PATH, "domain")
    raw_orgs = _load_raw_org_by_domain()
    counter = CallCounter()

    tracked_uids = set()
    if not args.dry_run:
        tracked_uids = apollo_client.get_supported_technology_uids()
        counter.tick("apollo_supported_technologies_csv (one-time)", 1)
        relevant = {cms for cms, guesses in CMS_APOLLO_TECH_UID_GUESSES.items() if any(g in tracked_uids for g in guesses)}
        if relevant:
            print(f"  Apollo tracks tech signals for: {', '.join(relevant)}")
        else:
            print("  Apollo does not appear to track any of our 5 target CMS platforms as technologies — relying on fingerprinting/jobs/Exa only.")

    todo = [f for f in firms if f["domain"] not in done_domains][: args.limit]
    print(f"Stage 2: processing {len(todo)} firms ({len(done_domains)} already done, skipped).")

    for i, firm in enumerate(todo, 1):
        domain = firm["domain"]
        firm_name = firm["firm_name"]
        print(f"[{i}/{len(todo)}] {firm_name} ({domain})")

        if args.dry_run:
            ev = _mock_evidence(firm_name)
            counter.tick("dry_run_mock_evidence", 1)
        else:
            fingerprint = site_fingerprint.fingerprint_site(domain)
            counter.tick("site_fetch (free)", len(fingerprint.get("pages_checked", [])) + len(fingerprint.get("errors", [])))

            job_postings = []
            if not args.skip_apollo_jobs and firm.get("apollo_org_id", "").strip():
                job_postings = apollo_client.get_job_postings(firm["apollo_org_id"])
                counter.tick("apollo_job_postings", 1)

            technologies = _extract_technologies(raw_orgs.get(domain, {}), tracked_uids)

            exa_evidence = []
            if not args.skip_exa:
                exa_evidence = exa_client.search_cms_evidence(firm_name, firm.get("city", ""), firm.get("state", ""))
                counter.tick("exa_search", 1)

            ev = {
                "fingerprint": fingerprint,
                "job_postings": job_postings,
                "technologies": technologies,
                "exa_evidence": exa_evidence,
            }

        if args.dry_run:
            result = {
                "cms_detected": "Unknown",
                "cms_confidence": "Low",
                "cms_evidence": "dry-run mock — no real classification performed",
                "hiring_signal": "No",
                "hiring_signal_evidence": "dry-run mock",
                "plaintiff_pi_focus": "Yes",
                "plaintiff_pi_focus_note": "dry-run mock",
            }
        else:
            result = claude_classifier.classify_firm(
                firm_name=firm_name,
                fingerprint_result=ev["fingerprint"],
                apollo_job_postings=ev["job_postings"],
                apollo_technologies=ev["technologies"],
                exa_evidence=ev["exa_evidence"],
            )
        counter.tick("claude_classification" + (" (DRY RUN - not actually called)" if args.dry_run else ""), 1)

        row = {
            "domain": domain,
            "firm_name": firm_name,
            "cms_detected": result["cms_detected"],
            "cms_confidence": result["cms_confidence"],
            "cms_evidence": result["cms_evidence"],
            "hiring_signal": result["hiring_signal"],
            "hiring_signal_evidence": result["hiring_signal_evidence"],
            "plaintiff_pi_focus": result["plaintiff_pi_focus"],
            "plaintiff_pi_focus_note": result["plaintiff_pi_focus_note"],
            "site_pages_checked": "; ".join(ev["fingerprint"].get("pages_checked", [])),
            "site_errors": "; ".join(ev["fingerprint"].get("errors", [])),
        }
        append_csv_row(OUTPUT_PATH, row, FIELDNAMES)
        print(f"    -> {result['cms_detected']} ({result['cms_confidence']} confidence)")
        if not args.dry_run and i < len(todo):
            apollo_client.polite_sleep()

    print(f"\nStage 2 complete. Results in {OUTPUT_PATH}")
    print(f"API usage this run: {counter.summary()}")
    if args.dry_run:
        print("NOTE: this was a --dry-run. No paid API calls were made; results are mock data.")


if __name__ == "__main__":
    main()
