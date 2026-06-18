"""
Stage 3 — Decision-maker lookup via firm website scraping.

Fetches the firm's About/Attorneys/Team pages and uses a small Claude call to
identify the best decision-maker contact (COO, Director of Ops, Managing Partner,
etc.). This replaces the previous Apollo people search, which required a paid
"master" API tier and was effectively broken for most users.

Website scraping is free. The Claude call per firm is tiny (~1200 chars of page
text + a short system prompt) — roughly the same cost as the CMS classification
call in Stage 2.

Output: data/contacts.csv
"""
import argparse
import json
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import CONTACT_PATHS_TO_CHECK, CLAUDE_MODEL, SITE_FETCH_TIMEOUT_SECONDS, USER_AGENT
from utils import DATA_DIR, read_csv_rows, append_csv_row, already_processed_keys, CallCounter

INPUT_PATH = os.path.join(DATA_DIR, "firms_raw.csv")
CMS_PATH = os.path.join(DATA_DIR, "cms_results.csv")
OUTPUT_PATH = os.path.join(DATA_DIR, "contacts.csv")

FIELDNAMES = ["domain", "decision_maker_name", "decision_maker_title", "decision_maker_note"]

CONTACT_SYSTEM_PROMPT = """You are extracting one decision-maker contact from a law firm's \
team or about page. Prefer these titles in order: Director of Operations, COO, \
Chief Operating Officer, Managing Partner, Owner, Founding Partner.

Respond ONLY with a single JSON object, no markdown fences:
{"name": "<full name or empty string>", "title": "<title or empty string>", \
"note": "website_team_page_scrape"}

If no clear decision-maker is found, return:
{"name": "", "title": "", "note": "not found on team page"}"""


def _extract_visible_text(html: str, max_chars: int = 1200) -> str:
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ").split())
        return text[:max_chars]
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
        return " ".join(text.split())[:max_chars]


def _fetch_contact_page(domain: str) -> tuple:
    """
    Try each path in CONTACT_PATHS_TO_CHECK and return (page_text, url) for the
    first one that succeeds. Returns ("", "") if none are reachable.
    """
    for path in CONTACT_PATHS_TO_CHECK:
        url = f"https://{domain}{path}"
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=SITE_FETCH_TIMEOUT_SECONDS,
            )
            time.sleep(0.5)
            if resp.status_code < 400:
                return _extract_visible_text(resp.text), url
        except requests.RequestException:
            continue
    return "", ""


def _extract_contact_claude(page_text: str) -> dict:
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set.")
    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=150,
            temperature=0,
            system=CONTACT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Team page text:\n{page_text}"}],
        )
        raw = "".join(b.text for b in response.content if b.type == "text").strip()
        raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        parsed = json.loads(raw)
        return {
            "decision_maker_name": str(parsed.get("name", ""))[:200],
            "decision_maker_title": str(parsed.get("title", ""))[:200],
            "decision_maker_note": str(parsed.get("note", "website_team_page_scrape"))[:200],
        }
    except Exception as e:
        return {
            "decision_maker_name": "",
            "decision_maker_title": "",
            "decision_maker_note": f"extraction failed: {e}",
        }


def main():
    parser = argparse.ArgumentParser(description="Look up decision-maker contacts via website scraping.")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    firms = read_csv_rows(INPUT_PATH)
    if not firms:
        print(f"No firms found in {INPUT_PATH} — run Stage 1 first.")
        return

    done = already_processed_keys(OUTPUT_PATH, "domain")
    classified = already_processed_keys(CMS_PATH, "domain")
    todo = [f for f in firms if f["domain"] in classified and f["domain"] not in done][: args.limit]
    counter = CallCounter()

    print(f"Stage 3: processing {len(todo)} firms ({len(done)} already done, skipped).")

    for i, firm in enumerate(todo, 1):
        domain = firm["domain"]
        print(f"[{i}/{len(todo)}] {firm['firm_name']} ({domain})")

        if args.dry_run:
            row = {
                "domain": domain,
                "decision_maker_name": "Mock Person",
                "decision_maker_title": "Managing Partner",
                "decision_maker_note": "dry-run mock",
            }
        else:
            page_text, page_url = _fetch_contact_page(domain)
            counter.tick("site_fetch_contact_page (free)", 1)

            if not page_text:
                row = {
                    "domain": domain,
                    "decision_maker_name": "",
                    "decision_maker_title": "",
                    "decision_maker_note": "no team/about page reachable",
                }
            else:
                contact = _extract_contact_claude(page_text)
                counter.tick("claude_contact_extraction", 1)
                row = {"domain": domain, **contact}

        append_csv_row(OUTPUT_PATH, row, FIELDNAMES)
        name = row.get("decision_maker_name") or "(not found)"
        print(f"    -> {name}")

    print(f"\nStage 3 complete. Results in {OUTPUT_PATH}")
    print(f"API usage this run: {counter.summary()}")
    if args.dry_run:
        print("NOTE: this was a --dry-run. No paid API calls were made; results are mock data.")


if __name__ == "__main__":
    main()
