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
    python run_pipeline.py --limit 150

  Cost-trimmed full run (skip Exa, the priciest per-firm signal, and rely
  on fingerprinting + Apollo job postings only):
    python run_pipeline.py --limit 150 --skip-exa

See README.md for required environment variables and per-stage cost notes.
"""
import argparse
import subprocess
import sys
import os

PIPELINE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline")


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
    parser.add_argument("--limit", type=int, default=10, help="Max firms to process end to end (default 10; raise to ~150-200 for a full run).")
    parser.add_argument("--dry-run", action="store_true", help="No real API calls anywhere; validates wiring with mock data.")
    parser.add_argument("--skip-exa", action="store_true", help="Skip Exa search in Stage 2 to cut cost.")
    parser.add_argument("--skip-apollo-jobs", action="store_true", help="Skip Apollo job-postings lookup in Stage 2 to cut cost.")
    parser.add_argument("--skip-contacts", action="store_true", help="Skip Stage 3 entirely (decision-maker lookup).")
    args = parser.parse_args()

    common = ["--limit", str(args.limit)]
    if args.dry_run:
        common.append("--dry-run")

    run_stage("01_source_firms.py", common)

    stage2_args = list(common)
    if args.skip_exa:
        stage2_args.append("--skip-exa")
    if args.skip_apollo_jobs:
        stage2_args.append("--skip-apollo-jobs")
    run_stage("02_detect_cms.py", stage2_args)

    if not args.skip_contacts:
        run_stage("03_enrich_contacts.py", common)

    run_stage("04_score_and_export.py", [])

    print(f"\n{'=' * 60}\nPipeline complete. See data/final_output.csv and data/bonus.csv\n{'=' * 60}")


if __name__ == "__main__":
    main()
