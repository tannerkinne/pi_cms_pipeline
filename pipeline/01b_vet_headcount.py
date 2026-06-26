"""
Stage 1b — Attorney-headcount vet gate.

Sits between sourcing (Stage 1) and the expensive CMS/contact enrichment
(Stages 2-3). For each freshly-sourced firm it does the CHEAP work only:

  1. Fingerprint the firm's site (free HTTP fetch + regex; renders JS team
     pages in fallback mode so a client-rendered attorney list isn't missed).
  2. Ask Claude for a single, bounded attorney-headcount estimate (tiny prompt,
     cheap CLAUDE_MODEL — a fraction of a full Stage 2 classification).

Only firms whose estimate lands inside the target band
(config.ATTORNEY_MIN_COUNT .. ATTORNEY_MAX_COUNT, inclusive) are marked "pass".
Stage 2 then processes ONLY the passing firms, so we never pay Exa + full
classification + contact enrichment on firms that are too small/large to use.

Resumable: firms already present in data/firms_vetted.csv are skipped.

Run with --dry-run to validate wiring with mock estimates at zero cost.

Output: data/firms_vetted.csv
Columns: place_id, firm_name, website, domain, city, state, source,
         est_attorneys, vet_status, vet_basis
  vet_status is one of: pass | fail_low | fail_high
"""
import argparse
import os
import sys
import warnings

# Same LibreSSL/urllib3 NotOpenSSLWarning suppression as Stage 2.
warnings.filterwarnings("ignore", message=r".*OpenSSL.*", module="urllib3")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import site_fingerprint
from api_wrappers import claude_classifier, cost_tracker
from config import ATTORNEY_MIN_COUNT, ATTORNEY_MAX_COUNT
from utils import (DATA_DIR, read_csv_rows, append_csv_row,
                   already_processed_keys, CallCounter)

INPUT_PATH = os.path.join(DATA_DIR, "firms_raw.csv")
OUTPUT_PATH = os.path.join(DATA_DIR, "firms_vetted.csv")

FIELDNAMES = [
    "place_id", "firm_name", "website", "domain", "city", "state", "source",
    "est_attorneys", "vet_status", "vet_basis",
]


def _classify_status(n: int, has_evidence: bool) -> str:
    """Map an estimate to a gate decision.

    Crucially, an estimate of <MIN means 'too small' ONLY when we actually read
    some firm content (a team page, attorney names, or homepage text). If the
    fetch came back empty — the site blocked us, timed out, or is JS-walled — a 0
    is a *false* zero, not a real solo shop, so we mark it 'unknown' instead of
    'fail_low'. Unknown firms are NOT dropped: Stage 2 still enriches them (with
    a real browser UA, render fallback, and Exa web search) to get a true count.
    This is what stops big firms like Celino from being thrown out on a failed
    fetch. See [[stage1b-unreachable-not-too-small]]."""
    if n > ATTORNEY_MAX_COUNT:
        return "fail_high"
    if ATTORNEY_MIN_COUNT <= n <= ATTORNEY_MAX_COUNT:
        return "pass"
    return "fail_low" if has_evidence else "unknown"


def _has_readable_evidence(fingerprint: dict) -> bool:
    """True if the fetch recovered ANY firm content to base a headcount on."""
    return bool(fingerprint.get("attorney_names")) \
        or bool((fingerprint.get("attorney_page_text") or "").strip()) \
        or bool((fingerprint.get("homepage_text_snippet") or "").strip())


