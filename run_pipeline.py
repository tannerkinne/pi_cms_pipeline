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
import subprocess
import sys
import os

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
PIPELINE_DIR = os.path.join(ROOT_DIR, "pipeline")
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


def main():
    parser = argparse.ArgumentParser(description="Run the full plaintiff-PI CMS-detection pipeline.")
    parser.add_argument("--limit", type=int, default=10,
                        help="Max firms to process end to end (default 10; raise to ~200 for a full run).")
    parser.add_argument("--dry-run", action="store_true",
                        help="No real API calls anywhere; validates wiring with mock data.")
    parser.add_argument("--skip-exa", action="store_true",
                        help="Skip Exa search in Stage 2 to cut cost.")
    parser.add_argument("--skip-contacts", action="store_true",
                        help="Skip Stage 3 entirely (decision-maker lookup).")
    parser.add_argument("--render", choices=["off", "fallback", "always"], default="off",
                        help="Stage 2 headless-browser fallback for JS-rendered sites "
                             "(requires the optional render extra; see requirements-render.txt).")
    parser.add_argument("--no-render", action="store_true",
                        help="Disable Stage 3's contact-scrape rendering, which is ON by default "
                             "(fallback mode — renders only JS/empty team pages). Use this to run "
                             "Stage 3 static-only.")
    parser.add_argument("--to-sheets", action="store_true",
                        help="After Stage 4, also push final_output.csv / bonus.csv to a "
                             "Google Sheet (requires the optional sheets extra plus "
                             "GOOGLE_SHEET_ID + GOOGLE_SHEETS_CREDENTIALS_FILE; see "
                             "requirements-sheets.txt). Skipped under --dry-run.")
    args = parser.parse_args()

    common = ["--limit", str(args.limit)]
    if args.dry_run:
        common.append("--dry-run")

    # Mark the start so the end-of-run total only sums THIS run's API calls
    # (api_usage.csv accumulates across runs). Stages run as subprocesses, so we
    # read their logged rows back from the CSV rather than from memory.
    run_started = cost_tracker.now_iso()

    run_stage("01_source_firms.py", common)

    stage2_args = list(common)
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
        stage3_args = list(common)
        if args.no_render and not args.dry_run:
            stage3_args += ["--render", "off"]
        run_stage("03_enrich_contacts.py", stage3_args)

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
