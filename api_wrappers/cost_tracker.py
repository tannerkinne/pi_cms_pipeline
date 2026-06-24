"""
Unified API cost / usage tracking for the whole pipeline.

Every API wrapper (google_places_client, exa_client, claude_classifier,
apollo_client) calls cost_tracker.record(...) after each API call. Each call is
appended as one row to data/api_usage.csv with shared columns plus
service-specific token / credit / result columns and an estimated USD cost
derived from config.COST_RATES.

HARD REQUIREMENT: this module must never crash the pipeline. Every public
function wraps its work in try/except and logs failures to
data/tracker_errors.log instead of raising. If tracking breaks, the pipeline
keeps running and only loses telemetry.

Stage / domain context: stages call set_context(stage=..., domain=...) so the
wrappers don't have to thread that plumbing through every signature; record()
falls back to the current context when stage/domain aren't passed explicitly.
"""
import csv
import os
import traceback
from collections import defaultdict
from datetime import datetime, timezone

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(_THIS_DIR), "data")
USAGE_PATH = os.path.join(DATA_DIR, "api_usage.csv")
ERROR_LOG_PATH = os.path.join(DATA_DIR, "tracker_errors.log")

FIELDNAMES = [
    "timestamp", "stage", "service", "domain", "endpoint", "success", "error_message",
    "tokens_input", "tokens_output", "tokens_cache_read", "tokens_cache_write",
    "credits_consumed", "results_returned", "estimated_cost_usd",
]

# Current pipeline context, set by the stage scripts. Wrappers read this when
# stage/domain aren't passed to record() directly.
_context = {"stage": "", "domain": ""}

# In-memory record of rows logged by THIS process, so a stage can print its own
# end-of-run cost summary without re-reading the (shared, cross-run) CSV.
_session_rows = []


def set_context(stage=None, domain=None):
    """Set the current stage ('01'..'04') and/or domain for subsequent records."""
    try:
        if stage is not None:
            _context["stage"] = stage
        if domain is not None:
            _context["domain"] = domain
    except Exception as e:  # pragma: no cover - defensive only
        _log_error(e)


def _log_error(exc):
    """Append a tracker failure to data/tracker_errors.log; never raise."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {exc!r}\n")
            f.write(traceback.format_exc())
            f.write("\n")
    except Exception:
        pass  # last resort: swallow — tracking must never break the pipeline


def _rates():
    try:
        from config import COST_RATES
        return COST_RATES
    except Exception as e:
        _log_error(e)
        return {}


def estimate_cost(service, success=True, model=None,
                  tokens_input=0, tokens_output=0, tokens_cache_read=0,
                  tokens_cache_write=0, credits_consumed=0, results_returned=0):
    """Best-effort USD estimate for one API call. Returns 0.0 on any problem."""
    try:
        rates = _rates()
        svc = rates.get(service, {})
        if service == "claude":
            m = svc.get(model) or {}
            return round(
                (tokens_input / 1e6) * m.get("input", 0.0)
                + (tokens_output / 1e6) * m.get("output", 0.0)
                + (tokens_cache_read / 1e6) * m.get("cache_read", 0.0)
                + (tokens_cache_write / 1e6) * m.get("cache_write", 0.0),
                6,
            )
        if service == "apollo":
            # Credits are consumed regardless of nominal success flag, but a
            # failed call typically reports 0 credits anyway.
            return round((credits_consumed or 0) * svc.get("per_credit", 0.0), 6)
        # Flat per-call services: only bill successful calls.
        if not success:
            return 0.0
        if service == "exa":
            return round(svc.get("per_search", 0.0), 6)
        if service == "google_places":
            return round(svc.get("per_request", 0.0), 6)
        return 0.0
    except Exception as e:
        _log_error(e)
        return 0.0


def record(service, endpoint, success=True, domain=None, stage=None,
           error_message="", model=None,
           tokens_input=0, tokens_output=0, tokens_cache_read=0, tokens_cache_write=0,
           credits_consumed=None, results_returned=None):
    """Append one usage row to data/api_usage.csv. Never raises."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        stage = stage if stage is not None else _context.get("stage", "")
        domain = domain if domain is not None else _context.get("domain", "")

        cost = estimate_cost(
            service, success=success, model=model,
            tokens_input=tokens_input or 0, tokens_output=tokens_output or 0,
            tokens_cache_read=tokens_cache_read or 0, tokens_cache_write=tokens_cache_write or 0,
            credits_consumed=credits_consumed or 0, results_returned=results_returned or 0,
        )

        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "service": service,
            "domain": domain,
            "endpoint": endpoint,
            "success": bool(success),
            "error_message": (error_message or "")[:300],
            "tokens_input": tokens_input or 0,
            "tokens_output": tokens_output or 0,
            "tokens_cache_read": tokens_cache_read or 0,
            "tokens_cache_write": tokens_cache_write or 0,
            # Leave service-irrelevant numeric columns blank (null) rather than 0.
            "credits_consumed": credits_consumed if credits_consumed is not None else "",
            "results_returned": results_returned if results_returned is not None else "",
            "estimated_cost_usd": cost,
        }

        write_header = (not os.path.exists(USAGE_PATH)) or os.path.getsize(USAGE_PATH) == 0
        with open(USAGE_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            if write_header:
                writer.writeheader()
            writer.writerow(row)

        _session_rows.append(row)
    except Exception as e:
        _log_error(e)


# --- Summaries ------------------------------------------------------------

def _format_summary(rows, title):
    agg = defaultdict(lambda: {"calls": 0, "cost": 0.0})
    total = 0.0
    for r in rows:
        svc = r.get("service", "?")
        agg[svc]["calls"] += 1
        try:
            c = float(r.get("estimated_cost_usd") or 0)
        except (TypeError, ValueError):
            c = 0.0
        agg[svc]["cost"] += c
        total += c
    lines = [f"--- {title} cost summary ---"]
    if not agg:
        lines.append("  (no API calls recorded)")
    for svc in sorted(agg):
        lines.append(f"  {svc:<15} calls={agg[svc]['calls']:>4}   est_cost=${agg[svc]['cost']:.4f}")
    lines.append(f"  {'TOTAL':<15} {'':<10}   est_cost=${total:.4f}")
    return "\n".join(lines)


def session_summary(title="This stage"):
    """Formatted cost summary for calls recorded in THIS process."""
    try:
        return _format_summary(_session_rows, title)
    except Exception as e:
        _log_error(e)
        return f"--- {title} cost summary unavailable (tracker error) ---"


def read_rows(since_iso=None):
    """Read api_usage.csv, optionally only rows with timestamp >= since_iso."""
    rows = []
    try:
        if not os.path.exists(USAGE_PATH):
            return rows
        with open(USAGE_PATH, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if since_iso and (r.get("timestamp", "") < since_iso):
                    continue
                rows.append(r)
    except Exception as e:
        _log_error(e)
    return rows


def summary_since(since_iso, title="Full run"):
    """Formatted cost summary read from the CSV, for rows at/after since_iso.

    Used by run_pipeline.py, which runs each stage in a subprocess and so can't
    see the in-memory _session_rows of those children.
    """
    try:
        return _format_summary(read_rows(since_iso), title)
    except Exception as e:
        _log_error(e)
        return f"--- {title} cost summary unavailable (tracker error) ---"


def now_iso():
    """UTC timestamp matching the format written to the CSV (for since filters)."""
    return datetime.now(timezone.utc).isoformat()
