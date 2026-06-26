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
from config import ATTORNEY_MIN_COUNT
from utils import (DATA_DIR, read_csv_rows, append_csv_row, write_csv_rows,
                   already_processed_keys, CallCounter)

# site_errors substrings that mean the previous fetch was BLOCKED rather than the
# firm genuinely having no such page — i.e. a re-fetch with the new browser UA +
# render could change the outcome. (A plain 404 on a subpath is normal and absent.)
_BLOCK_ERROR_HINTS = ("403", "Timeout", "timeout", "SSLError", "ConnectionError", "TooManyRedirects")

RAW_INPUT_PATH = os.path.join(DATA_DIR, "firms_raw.csv")
VETTED_INPUT_PATH = os.path.join(DATA_DIR, "firms_vetted.csv")
OUTPUT_PATH = os.path.join(DATA_DIR, "cms_results.csv")


def _load_input_firms():
    """Prefer the headcount-vetted firm list when it exists: process ONLY firms
    that passed the Stage 1b 5-100 attorney gate, so we never spend Exa + full
    classification on firms that are too small/large to use. Falls back to the
    raw sourced list when no vet has been run yet (backward compatible)."""
    vetted = read_csv_rows(VETTED_INPUT_PATH)
    if vetted:
        # Process firms that passed the 5-100 gate PLUS 'unknown' firms — ones the
        # cheap vet couldn't size because their site blocked us / timed out. We
        # don't reject those on a failed fetch; Stage 2 gives them the full
        # treatment (browser UA, render fallback, Exa web search) to get a real
        # count, and Stage 4's authoritative est_attorneys filter makes the final
        # call. Confidently-out-of-range firms (fail_low / fail_high) are excluded.
        keep = {"pass", "unknown"}
        proceed = [r for r in vetted if (r.get("vet_status") or "").strip() in keep]
        npass = sum(1 for r in proceed if (r.get("vet_status") or "").strip() == "pass")
        nunknown = len(proceed) - npass
        excluded = len(vetted) - len(proceed)
        print(f"Stage 2: processing {len(proceed)} vetted firms "
              f"({npass} in-range + {nunknown} unknown/unreachable); "
              f"{excluded} confidently out-of-range firms excluded.")
        return proceed
    return read_csv_rows(RAW_INPUT_PATH)

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
    parser.add_argument("--rerender-unknowns", action="store_true",
                        help="Targeted quality pass: reprocess ONLY firms already classified "
                             "'Unknown' in cms_results.csv, with rendering on, overwriting their "
                             "rows. Spends render time only where static detection found no CMS — "
                             "the cheap way to chase JS-injected portal links. Implies --render "
                             "always unless --render is given explicitly.")
    parser.add_argument("--only", default="",
                        help="Comma-separated domains to (re)detect — overwrites just those rows "
                             "and ignores --limit. Useful to re-run a specific batch (e.g. firms "
                             "detected while an API was down).")
    parser.add_argument("--refetch-unreachable", action="store_true",
                        help="Backfill: reprocess firms whose PREVIOUS fetch failed (HTTP 403 / "
                             "timeout / SSL) and so came back with est_attorneys <5 — these are "
                             "false zeros from a blocked fetch, not real solo firms (e.g. Cellino). "
                             "Re-runs them with the new browser UA + render fallback, overwriting "
                             "their rows. Respects --limit (batch with a high value). Add "
                             "--include-partial to also retry firms that fetched some pages but hit "
                             "a block error on others.")
    parser.add_argument("--include-partial", action="store_true",
                        help="With --refetch-unreachable, also retry est<5 firms that DID fetch some "
                             "pages but logged a 403/timeout/SSL error on others (partial undercounts), "
                             "not just the firms that fetched zero pages.")
    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    cost_tracker.set_context(stage="02")
    # Backfill mode works over the full sourced universe (firms_raw), independent
    # of the vet filter, so it can recover any firm previously dropped on a bad fetch.
    firms = read_csv_rows(RAW_INPUT_PATH) if args.refetch_unreachable else _load_input_firms()
    if not firms:
        print(f"No input firms found — run Stage 1 (and Stage 1b vet) first.")
        return

    counter = CallCounter()
    render_mode = args.render
    only = {d.strip() for d in args.only.split(",") if d.strip()}

    if only:
        # Targeted re-detect of specific domains, overwriting their rows (upsert,
        # same drop-then-reappend pattern as --rerender-unknowns / Stage 3 --force).
        existing = read_csv_rows(OUTPUT_PATH)
        todo = [f for f in firms if f["domain"] in only]
        todo_domains = {f["domain"] for f in todo}
        retained = [r for r in existing if r["domain"] not in todo_domains]
        write_csv_rows(OUTPUT_PATH, retained, FIELDNAMES, mode="w")
        print(f"Stage 2 [only]: re-detecting {len(todo)} firms with render='{render_mode}' "
              f"(overwriting their rows).")
    elif args.rerender_unknowns:
        # Targeted quality pass: reprocess only the firms currently classified
        # 'Unknown' (static detection found no CMS), with rendering on. Drop their
        # rows up front so the fresh rows replace them (no duplicates), mirroring
        # Stage 3's --force upsert. Forces render on if the caller left it 'off'.
        if render_mode == "off":
            render_mode = "always"
        existing = read_csv_rows(OUTPUT_PATH)
        unknown_domains = {r["domain"] for r in existing
                           if (r.get("cms_detected") or "").strip() == "Unknown"}
        todo = [f for f in firms if f["domain"] in unknown_domains][: args.limit]
        todo_domains = {f["domain"] for f in todo}
        retained = [r for r in existing if r["domain"] not in todo_domains]
        write_csv_rows(OUTPUT_PATH, retained, FIELDNAMES, mode="w")
        print(f"Stage 2 [rerender-unknowns]: reprocessing {len(todo)} Unknown firms "
              f"with render='{render_mode}' (overwriting their rows).")
    elif args.refetch_unreachable:
        # Backfill the false zeros: firms whose previous fetch was blocked/timed
        # out (so est_attorneys came back <5) get re-run with the new browser UA +
        # render, overwriting their rows. Render on by default here — it's the
        # whole point (it's what gets past JS-challenge WAFs like Cellino's).
        if render_mode == "off":
            render_mode = "fallback"
        existing = read_csv_rows(OUTPUT_PATH)

        def _was_blocked(r):
            try:
                n = int(float(r.get("est_attorneys", "")))
            except (TypeError, ValueError):
                n = 0
            if n >= ATTORNEY_MIN_COUNT:
                return False  # already counted as mid-size+; nothing to recover
            pages = (r.get("site_pages_checked", "") or "").strip()
            errs = r.get("site_errors", "") or ""
            if not pages:
                return True  # zero pages fetched — the clearest false zero
            if args.include_partial and any(h in errs for h in _BLOCK_ERROR_HINTS):
                return True  # fetched some pages but a block error undercut the count
            return False

        target_domains = {r["domain"] for r in existing if _was_blocked(r)}
        todo = [f for f in firms if f["domain"] in target_domains][: args.limit]
        todo_domains = {f["domain"] for f in todo}
        retained = [r for r in existing if r["domain"] not in todo_domains]
        write_csv_rows(OUTPUT_PATH, retained, FIELDNAMES, mode="w")
        scope = "zero-page + partial-block" if args.include_partial else "zero-page"
        print(f"Stage 2 [refetch-unreachable]: re-running {len(todo)} of "
              f"{len(target_domains)} blocked firms ({scope}) with render='{render_mode}' "
              f"(overwriting their rows). Raise --limit to do more per batch.")
    else:
        done_domains = already_processed_keys(OUTPUT_PATH, "domain")
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
            fingerprint = site_fingerprint.fingerprint_site(domain, render=render_mode)
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
