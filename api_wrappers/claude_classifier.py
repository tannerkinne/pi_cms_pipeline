"""
Takes the assembled evidence (site fingerprint hits, Apollo job postings /
technologies, Exa search snippets) for one firm and asks Claude to produce
the final structured call: cms_detected, cms_confidence, cms_evidence,
hiring_signal. This is the only step requiring judgment across noisy,
possibly-contradictory evidence, which is why it's the one step that
isn't simple rule-matching.

Uses the cheapest current model (Haiku) by default — see config.py.
Costs are small: each call is a short, bounded-size prompt with a small
JSON response, so even a 150-firm run should be inexpensive. If you find
Haiku's calls inconsistent on ambiguous cases, swap CLAUDE_MODEL in
config.py to claude-sonnet-4-6 — it'll cost more per call but should be
more reliable for borderline judgment calls.
"""
import os
import json
import re

from config import CLAUDE_MODEL

VALID_CMS = ["CloudLex", "Filevine", "Litify", "SmartAdvocate", "CASEpeer", "Unknown"]
VALID_CONFIDENCE = ["High", "Medium", "Low"]
VALID_PI_FOCUS = ["Yes", "Mixed", "No"]

SYSTEM_PROMPT = """You are tagging law firms with the case-management software (CMS) they use, \
based on evidence gathered from their website, Apollo.io company data, and web search snippets. \
You also judge whether the firm is plaintiff-side personal injury, using a homepage text snippet.

Valid cms_detected values: CloudLex, Filevine, Litify, SmartAdvocate, CASEpeer, Unknown.
Valid plaintiff_pi_focus values: Yes, Mixed, No.

Rules:
- A CMS name found directly in the firm's own site source (portal links, branding) is strong evidence.
- A CMS name mentioned in a job posting (e.g. "experience with Filevine required") is strong evidence.
- A CMS name only appearing in a generic/unrelated web search snippet is weak evidence.
- Litify is built on Salesforce — "force.com" or "salesforce" alone, without the word "litify" \
itself, is only weak/Low-confidence evidence, never High.
- If evidence is contradictory (e.g. two different CMS names both mentioned), prefer the one with \
stronger evidence type (site source / job posting > generic web mention), and lower the confidence.
- If there is no real evidence for any CMS, return "Unknown" with confidence "Low" — never guess.
- hiring_signal is "Yes" if there's evidence of an open case manager / intake / paralegal / \
records role at this firm, else "No".
- plaintiff_pi_focus: "Yes" if the homepage clearly markets to injury victims/plaintiffs (e.g. \
contingency-fee language, practice areas like car accidents, slip and fall, medical malpractice \
as plaintiff representation). "No" if it's clearly insurance defense, general practice with no PI \
emphasis, or a non-PI specialty. "Mixed" if it does both plaintiff PI and other work, or if the \
snippet is too thin to tell confidently — do not guess "Yes" from a thin snippet.

Respond ONLY with a single JSON object, no markdown fences, no preamble, matching this exact shape:
{
  "cms_detected": "<one of the valid values>",
  "cms_confidence": "<High|Medium|Low>",
  "cms_evidence": "<one or two short sentences citing what was found, e.g. 'Filevine listed in job posting for Case Manager role.'>",
  "hiring_signal": "<Yes|No>",
  "hiring_signal_evidence": "<short note or empty string>",
  "plaintiff_pi_focus": "<Yes|Mixed|No>",
  "plaintiff_pi_focus_note": "<short note, or empty string if homepage text wasn't available>"
}"""


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
    return {
        "cms_detected": cms,
        "cms_confidence": confidence,
        "cms_evidence": str(parsed.get("cms_evidence", ""))[:500],
        "hiring_signal": "Yes" if str(parsed.get("hiring_signal", "No")).lower().startswith("y") else "No",
        "hiring_signal_evidence": str(parsed.get("hiring_signal_evidence", ""))[:300],
        "plaintiff_pi_focus": pi_focus,
        "plaintiff_pi_focus_note": str(parsed.get("plaintiff_pi_focus_note", ""))[:300],
    }


def classify_firm(firm_name: str, fingerprint_result: dict, apollo_job_postings: list,
                   apollo_technologies: list, exa_evidence: list) -> dict:
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

    lines.append("APOLLO JOB POSTINGS (current openings at this firm):")
    if apollo_job_postings:
        for jp in apollo_job_postings[:8]:
            title = jp.get("title") or jp.get("name") or "Untitled posting"
            desc = (jp.get("description") or jp.get("snippet") or "")[:300]
            lines.append(f"  - {title}: {desc}")
    else:
        lines.append("  - none found / not available")
    lines.append("")

    lines.append("APOLLO TECHNOLOGY TAGS (Apollo's own tech-stack detection, if any):")
    lines.append(f"  - {', '.join(apollo_technologies) if apollo_technologies else 'none found / not available'}")
    lines.append("")

    lines.append("WEB SEARCH SNIPPETS (Exa):")
    if exa_evidence:
        for ev in exa_evidence[:4]:
            lines.append(f"  - [{ev.get('title', '')}]({ev.get('url', '')}): {ev.get('text', '')[:400]}")
    else:
        lines.append("  - none found / not available")

    evidence_text = "\n".join(lines)
    return _call_claude(evidence_text)
