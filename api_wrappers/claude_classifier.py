"""
Takes the assembled evidence (site fingerprint hits, careers page job titles,
Exa search snippets) for one firm and asks Claude to produce the final
structured call: cms_detected, cms_confidence, cms_evidence, hiring_signal.

Uses the cheapest current model (Haiku) by default — see config.py.
Each call is a short, bounded-size prompt with a small JSON response, so
even a 200-firm run should be inexpensive. If Haiku's calls look inconsistent
on ambiguous cases, swap CLAUDE_MODEL in config.py to claude-sonnet-4-6.
"""
import os
import json
import re

from config import CLAUDE_MODEL
from . import cost_tracker

VALID_CMS = ["CloudLex", "Filevine", "Litify", "SmartAdvocate", "CASEpeer", "Unknown"]
VALID_CONFIDENCE = ["High", "Medium", "Low"]
VALID_PI_FOCUS = ["Yes", "Mixed", "No"]
VALID_TRIAL_FOCUSED = ["Yes", "No", "Unknown"]

SYSTEM_PROMPT = """You are tagging law firms with the case-management software (CMS) they use, \
based on evidence gathered from their website and web search snippets. \
You also judge whether the firm is plaintiff-side personal injury, using a homepage text snippet.

Valid cms_detected values: CloudLex, Filevine, Litify, SmartAdvocate, CASEpeer, Unknown.
Valid plaintiff_pi_focus values: Yes, Mixed, No.

Rules:
- A "portal_link" signal is the STRONGEST evidence: it means the firm's own "Client Login" link \
resolves to that CMS vendor's hosted portal domain (e.g. firmname.cloudlex.com, app.filevineapp.com, \
casepeer.com). A portal_link signal alone justifies High confidence.
- A CMS name found directly in the firm's own site source ("primary" signal — branding, embedded \
widgets) is strong evidence (Medium/High).
- A CMS name mentioned in a job title or careers page (e.g. "experience with Filevine required") is strong evidence.
- A CMS name only appearing in a generic/unrelated web search snippet is weak evidence (Low).
- Litify is built on Salesforce, so a Salesforce app marker (a "secondary" signal) MIGHT indicate Litify — \
but ONLY if corroborated. A secondary signal ALONE — without the word "litify" itself in the firm's own site \
source, a portal_link resolving to a Litify host, a careers/job mention of Litify, or a web snippet naming \
Litify — is NOT sufficient to name the CMS: return "Unknown" in that case, never "Litify". Salesforce markers \
appear on many firm sites that do not run Litify (marketing form embeds, Salesforce-branded icon fonts), so \
never name a CMS off a secondary signal alone.
- Evidence-type precedence, strongest to weakest: portal_link > primary site source / careers page > \
generic web mention > Salesforce-only secondary signal.
- If evidence is contradictory (e.g. two different CMS names both mentioned), prefer the one with the \
stronger evidence type, and lower the confidence.
- If there is no real evidence for any CMS, return "Unknown" with confidence "Low" — never guess.
- hiring_signal is "Yes" if there's evidence of an open case manager / intake / paralegal / \
records role at this firm, else "No".
- plaintiff_pi_focus: "Yes" if the homepage clearly markets to injury victims/plaintiffs (e.g. \
contingency-fee language, practice areas like car accidents, slip and fall, medical malpractice \
as plaintiff representation). "No" if it's clearly insurance defense, general practice with no PI \
emphasis, or a non-PI specialty. "Mixed" if it does both plaintiff PI and other work, or if the \
snippet is too thin to tell confidently — do not guess "Yes" from a thin snippet.
- trial_focused: "Yes" if the firm emphasizes trial experience, courtroom wins, jury verdicts, \
"we go to trial", or "we don't just settle". "No" if language emphasizes fast settlements, \
no-risk settlements, or avoids trial language. "Unknown" if there isn't enough evidence to tell.
- est_attorneys: Your best integer estimate of how many attorneys work at this firm, based on any \
evidence available (attorney listing pages, "our team of X attorneys", job postings count, firm \
size mentions in snippets). If you truly cannot estimate, return 0. Do not guess wildly — if the \
only evidence is a 2-person About page with 2 named attorneys, return 2.

Respond ONLY with a single JSON object, no markdown fences, no preamble, matching this exact shape:
{
  "cms_detected": "<one of the valid values>",
  "cms_confidence": "<High|Medium|Low>",
  "cms_evidence": "<one or two short sentences citing what was found, e.g. 'Filevine listed in job posting for Case Manager role.'>",
  "hiring_signal": "<Yes|No>",
  "hiring_signal_evidence": "<short note or empty string>",
  "plaintiff_pi_focus": "<Yes|Mixed|No>",
  "plaintiff_pi_focus_note": "<short note, or empty string if homepage text wasn't available>",
  "trial_focused": "<Yes|No|Unknown>",
  "est_attorneys": <integer, 0 if unknown>
}"""


