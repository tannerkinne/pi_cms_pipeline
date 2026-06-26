#!/usr/bin/env python3
"""
Single entry point for the whole pipeline. Runs Stages 1-4 in order with a
consistent --limit and --dry-run applied throughout.

USAGE

  Free wiring check (no API keys needed at all):
    python run_pipeline.py --dry-run --limit 5

  Cheap real test (small batch, real APIs, see actual cost/quality):
    python run_pipeline.py --limit 5

  Full run once you trust the output:
    python run_pipeline.py --limit 200

  Cost-trimmed full run (skip Exa, the priciest per-firm signal):
    python run_pipeline.py --limit 200 --skip-exa

See README.md for required environment variables and per-stage cost notes.
"""
import argparse
import csv
import subprocess
import sys
import os

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
PIPELINE_DIR = os.path.join(ROOT_DIR, "pipeline")
DATA_DIR = os.path.join(ROOT_DIR, "data")
FIRMS_RAW_PATH = os.path.join(DATA_DIR, "firms_raw.csv")
FIRMS_VETTED_PATH = os.path.join(DATA_DIR, "firms_vetted.csv")
sys.path.insert(0, ROOT_DIR)

from api_wrappers import cost_tracker


def run_stage(script_name, extra_args):
    path = os.path.join(PIPELINE_DIR, script_name)
    cmd = [sys.executable, path] + extra_args
    print(f"\n{'=' * 60}\nRunning {script_name}\n{'=' * 60}", flush=True)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\n[!] {script_name} exited with code {result.returncode} — stopping pipeline.")
        sys.exit(result.returncode)


def _row_count(path):
    """Number of data rows (excludes header) in a CSV, or 0 if it doesn't exist."""
    if not os.path.exists(path):
        return 0
    with open(path, newline="", encoding="utf-8") as f:
        return max(0, sum(1 for _ in csv.reader(f)) - 1)


def _vet_counts(path):
    """(passing, processable) from firms_vetted.csv. passing = confirmed in 5-100;
    processable = passing + 'unknown' (unreachable firms Stage 2 still enriches)."""
    if not os.path.exists(path):
        return 0, 0
    with open(path, newline="", encoding="utf-8") as f:
        statuses = [(r.get("vet_status") or "").strip() for r in csv.DictReader(f)]
    passing = sum(1 for s in statuses if s == "pass")
    processable = passing + sum(1 for s in statuses if s == "unknown")
    return passing, processable


