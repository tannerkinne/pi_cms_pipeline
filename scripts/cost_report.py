#!/usr/bin/env python3
"""
Standalone cost report. Reads data/api_usage.csv (written by the pipeline's
api_wrappers/cost_tracker.py) and prints a full breakdown of API calls and
estimated spend by stage, by service, and by date — so cost can be reviewed any
time without re-running the pipeline.

Usage:
    python scripts/cost_report.py
    python scripts/cost_report.py --since 2026-06-24      # ISO date or timestamp
"""
import argparse
import csv
import os
import sys
from collections import defaultdict

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USAGE_PATH = os.path.join(ROOT_DIR, "data", "api_usage.csv")

STAGE_NAMES = {
    "01": "Source firms (Google Places)",
    "02": "Detect CMS (fingerprint + Exa + Claude)",
    "03": "Enrich contacts (scrape + Apollo)",
    "04": "Score & export",
}


def _load_rows(since):
    if not os.path.exists(USAGE_PATH):
        print(f"No usage data found at {USAGE_PATH}. Run the pipeline first.")
        return []
    rows = []
    with open(USAGE_PATH, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if since and r.get("timestamp", "") < since:
                continue
            rows.append(r)
    return rows


def _cost(r):
    try:
        return float(r.get("estimated_cost_usd") or 0)
    except (TypeError, ValueError):
        return 0.0


def _int(r, key):
    try:
        return int(float(r.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def _print_group(title, key_fn, rows, label_map=None):
    agg = defaultdict(lambda: {"calls": 0, "ok": 0, "cost": 0.0})
    for r in rows:
        k = key_fn(r)
        agg[k]["calls"] += 1
        if str(r.get("success", "")).lower() == "true":
            agg[k]["ok"] += 1
        agg[k]["cost"] += _cost(r)
    print(f"\n=== By {title} ===")
    print(f"  {title:<42} {'calls':>6} {'ok':>6} {'est_cost':>12}")
    print(f"  {'-' * 42} {'-' * 6} {'-' * 6} {'-' * 12}")
    for k in sorted(agg):
        label = (label_map or {}).get(k, k) if label_map else k
        disp = f"{k} {label}" if label_map and label != k else label
        a = agg[k]
        print(f"  {disp:<42} {a['calls']:>6} {a['ok']:>6} {'$' + format(a['cost'], '.4f'):>12}")


def main():
    parser = argparse.ArgumentParser(description="Report API usage and estimated cost.")
    parser.add_argument("--since", default=None,
                        help="Only include rows with timestamp >= this ISO date/timestamp.")
    args = parser.parse_args()

    rows = _load_rows(args.since)
    if not rows:
        return

    total_cost = sum(_cost(r) for r in rows)
    total_calls = len(rows)

    print("=" * 64)
    print("API COST REPORT")
    print(f"  source: {USAGE_PATH}")
    if args.since:
        print(f"  since:  {args.since}")
    print(f"  total calls: {total_calls}   total estimated cost: ${total_cost:.4f}")
    print("=" * 64)

    _print_group("stage", lambda r: r.get("stage", "?") or "?", rows, STAGE_NAMES)
    _print_group("service", lambda r: r.get("service", "?") or "?", rows)
    _print_group("date", lambda r: (r.get("timestamp", "") or "")[:10] or "?", rows)

    # Claude token rollup — the main variable-cost driver worth surfacing.
    claude = [r for r in rows if r.get("service") == "claude"]
    if claude:
        ti = sum(_int(r, "tokens_input") for r in claude)
        to = sum(_int(r, "tokens_output") for r in claude)
        tcr = sum(_int(r, "tokens_cache_read") for r in claude)
        tcw = sum(_int(r, "tokens_cache_write") for r in claude)
        print("\n=== Claude token totals ===")
        print(f"  input={ti:,}  output={to:,}  cache_read={tcr:,}  cache_write={tcw:,}")

    print()


if __name__ == "__main__":
    main()