def _track_usage(response, endpoint: str = "messages.create"):
    """Log this Claude call's token usage to the cost tracker. Never raises."""
    try:
        u = getattr(response, "usage", None)
        if u is None:
            return
        cost_tracker.record(
            "claude", endpoint, success=True, model=CLAUDE_MODEL,
            tokens_input=getattr(u, "input_tokens", 0) or 0,
            tokens_output=getattr(u, "output_tokens", 0) or 0,
            tokens_cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            tokens_cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
    except Exception:
        pass  # tracking must never break classification


def _call_claude(evidence_text: str, max_retries: int = 2) -> dict:
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set.")

    client = anthropic.Anthropic(api_key=api_key)

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=512,
                temperature=0,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": evidence_text}],
            )
            _track_usage(response)
            raw_text = "".join(
                block.text for block in response.content if block.type == "text"
            ).strip()
            # Defensive cleanup in case the model wraps in a code fence anyway.
            raw_text = re.sub(r"^```(json)?|```$", "", raw_text.strip(), flags=re.MULTILINE).strip()
            parsed = json.loads(raw_text)
            return _validate(parsed)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            last_error = e
            continue
    # Fall through: never crash the batch over one firm's parse failure.
    return {
        "cms_detected": "Unknown",
        "cms_confidence": "Low",
        "cms_evidence": f"Classification failed after retries ({last_error}); needs manual check.",
        "hiring_signal": "No",
        "hiring_signal_evidence": "",
        "plaintiff_pi_focus": "Mixed",
        "plaintiff_pi_focus_note": "Classification failed; needs manual check.",
        "trial_focused": "Unknown",
        "est_attorneys": 0,
    }


def _validate(parsed: dict) -> dict:
    cms = parsed.get("cms_detected", "Unknown")
    if cms not in VALID_CMS:
        cms = "Unknown"
    confidence = parsed.get("cms_confidence", "Low")
    if confidence not in VALID_CONFIDENCE:
        confidence = "Low"
    pi_focus = parsed.get("plaintiff_pi_focus", "Mixed")
    if pi_focus not in VALID_PI_FOCUS:
        pi_focus = "Mixed"
    trial_focused = parsed.get("trial_focused", "Unknown")
    if trial_focused not in VALID_TRIAL_FOCUSED:
        trial_focused = "Unknown"
    try:
        est_attorneys = int(parsed.get("est_attorneys", 0))
    except (TypeError, ValueError):
        est_attorneys = 0
    return {
        "cms_detected": cms,
        "cms_confidence": confidence,
        "cms_evidence": str(parsed.get("cms_evidence", ""))[:500],
        "hiring_signal": "Yes" if str(parsed.get("hiring_signal", "No")).lower().startswith("y") else "No",
        "hiring_signal_evidence": str(parsed.get("hiring_signal_evidence", ""))[:300],
        "plaintiff_pi_focus": pi_focus,
        "plaintiff_pi_focus_note": str(parsed.get("plaintiff_pi_focus_note", ""))[:300],
        "trial_focused": trial_focused,
        "est_attorneys": est_attorneys,
    }


