"""
Stage 3 — Decision-maker lookup via firm website scraping + Apollo.

The firm's own attorneys/team page is the PRIMARY source (scraped and parsed by
a small Claude call) — it's the most up-to-date record of who runs the practice.
Apollo People Search is the fallback, queried only to recover a last name when
the scrape returns no name or a first name alone. Once a name is resolved,
Apollo People Match enriches it with a verified email (if APOLLO_API_KEY is set).

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

from api_wrappers import apollo_client, cost_tracker
from config import (CONTACT_PATHS_TO_CHECK, CLAUDE_MODEL, SITE_FETCH_TIMEOUT_SECONDS,
                    USER_AGENT, APOLLO_RATE_LIMIT_DELAY_SECONDS)
from utils import (DATA_DIR, read_csv_rows, append_csv_row, already_processed_keys,
                   write_csv_rows, CallCounter)

INPUT_PATH = os.path.join(DATA_DIR, "firms_raw.csv")
CMS_PATH = os.path.join(DATA_DIR, "cms_results.csv")
OUTPUT_PATH = os.path.join(DATA_DIR, "contacts.csv")

FIELDNAMES = ["domain", "decision_maker_name", "decision_maker_title",
              "decision_maker_email", "decision_maker_note"]

CONTACT_SYSTEM_PROMPT = """You are extracting one decision-maker contact from a law firm's \
team or about page. Prefer these titles in order: Managing Partner, Owner, \
Founding Partner, Director of Operations, COO, Chief Operating Officer.

ALWAYS return the person's FULL name — both first and last name — exactly as it \
appears on the page. A surname is required for downstream email lookup, so never \
return a first name alone when the page shows a full name. Strip credential \
suffixes like "Esq." or ", P.C." Do not invent a last name; only use what the \
page actually shows.