def main():
    parser = argparse.ArgumentParser(description="Run the full plaintiff-PI CMS-detection pipeline.")
    parser.add_argument("--limit", type=int, default=10,
                        help="TARGET number of in-range (5-100 attorney) firms to end up with. "
                             "With the vet gate on (default), the pipeline keeps sourcing + vetting "
                             "until this many firms PASS the gate, then enriches them — so --limit is "
                             "a goal, not a raw-firm cap. With --no-vet it reverts to the old meaning "
                             "(max firms pushed through end to end). Default 10; raise to refill the list.")
    parser.add_argument("--source-batch", type=int, default=100,
                        help="How many new firms to source per round while chasing --limit passing "
                             "firms (default 100). Larger = fewer Google Places re-scans, coarser overshoot.")
    parser.add_argument("--max-source-rounds", type=int, default=40,
                        help="Safety cap on sourcing rounds so an exhausted search can't loop forever "
                             "(default 40). The loop also stops on its own when sourcing finds no new firms.")
    parser.add_argument("--dry-run", action="store_true",
                        help="No real API calls anywhere; validates wiring with mock data.")
    parser.add_argument("--skip-exa", action="store_true",
                        help="Skip Exa search in Stage 2 to cut cost.")
    parser.add_argument("--no-vet", action="store_true",
                        help="Skip the Stage 1b attorney-headcount vet gate (5-100). Reverts --limit "
                             "to a raw-firm cap and processes every sourced firm. By default the gate "
                             "is on and the pipeline sources until --limit firms pass it.")
    parser.add_argument("--skip-contacts", action="store_true",
                        help="Skip Stage 3 entirely (decision-maker lookup).")
    parser.add_argument("--render", choices=["off", "fallback", "always"], default="off",
                        help="Stage 2 headless-browser fallback for JS-rendered sites "
                             "(requires the optional render extra; see requirements-render.txt).")
    parser.add_argument("--no-render", action="store_true",
                        help="Disable Stage 3's contact-scrape rendering, which is ON by default "
                             "(fallback mode — renders only JS/empty team pages). Use this to run "
                             "Stage 3 static-only.")
    parser.add_argument("--vendor-backlinks", action="store_true",
                        help="Before Stage 4, scrape the CMS vendors' own customer / testimonial "
                             "pages and back-track any of our firms found there into cms_results "
                             "(fills Unknown firms). Pure scraping, no API cost. Add "
                             "--include-vendor-names to also apply weaker name-only matches.")
    parser.add_argument("--include-vendor-names", action="store_true",
                        help="With --vendor-backlinks, also apply name-only vendor matches "
                             "(Medium confidence), not just domain matches.")
    parser.add_argument("--to-sheets", action="store_true",
                        help="After Stage 4, also push final_output.csv / bonus.csv to a "
                             "Google Sheet (requires the optional sheets extra plus "
                             "GOOGLE_SHEET_ID + GOOGLE_SHEETS_CREDENTIALS_FILE; see "
                             "requirements-sheets.txt). Skipped under --dry-run.")
    args = parser.parse_args()

    dry = ["--dry-run"] if args.dry_run else []
    render_pass = (["--render", args.render] if args.render != "off" else [])

    # Mark the start so the end-of-run total only sums THIS run's API calls
    # (api_usage.csv accumulates across runs). Stages run as subprocesses, so we
    # read their logged rows back from the CSV rather than from memory.
    run_started = cost_tracker.now_iso()

    # process_limit = how many firms Stage 2/3 should be allowed to enrich.
    process_limit = args.limit

    if args.no_vet:
        # Legacy behavior: --limit is a raw-firm cap; no headcount gate.
        run_stage("01_source_firms.py", ["--limit", str(args.limit)] + dry)
    elif args.dry_run:
        # Wiring check only — one source + vet pass at --limit, no source-until loop
        # (so a dry run stays fast and doesn't churn the whole sourced backlog).
        run_stage("01_source_firms.py", ["--limit", str(args.limit)] + dry)
        run_stage("01b_vet_headcount.py", ["--limit", str(args.limit)] + dry + render_pass)
    else:
        # Source + vet in a loop until --limit firms PASS the 5-100 gate. Each round
        # first vets any already-sourced-but-unvetted firms (cheap, reuses firms on
        # hand), then sources a fresh batch only if still short. Stops when the
        # target is met, sourcing finds nothing new, or --max-source-rounds is hit.
        target = args.limit
        print(f"\n{'=' * 60}\nSourcing + vetting until {target} firms pass the 5-100 attorney gate"
              f"\n{'=' * 60}", flush=True)
        rounds = 0
        while True:
            unvetted = _row_count(FIRMS_RAW_PATH) - _row_count(FIRMS_VETTED_PATH)
            if unvetted > 0:
                run_stage("01b_vet_headcount.py", ["--limit", str(unvetted)] + render_pass)
            passing, _ = _vet_counts(FIRMS_VETTED_PATH)
            print(f"\n  -> {passing}/{target} firms confirmed in the 5-100 range so far "
                  f"(round {rounds}).", flush=True)
            if passing >= target:
                break
            rounds += 1
            if rounds > args.max_source_rounds:
                print(f"\n[!] Hit --max-source-rounds ({args.max_source_rounds}); "
                      f"stopping at {passing}/{target} in range.", flush=True)
                break
            before = _row_count(FIRMS_RAW_PATH)
            run_stage("01_source_firms.py", ["--limit", str(args.source_batch)])
            if _row_count(FIRMS_RAW_PATH) == before:
                print(f"\n[!] Sourcing exhausted — no new firms found. Stopping at "
                      f"{passing}/{target} in range.\n    Widen sourcing coverage (see README "
                      f"'Tuning firm sourcing coverage') to reach a higher target.", flush=True)
                break
        # Enrich every passing + unknown firm not already done (resumable); that
        # count is a safe upper bound on how many Stage 2/3 still need to process.
        # Unknowns are unreachable firms we won't reject on a failed fetch — Stage 2
        # gives them a real estimate, then Stage 4 makes the final 5-100 call.
        _, process_limit = _vet_counts(FIRMS_VETTED_PATH)

    stage2_args = ["--limit", str(process_limit)] + dry
    if args.skip_exa:
        stage2_args.append("--skip-exa")
    # Render is a Stage-2-only concern and is ignored under --dry-run (Stage 2
    # never renders mock evidence), but pass it through so a real run can opt in.
    if args.render != "off":
        stage2_args += ["--render", args.render]
    run_stage("02_detect_cms.py", stage2_args)

    if not args.skip_contacts:
        # Stage 3 renders JS-rendered team/contact pages by default (fallback);
        # --no-render forces static-only. Skipped under --dry-run (no real fetches).
        stage3_args = ["--limit", str(process_limit)] + dry
        if args.no_render and not args.dry_run:
            stage3_args += ["--render", "off"]
        run_stage("03_enrich_contacts.py", stage3_args)

    # Optional reverse-lookup: patch cms_results from CMS vendor customer pages
    # BEFORE Stage 4 so any newly-detected firms flow into final_output. Pure
    # scraping; skipped under --dry-run (it hits live vendor sites).
    if args.vendor_backlinks and not args.dry_run:
        stage6_args = ["--apply"]
        if args.include_vendor_names:
            stage6_args.append("--include-names")
        run_stage("06_vendor_backlinks.py", stage6_args)

    run_stage("04_score_and_export.py", [])

    # Optional Stage 5: mirror the CSVs to a Google Sheet. CSVs are already
    # written above, so this is purely additive. Skipped under --dry-run since
    # there's no real deliverable to publish.
    if args.to_sheets and not args.dry_run:
        run_stage("05_export_to_sheets.py", [])

    print(f"\n{'=' * 60}\nPipeline complete. See data/final_output.csv and data/bonus.csv\n{'=' * 60}")

    # Total estimated API spend across every stage of this run.
    print()
    print(cost_tracker.summary_since(run_started, "Full pipeline run"))
    print("Per-stage / per-date breakdown: python scripts/cost_report.py")


if __name__ == "__main__":
    main()
