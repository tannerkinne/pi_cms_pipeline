"""
Stage 2 — CMS detection. This is the core of the whole pipeline.

For each firm sourced in Stage 1, combines three evidence sources:
  1. Site fingerprinting (free — direct HTTP fetch + regex, no API cost)
     Includes careers-page job title extraction for hiring signal detection.
  2. Exa neural search (one combined query per firm — uses Exa credits)
  3. Claude classification synthesizing all of the above (uses Anthropic credits)

Resumable: firms already present in data/cms_results.csv are skipped, so
a crash or an extended --limit doesn't redo paid calls on firms already done.

Run with --dry-run to validate wiring using mock evidence at zero cost, or
--skip-exa to cut costs further once you've confirmed which signal is paying
off for your sample (see README).
"""
import argparse
import os
import sys
import warnings

# This repo's macOS Python links against LibreSSL, so urllib3 v2 emits a
# NotOpenSSLWarning on every requests call (see docs/headless_browser_scope.md §6).
# Suppress ONLY that one warning by message so the per-firm / render logs stay
# readable. Scoped narrowly on purpose: all other warnings still surface, and
# this is a no-op on environments that don't emit it.
warnings.filterwarnings("ignore", message=r".*OpenSSL.*", module="urllib3")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import site_fingerprint
from api_wrappers import claude_classifier, exa_client, cost_tracker
from utils import DATA_DIR, read_csv_rows, append_csv_row, already_processed_keys, CallCounter

INPUT_PATH = os.path.join(DATA_DIR, "firms_raw.csv")
OUTPUT_PATH = os.path.join(DATA_DIR, "cms_results.csv")

FIELDNAMES = [
    "domain", "firm_name", "cms_detected", "cms_confidence", "cms_evidence",
    "hiring_signal", "hiring_signal_evidence", "est_attorneys", "plaintiff_pi_focus",
    "plaintiff_pi_focus_note", "trial_focused", "site_pages_checked", "site_errors",
]


def _mock_evidence(firm_name: str) -> dict:
    """Deterministic-ish mock evidence for --dry-run wiring checks."""
    return {
        "fingerprint": {
            "hits": {},
            "pages_checked": [],
            "errors": [],
            "homepage_text_snippet": f"{firm_name} fights for injury victims on a contingency-fee basis.",
            "job_titles_found": [],
        },
        "exa_evidence": [],
    }


def main():
    parser = argparse.ArgumentParser(description="Detect CMS for sourced firms.")
    parser.add_argument("--limit", type=int, default=10,
                        help="Max firms to process this run (default 10).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Use mock evidence; no paid API calls at all.")
    parser.add_argument("--skip-exa", action="store_true",
                        help="Skip Exa search step (saves Exa credits; relies on fingerprint only).")
    parser.add_argument("--render", choices=["off", "fallback", "always"], default="off",
                        help="Headless-browser (Playwright) fallback for JS-rendered sites. "
                             "'off' (default) = static only; 'fallback' = render only low-signal "
                             "firms; 'always' = render every firm (debug). Requires the optional "
                             "render extra (requirements-render.txt). Never used in --dry-run.")
    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    cost_tracker.set_context(stage="02")
    firms = read_csv_rows(INPUT_PATH)
    if not firms:
        print(f"No firms found in {INPUT_PATH} — run Stage 1 first.")
        return

    done_domains = already_processed_keys(OUTPUT_PATH, "domain")
    counter = CallCounter()

    todo = [f for f in firms if f["domain"] not in done_domains][: args.limit]
    print(f"Stage 2: processing {len(todo)} firms ({len(done_domains)} already done, skipped).")

    for i, firm in enumerate(todo, 1):
        domain = firm["domain"]
        firm_name = firm["firm_name"]
        cost_tracker.set_context(stage="02", domain=domain)
        print(f"[{i}/{len(todo)}] {firm_name} ({domain})")

        if args.dry_run:
            ev = _mock_evidence(firm_name)
            counter.tick("dry_run_mock_evidence", 1)
        else:
            fingerprint = site_fingerprint.fingerprint_site(domain, render=args.render)
            counter.tick("site_fetch (free)", len(fingerprint.get("pages_checked", [])) + len(fingerprint.get("errors", [])))
            if fingerprint.get("rendered"):
                counter.tick("playwright_render", 1)

            exa_evidence = []
            if not args.skip_exa:
                exa_evidence = exa_client.search_cms_evidence(
                    firm_name, firm.get("city", ""), firm.get("state", "")
                )
                counter.tick("exa_search", 1)

            ev = {
                "fingerprint": fingerprint,
                "exa_evidence": exa_evidence,
            }

        if args.dry_run:
            result = {
                "cms_detected": "Unknown",
                "cms_confidence": "Low",
                "cms_evidence": "dry-run mock — no real classification performed",
                "hiring_signal": "No",
                "hiring_signal_evidence": "dry-run mock",
                "est_attorneys": 0,
                "plaintiff_pi_focus": "Yes",
                "plaintiff_pi_focus_note": "dry-run mock",
                "trial_focused": "Unknown",
            }
        else:
            result = claude_classifier.classify_firm(
                firm_name=firm_name,
                fingerprint_result=ev["fingerprint"],
                careers_job_titles=ev["fingerprint"].get("job_titles_found", []),
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
            "est_attorneys": result["est_attorneys"],
            "plaintiff_pi_focus": result["plaintiff_pi_focus"],
            "plaintiff_pi_focus_note": result["plaintiff_pi_focus_note"],
            "trial_focused": result["trial_focused"],
            "site_pages_checked": "; ".join(ev["fingerprint"].get("pages_checked", [])),
            "site_errors": "; ".join(ev["fingerprint"].get("errors", [])),
        }
        append_csv_row(OUTPUT_PATH, row, FIELDNAMES)
        print(f"    -> {result['cms_detected']} ({result['cms_confidence']} confidence)")

    print(f"\nStage 2 complete. Results in {OUTPUT_PATH}")
    print(f"API usage this run: {counter.summary()}")
    print(cost_tracker.session_summary("Stage 2"))
    if args.dry_run:
        print("NOTE: this was a --dry-run. No paid API calls were made; results are mock data.")


if __name__ == "__main__":
    main()