def _suppress_secondary_only(result: dict, fingerprint_result: dict,
                             careers_job_titles: list, exa_evidence: list) -> dict:
    """Deterministic backstop for the prompt rule above: if the model named a CMS
    whose ONLY site-fingerprint signal is "secondary" (Salesforce markers, which
    also fire on Web-to-Lead forms and the .fa-salesforce icon class) AND nothing
    else corroborates that CMS, downgrade it to Unknown.

    A named CMS at Low confidence pollutes the target list worse than an honest
    Unknown, and we don't want correctness to depend on model variance. Corroboration
    = the CMS name appearing in a careers job title or an Exa snippet. If the chosen
    CMS has a "primary"/"portal_link" hit, or no fingerprint hit at all (the model
    inferred it from other evidence), this is a no-op.
    """
    cms = result.get("cms_detected", "Unknown")
    if cms == "Unknown":
        return result
    hits = (fingerprint_result or {}).get("hits", {}) or {}
    kinds = set(hits.get(cms, []))
    # Only act when the CMS's sole fingerprint evidence is a secondary signal.
    if kinds != {"secondary"}:
        return result
    needle = cms.lower()
    corroborated = any(needle in (t or "").lower() for t in (careers_job_titles or []))
    if not corroborated:
        for ev in (exa_evidence or []):
            blob = f"{ev.get('title', '')} {ev.get('text', '')}".lower()
            if needle in blob:
                corroborated = True
                break
    if corroborated:
        return result
    result = dict(result)
    result["cms_detected"] = "Unknown"
    result["cms_confidence"] = "Low"
    result["cms_evidence"] = (
        f"Suppressed {cms}: the only evidence was an indirect Salesforce/secondary "
        f"marker (no {cms} branding, portal link, careers mention, or web corroboration). "
        f"Salesforce markers also appear on non-{cms} sites, so treated as Unknown."
    )
    return result


def classify_firm(firm_name: str, fingerprint_result: dict, careers_job_titles: list,
                   exa_evidence: list) -> dict:
    """
    Assembles a compact evidence bundle as text and asks Claude to classify.
    Keeps the prompt small/cheap by truncating list lengths up front rather
    than relying on the model to ignore excess context.
    """
    lines = [f"FIRM: {firm_name}", ""]

    lines.append("SITE FINGERPRINT HITS (from firm's own website source):")
    if fingerprint_result.get("hits"):
        for cms, kinds in fingerprint_result["hits"].items():
            lines.append(f"  - {cms}: {', '.join(kinds)} signal found on {len(fingerprint_result.get('pages_checked', []))} page(s)")
    else:
        lines.append("  - none found")
    lines.append("")

    lines.append("HOMEPAGE TEXT SNIPPET (for plaintiff-PI-focus judgment):")
    snippet = fingerprint_result.get("homepage_text_snippet", "")
    lines.append(f"  {snippet if snippet else '(homepage unreachable or empty)'}")
    lines.append("")

    lines.append("CAREERS PAGE JOB TITLES (from /careers page scrape):")
    if careers_job_titles:
        for title in careers_job_titles[:10]:
            lines.append(f"  - {title}")
    else:
        lines.append("  - none found / careers page not available")
    lines.append("")

    lines.append("ATTORNEY NAMES EXTRACTED FROM LISTING PAGES (parsed from JSON-LD + honorific markers; count distinct names for est_attorneys):")
    attorney_names = fingerprint_result.get("attorney_names", [])
    if attorney_names:
        for name in attorney_names[:40]:
            lines.append(f"  - {name}")
    else:
        lines.append("  - (no names cleanly extracted; fall back to the listing text below)")
    lines.append("")

    lines.append("ATTORNEY LISTING PAGE TEXT (count named attorneys here to estimate est_attorneys):")
    attorney_text = fingerprint_result.get("attorney_page_text", "")
    if attorney_text:
        lines.append(f"  {attorney_text}")
    else:
        lines.append("  (no attorney listing pages reachable)")
    lines.append("")

    lines.append("WEB SEARCH SNIPPETS (Exa):")
    if exa_evidence:
        for ev in exa_evidence[:4]:
            lines.append(f"  - [{ev.get('title', '')}]({ev.get('url', '')}): {ev.get('text', '')[:400]}")
    else:
        lines.append("  - none found / not available")

    evidence_text = "\n".join(lines)
    result = _call_claude(evidence_text)
    return _suppress_secondary_only(result, fingerprint_result, careers_job_titles, exa_evidence)