Respond ONLY with a single JSON object, no markdown fences:
{"name": "<full first and last name, or empty string>", "title": "<title or empty string>", \
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
            if resp.status_code < 400:
                time.sleep(0.5)
                return _extract_visible_text(resp.text), url
        except requests.RequestException:
            continue
    return "", ""


def _migrate_contacts_schema():
    """Backfill the new decision_maker_email column into an older contacts.csv.

    append_csv_row only writes a header when the file doesn't exist, so a
    contacts.csv created before this column existed would get 5-field rows
    appended under a 4-field header — silently misaligning every column. If the
    existing file is missing the email column, rewrite it once with the full
    schema (empty email for existing rows) so subsequent appends line up.
    """
    existing = read_csv_rows(OUTPUT_PATH)
    if not existing:
        return
    if all("decision_maker_email" in r for r in existing):
        return  # already on the new schema
    migrated = [{**{k: "" for k in FIELDNAMES}, **r} for r in existing]
    write_csv_rows(OUTPUT_PATH, migrated, FIELDNAMES, mode="w")
    print(f"  [schema] added decision_maker_email column to existing {OUTPUT_PATH}")


def _track_claude_usage(response):
    """Log the contact-extraction Claude call to the cost tracker. Never raises."""
    try:
        u = getattr(response, "usage", None)
        if u is None:
            return
        cost_tracker.record(
            "claude", "messages.create (contact)", success=True, model=CLAUDE_MODEL,
            tokens_input=getattr(u, "input_tokens", 0) or 0,
            tokens_output=getattr(u, "output_tokens", 0) or 0,
            tokens_cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
            tokens_cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
    except Exception:
        pass


def _split_name(full_name: str) -> tuple:
    """Split a 'First [Middle ...] Last' string into (first, last) for Apollo.

    Drops a trailing credential suffix (', Esq.', ', LLC', etc.) and collapses
    any middle names into the first-name field so the last token is the surname.
    Returns ('', '') if nothing usable is present.
    """
    name = (full_name or "").split(",")[0].strip()
    parts = [p for p in name.split() if p]
    if len(parts) < 2:
        return (parts[0] if parts else "", "")
    return (" ".join(parts[:-1]), parts[-1])


def _has_surname(full_name: str) -> bool:
    """True if the name has a usable last name (needed for email lookup)."""
    return bool(_split_name(full_name)[1])


def _enrich_email(decision_maker_name: str, domain: str, apollo_enabled: bool) -> str:
    """Return a verified email for the decision-maker, or '' if none / disabled.

    Never guesses: only returns an address Apollo reports as verified.
    """
    if not apollo_enabled or not decision_maker_name:
        return ""
    first, last = _split_name(decision_maker_name)
    if not last:
        return ""
    try:
        result = apollo_client.enrich_email(first, last, domain)
    except RuntimeError:
        # APOLLO_API_KEY vanished between the start-of-run check and here.
        return ""
    finally:
        # Politeness delay between Apollo calls regardless of outcome.
        time.sleep(APOLLO_RATE_LIMIT_DELAY_SECONDS)
    return result["email"] if result else ""


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
        _track_claude_usage(response)
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
    parser.add_argument("--force", action="store_true",
                        help="Re-scrape and overwrite existing contacts instead of skipping "
                             "already-enriched domains. A previously-found verified email is "
                             "reused (not re-billed) when the re-resolved name is unchanged.")
    parser.add_argument("--only", default="",
                        help="Comma-separated domains to (re)process — overwrites just those "
                             "rows and ignores --limit. Implies --force. Useful for sampling.")
    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    cost_tracker.set_context(stage="03")
    _migrate_contacts_schema()
    firms = read_csv_rows(INPUT_PATH)
    if not firms:
        print(f"No firms found in {INPUT_PATH} — run Stage 1 first.")
        return

    # Apollo email enrichment is opt-in on the key being present. If it's not
    # set, warn once and skip enrichment entirely rather than crashing.
    apollo_enabled = bool(os.environ.get("APOLLO_API_KEY")) and not args.dry_run
    if not args.dry_run and not apollo_enabled:
        print("  [warn] APOLLO_API_KEY not set — skipping Apollo email enrichment "
              "(decision_maker_email will be left blank).")

    # Snapshot existing contacts (by domain) so --force can reuse an unchanged
    # name's already-verified email instead of re-billing Apollo for it.
    existing_rows = read_csv_rows(OUTPUT_PATH)
    existing_by_domain = {r["domain"]: r for r in existing_rows}
    classified = already_processed_keys(CMS_PATH, "domain")

    only = {d.strip() for d in args.only.split(",") if d.strip()}
    if args.force or only:
        # Re-scrape, overwriting prior rows. --only targets specific domains (no
        # limit); otherwise re-scrape everything up to --limit. Drop the rows
        # we're about to regenerate up front so the fresh appends replace them
        # (no duplicates), while keeping incremental crash-safety.
        if only:
            todo = [f for f in firms if f["domain"] in classified and f["domain"] in only]
        else:
            todo = [f for f in firms if f["domain"] in classified][: args.limit]
        todo_domains = {f["domain"] for f in todo}
        retained = [r for r in existing_rows if r["domain"] not in todo_domains]
        write_csv_rows(OUTPUT_PATH, retained, FIELDNAMES, mode="w")
        scope = f"{len(todo)} firms" + (" (--only)" if only else "")
        print(f"Stage 3 [force]: re-scraping {scope} (website-primary), "
              f"overwriting their existing rows.")
    else:
        # Resume support: a domain is "done" only once it has a NON-EMPTY
        # decision_maker_email. Domains scraped by an older run (no email column,
        # or an empty one) are reprocessed so their emails get backfilled; Stage 4
        # dedupes by domain (last row wins), so the backfilled row is used.
        done = {r["domain"] for r in existing_rows if r.get("decision_maker_email")}
        todo = [f for f in firms if f["domain"] in classified and f["domain"] not in done][: args.limit]
        print(f"Stage 3: processing {len(todo)} firms ({len(done)} already enriched, skipped).")

    counter = CallCounter()

    for i, firm in enumerate(todo, 1):
        domain = firm["domain"]
        cost_tracker.set_context(stage="03", domain=domain)
        print(f"[{i}/{len(todo)}] {firm['firm_name']} ({domain})")

        if args.dry_run:
            row = {
                "domain": domain,
                "decision_maker_name": "Mock Person",
                "decision_maker_title": "Managing Partner",
                "decision_maker_email": "mock.person@example.com",
                "decision_maker_note": "dry-run mock",
            }
        else:
            page_text, page_url = _fetch_contact_page(domain)
            counter.tick("site_fetch_contact_page (free)", 1)

            if not page_text:
                scrape_contact = {
                    "decision_maker_name": "",
                    "decision_maker_title": "",
                    "decision_maker_note": "no team/about page reachable",
                }
            else:
                scrape_contact = _extract_contact_claude(page_text)
                counter.tick("claude_contact_extraction", 1)

            # Website scrape is the PRIMARY source — a firm's own team/about page
            # is the most up-to-date record of who runs the practice. Apollo is a
            # fallback used specifically to recover a surname: we only query it
            # when the scrape produced no name, or only a first name (no usable
            # last name for the downstream email lookup). This also avoids an
            # Apollo call/credit whenever the scrape already gave a full name.
            contact = scrape_contact
            if not _has_surname(contact.get("decision_maker_name", "")):
                apollo_result = None
                try:
                    apollo_result = apollo_client.find_decision_maker(domain)
                    counter.tick("apollo_people_search", 1)
                except RuntimeError:
                    pass  # APOLLO_API_KEY not set — keep the scrape result
                else:
                    if apollo_enabled:
                        time.sleep(APOLLO_RATE_LIMIT_DELAY_SECONDS)
                # Take Apollo only when it actually adds a surname the scrape
                # lacked, or when the scrape found no name at all.
                if apollo_result and (
                    _has_surname(apollo_result.get("decision_maker_name", ""))
                    or not contact.get("decision_maker_name", "").strip()
                ):
                    contact = apollo_result

            # Email enrichment: take the resolved decision-maker name + domain
            # and ask Apollo People Match for a verified address. On a re-scrape,
            # reuse a previously-verified email when the person is unchanged so we
            # don't re-bill Apollo for it.
            new_name = contact.get("decision_maker_name", "").strip()
            prev = existing_by_domain.get(domain, {})
            prev_email = (prev.get("decision_maker_email") or "").strip()
            prev_name = (prev.get("decision_maker_name") or "").strip()
            if prev_email and prev_name and prev_name == new_name:
                email = prev_email
                counter.tick("email_reused (unchanged name)", 1)
            else:
                email = _enrich_email(new_name, domain, apollo_enabled)
                if email:
                    counter.tick("apollo_email_match (hit)", 1)
                elif apollo_enabled and new_name:
                    counter.tick("apollo_email_match (miss)", 1)
                    print(f"    [miss] no verified Apollo email for {new_name}")

            row = {"domain": domain, "decision_maker_email": email, **contact}

        append_csv_row(OUTPUT_PATH, row, FIELDNAMES)
        name = row.get("decision_maker_name") or "(not found)"
        email_note = f" <{row['decision_maker_email']}>" if row.get("decision_maker_email") else ""
        print(f"    -> {name}{email_note}")

    print(f"\nStage 3 complete. Results in {OUTPUT_PATH}")
    print(f"API usage this run: {counter.summary()}")
    print(cost_tracker.session_summary("Stage 3"))
    if args.dry_run:
        print("NOTE: this was a --dry-run. No paid API calls were made; results are mock data.")


if __name__ == "__main__":
    main()