def main():
    parser = argparse.ArgumentParser(
        description="Vet sourced firms by attorney headcount before paid enrichment.")
    parser.add_argument("--limit", type=int, default=10,
                        help="Max firms to vet this run (default 10).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Use a mock estimate; no fingerprint fetches or Claude calls.")
    parser.add_argument("--render", choices=["off", "fallback", "always"], default="fallback",
                        help="Headless-browser fallback for JS-rendered team pages (default "
                             "'fallback' — render only when static finds <2 attorney names, so a "
                             "client-rendered roster doesn't get a firm wrongly rejected as too small). "
                             "Degrades gracefully to static if Playwright isn't installed.")
    parser.add_argument("--only", default="",
                        help="Comma-separated domains to (re)vet, overwriting their rows; ignores --limit.")
    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    cost_tracker.set_context(stage="01b")
    firms = read_csv_rows(INPUT_PATH)
    if not firms:
        print(f"No firms found in {INPUT_PATH} — run Stage 1 first.")
        return

    counter = CallCounter()
    only = {d.strip() for d in args.only.split(",") if d.strip()}

    if only:
        existing = read_csv_rows(OUTPUT_PATH)
        todo = [f for f in firms if f["domain"] in only]
        todo_domains = {f["domain"] for f in todo}
        retained = [r for r in existing if r["domain"] not in todo_domains]
        from utils import write_csv_rows
        write_csv_rows(OUTPUT_PATH, retained, FIELDNAMES, mode="w")
        print(f"Stage 1b [only]: re-vetting {len(todo)} firms (overwriting their rows).")
    else:
        done_domains = already_processed_keys(OUTPUT_PATH, "domain")
        todo = [f for f in firms if f["domain"] not in done_domains][: args.limit]
        print(f"Stage 1b: vetting {len(todo)} firms ({len(done_domains)} already vetted, skipped).")

    passed = fail_low = fail_high = unknown = 0

    for i, firm in enumerate(todo, 1):
        domain = firm["domain"]
        firm_name = firm["firm_name"]
        cost_tracker.set_context(stage="01b", domain=domain)
        print(f"[{i}/{len(todo)}] {firm_name} ({domain})")

        if args.dry_run:
            est = {"est_attorneys": 10, "basis": "dry-run mock estimate"}
            has_evidence = True
            counter.tick("dry_run_mock_estimate", 1)
        else:
            fingerprint = site_fingerprint.fingerprint_site(domain, render=args.render)
            counter.tick("site_fetch (free)",
                         len(fingerprint.get("pages_checked", [])) + len(fingerprint.get("errors", [])))
            if fingerprint.get("rendered"):
                counter.tick("playwright_render", 1)
            has_evidence = _has_readable_evidence(fingerprint)
            est = claude_classifier.estimate_headcount(firm_name, fingerprint)
            counter.tick("claude_headcount_estimate", 1)

        n = est["est_attorneys"]
        status = _classify_status(n, has_evidence)
        if status == "pass":
            passed += 1
        elif status == "fail_low":
            fail_low += 1
        elif status == "fail_high":
            fail_high += 1
        else:
            unknown += 1
            if not est.get("basis"):
                est["basis"] = ""
            est["basis"] = ("site unreachable (blocked/timeout/JS-walled) — no content read; "
                            "not rejected, sent to Stage 2 for a fuller estimate. " + est["basis"]).strip()

        append_csv_row(OUTPUT_PATH, {
            "place_id": firm.get("place_id", ""),
            "firm_name": firm_name,
            "website": firm.get("website", ""),
            "domain": domain,
            "city": firm.get("city", ""),
            "state": firm.get("state", ""),
            "source": firm.get("source", ""),
            "est_attorneys": n,
            "vet_status": status,
            "vet_basis": est.get("basis", ""),
        }, FIELDNAMES)
        print(f"    -> ~{n} attorneys [{status}]")

    print(f"\nStage 1b complete. Results in {OUTPUT_PATH}")
    print(f"  PASS (5-{ATTORNEY_MAX_COUNT}): {passed} | "
          f"fail_low (<{ATTORNEY_MIN_COUNT}): {fail_low} | "
          f"fail_high (>{ATTORNEY_MAX_COUNT}): {fail_high} | "
          f"unknown (unreachable, sent to Stage 2): {unknown}")
    print(f"  API usage this run: {counter.summary()}")
    print(cost_tracker.session_summary("Stage 1b"))
    if args.dry_run:
        print("NOTE: this was a --dry-run. No paid calls were made; estimates are mock data.")


if __name__ == "__main__":
    main()