# ---------------------------------------------------------------------------
# Lightweight attorney-headcount estimate for the Stage 1b vet gate.
#
# This runs BEFORE the paid Exa search + full classification, so we only spend
# on firms inside the target size band (config.ATTORNEY_MIN/MAX_COUNT). It uses
# the same cheap CLAUDE_MODEL but a tiny, single-purpose prompt and a small
# response, so it costs a fraction of a full classification.
# ---------------------------------------------------------------------------
HEADCOUNT_SYSTEM_PROMPT = """You estimate how many attorneys (lawyers) work at a US law firm, using \
ONLY the website evidence provided. Count distinct attorneys/lawyers — never paralegals, intake staff, \
or office locations.

Guidance:
- The most reliable signal is the list of attorney names parsed from the firm's team/attorneys page. \
If you are given N distinct attorney names, the estimate is normally N.
- An explicit phrase in the page text ("our team of 12 attorneys", "30+ lawyers") is strong — use the stated number.
- If the only evidence is a small About page naming 1-2 attorneys, return that small number.
- If there is genuinely no evidence of attorney count, return 0 (unknown). Do NOT guess a mid-range number.

Respond ONLY with a single JSON object, no markdown fences, no preamble, exactly:
{"est_attorneys": <integer, 0 if unknown>, "basis": "<short phrase citing the evidence used>"}"""


def _headcount_evidence_text(firm_name: str, fingerprint_result: dict) -> str:
    lines = [f"FIRM: {firm_name}", ""]

    lines.append("ATTORNEY NAMES PARSED FROM TEAM/ATTORNEYS PAGES (count distinct names):")
    attorney_names = fingerprint_result.get("attorney_names", [])
    if attorney_names:
        for name in attorney_names[:60]:
            lines.append(f"  - {name}")
    else:
        lines.append("  - (no names cleanly extracted; use the listing text below)")
    lines.append("")

    lines.append("ATTORNEY LISTING PAGE TEXT:")
    attorney_text = fingerprint_result.get("attorney_page_text", "")
    lines.append(f"  {attorney_text}" if attorney_text else "  (no attorney listing pages reachable)")
    lines.append("")

    lines.append("HOMEPAGE TEXT SNIPPET (may mention firm size):")
    snippet = fingerprint_result.get("homepage_text_snippet", "")
    lines.append(f"  {snippet}" if snippet else "  (homepage unreachable or empty)")

    return "\n".join(lines)


def estimate_headcount(firm_name: str, fingerprint_result: dict, max_retries: int = 2) -> dict:
    """Cheap, single-purpose attorney-headcount estimate. Never raises; on any
    failure returns {"est_attorneys": 0, "basis": "estimate failed"} so the vet
    gate can treat it as out of range rather than crash the batch."""
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set.")

    evidence_text = _headcount_evidence_text(firm_name, fingerprint_result)
    client = anthropic.Anthropic(api_key=api_key)

    for _ in range(max_retries + 1):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=128,
                temperature=0,
                system=HEADCOUNT_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": evidence_text}],
            )
            _track_usage(response, endpoint="headcount.estimate")
            raw = "".join(b.text for b in response.content if b.type == "text").strip()
            raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
            parsed = json.loads(raw)
            return {
                "est_attorneys": int(parsed.get("est_attorneys", 0)),
                "basis": str(parsed.get("basis", ""))[:200],
            }
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue
    return {"est_attorneys": 0, "basis": "estimate failed after retries; treated as unknown"}
