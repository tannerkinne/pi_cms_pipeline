# Plaintiff PI Firm CMS-Detection Pipeline

Automates sourcing ~200 plaintiff personal-injury firms across NJ/NY/PA/CT,
detecting which case-management system (CMS) each one runs, and ranking them
by fit_score. Runs as a one-time batch you trigger yourself — not a scheduled job.

## What's automated vs. what isn't

**Fully automated:**
- Firm sourcing (Google Places API — searches Google Maps using 7 query variants per state)
- CMS site fingerprinting (free — direct HTTP fetch + pattern match, no API cost)
- Careers-page job title extraction for hiring signal detection (free, part of site fetch)
- Exa neural search for CMS evidence (job boards, case studies, reviews)
- Final CMS call + plaintiff-PI-focus judgment (Claude, synthesizing all of the above)
- Decision-maker lookup (scrapes firm's About/Attorneys/Team pages + Claude extraction)
- fit_score, sorting, Bonus-tab logic

**Not automated / known gaps:**
- `ads_running` (Google/TV/billboard ads) isn't in this version — would need a paid
  ad-intelligence API (SEMrush, SpyFu, etc.). Left as a manual spot-check column.
- `est_attorneys` is blank for all rows. Google Places doesn't provide headcount data.
  Worth a manual gut-check on firms you're about to contact.
- Direct LinkedIn/Indeed scraping was deliberately not built — both sites' ToS prohibit it.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env and add your GOOGLE_PLACES_API_KEY, EXA_API_KEY, ANTHROPIC_API_KEY
```

**Google Places API key setup**: Enable **"Places API (New)"** in Google Cloud Console
(APIs & Services → Library). This is a distinct product from the older "Places API" —
the New one is required. Then create an API key under APIs & Services → Credentials.

### Optional: JS-rendered sites (headless browser)

A minority of firms inject their "Client Login" portal link or their attorney
cards client-side, so the static fetch sees neither. Stage 2 has an opt-in
headless-browser fallback (Playwright + Chromium) that re-fetches *only* those
low-signal sites and re-runs the existing parsers over the rendered HTML.

It's behind an **optional extra** — the base install stays dependency-light:

```bash
pip install -r requirements-render.txt
python -m playwright install chromium   # Chromium only, ~150 MB — do NOT run bare `playwright install`
```

Then enable it via `--render`:

```bash
# Render only firms whose static fetch found no portal link AND < 2 attorney names:
python run_pipeline.py --limit 200 --render fallback
python pipeline/02_detect_cms.py --limit 200 --render fallback

# Render every firm (debugging only — much slower):
python pipeline/02_detect_cms.py --limit 20 --render always
```

Modes: `off` (default; static only, zero new deps), `fallback` (render only
low-signal firms — the recommended cost-controlled mode), `always` (render every
firm, for debugging). If the extra isn't installed the render helper degrades to
a no-op and the static result stands. `--render` is ignored under `--dry-run`.
The end-of-run summary reports a `playwright_render` count so you can see how
often it actually fired.

### Optional: export to Google Sheets

Stage 4 always writes `data/final_output.csv` and `data/bonus.csv`. If you'd
rather hand off a live spreadsheet, Stage 5 mirrors those CSVs to a Google Sheet
(a `Leads` tab and a `Bonus` tab). It's behind an **optional extra**:

```bash
pip install -r requirements-sheets.txt
```

One-time setup: create a Google Cloud **service account**, enable the Google
Sheets API, download its JSON key, and **share your target sheet with the service
account's `client_email` as an Editor**. Then set two env vars (see `.env.example`):

```bash
GOOGLE_SHEETS_CREDENTIALS_FILE=/path/to/service-account.json
GOOGLE_SHEET_ID=<the long id from the sheet URL: /d/<THIS>/edit>
```

Run it standalone after Stage 4, or fold it into a full run:

```bash
python pipeline/05_export_to_sheets.py            # uses the env vars above
python run_pipeline.py --limit 200 --to-sheets    # runs Stages 1-4 then pushes
```

Each run clears and rewrites both tabs so the sheet stays in sync with the CSVs.
`--to-sheets` is skipped under `--dry-run`, and if the extra/credentials are
missing the step prints an actionable message and exits without touching the run's
CSV output.

## Usage — start cheap, then scale

```bash
# 1. Free wiring check — no API keys even required, uses mock data
python run_pipeline.py --dry-run --limit 5

# 2. Cheap real test — small batch, real APIs, see actual output quality and cost
python run_pipeline.py --limit 5

# 3. Look at data/final_output.csv. If it looks right, scale up:
python run_pipeline.py --limit 200
```

The pipeline is resumable: each stage skips firms already present in its
output CSV, keyed by domain. If you stop after `--limit 5` and rerun with
`--limit 200`, it picks up from where it left off rather than re-spending
credits on the first 5.

You can also run stages individually to inspect one step's output before
letting the next stage spend money:

```bash
python pipeline/01_source_firms.py --limit 20
python pipeline/02_detect_cms.py --limit 20
python pipeline/03_enrich_contacts.py --limit 20
python pipeline/04_score_and_export.py
```

## Cost-control knobs

| Flag | Effect |
|---|---|
| `--dry-run` | Zero API calls anywhere, mock data, validates wiring |
| `--limit N` | Caps how many firms go through the *entire* pipeline per run |
| `--skip-exa` | Stage 2 skips Exa search (the priciest per-firm signal); relies on free site fingerprinting only |
| `--skip-contacts` | Skips Stage 3 (decision-maker lookup) entirely |
| `--render {off,fallback,always}` | Stage 2 headless-browser fallback for JS-rendered sites (requires the optional render extra). `fallback` renders only low-signal firms; adds a few minutes per ~200-firm batch. Default `off`. |

**Realistic per-run cost** at 200 firms:
- Stage 1 (Google Places): ~$2–3 total (billed per API call, not per firm; ~140 calls across 4 states × 7 queries)
- Stage 2: ~200 Exa searches + ~200 small Claude calls — the main spend per firm
- Stage 3: free site fetches + ~200 tiny Claude calls (contact extraction)

Google gives $200/month free credit, so Stage 1 is effectively free. Run `--limit 5`
first to see your actual per-firm Exa + Claude cost before committing to the full batch.

Each stage prints an API-usage summary at the end so you can track spend as you go.

### Unified cost tracking

Every API call (Google Places, Exa, Claude, Apollo) is logged to
`data/api_usage.csv` with its tokens/credits/results and an estimated USD cost
derived from the `COST_RATES` table in `config.py`. Each stage prints a cost
summary at the end, and `run_pipeline.py` prints a full-run total. Review spend
any time without rerunning:

```bash
python scripts/cost_report.py              # breakdown by stage, service, and date
python scripts/cost_report.py --since 2026-06-24   # only calls on/after a date
```

Tracking is best-effort and never blocks the pipeline — any tracker failure is
logged to `data/tracker_errors.log` instead of raising. Edit `COST_RATES` to
match your contracted rates (Claude rates are per million tokens; Exa/Places are
per call; Apollo is per credit).

### Apollo email enrichment (Stage 3)

When `APOLLO_API_KEY` is set, Stage 3 takes each decision-maker name it finds and
calls Apollo People Match (`/api/v1/people/match`) for a **verified** email,
writing it to a `decision_maker_email` column in `contacts.csv` and
`final_output.csv`. Only Apollo-verified addresses are kept — misses are left
blank and logged, never guessed. Resume is by non-empty email (already-enriched
firms are skipped), and calls are spaced by `APOLLO_RATE_LIMIT_DELAY_SECONDS`
(default 1.0s). If `APOLLO_API_KEY` is unset, Stage 3 still runs and just leaves
the email column blank.

When Apollo has no verified email, Stage 3 falls back to **scraping the firm's own
website** for the decision-maker's published address — checking the team/about
page, the person's individual bio page (one same-domain hop), and the
homepage/contact pages. A scraped email is accepted **only** when it's on the
firm's own domain AND its local-part matches the decision-maker's name (e.g.
`dkwartler@kmfirm.com`), so it's always the actual person, never a generic inbox
or third-party address. Junk domains are filtered via `EMAIL_SCRAPE_DENYLIST_DOMAINS`
in `config.py`. This step is free (no API spend).

## Known caveats to sanity-check before trusting the output

- **`est_attorneys` is blank for all rows.** Google Places doesn't expose headcount.
  Spot-check firm sizes manually for firms you're about to contact.
- **`decision_maker` may be blank** if the firm's website doesn't have a readable
  About/Team page. Manual lookup is the fallback for those rows.
- **CMS confidence still deserves a skim, not blind trust**, especially anything marked
  "Medium" or "Low." A wrong CMS tag is worse than a blank one. Spot-check a sample
  of Hot/Warm results against `cms_evidence` before handing the list to sales.
- **Google Places will return some defense-side or general-practice firms.** The pipeline
  pre-filters obvious ones by name ("insurance", "defense") and uses Claude's
  `plaintiff_pi_focus` judgment on homepage text to catch the rest — but review any
  "Mixed" rows before treating them as targets.

## Tuning firm sourcing coverage

If Stage 1 returns fewer firms than expected, options in rough order of effort:

1. Increase `GOOGLE_PLACES_MAX_PAGES` in `config.py` (default 5 pages per query;
   each additional page = 1 API call ≈ $0.017).
2. Add more entries to `GOOGLE_PLACES_QUERIES` in `config.py` — additional search
   terms cover firms that describe themselves differently (e.g. "wrongful death attorney",
   "truck accident lawyer").
3. Add sub-region bounding boxes for underrepresented areas (e.g. western PA, upstate NY)
   to `GOOGLE_PLACES_STATE_BBOXES` in `config.py`.

## Output

- `data/final_output.csv` — main deliverable, sorted Hot → Warm → Cold/Unknown
- `data/bonus.csv` — reserved for firms outside a size target that still scored Hot/Warm
  (currently empty since Google Places doesn't provide headcount data)
- `data/firms_raw.csv`, `data/cms_results.csv`, `data/contacts.csv` —
  intermediate per-stage data, kept so you can audit any single firm's evidence
  trail without rerunning anything (`contacts.csv` now carries `decision_maker_email`)
- `data/api_usage.csv` — one row per API call with tokens/credits/results and
  estimated cost; read it with `scripts/cost_report.py`

Import `final_output.csv` into Airtable/Sheets — this pipeline produces the CSV;
it doesn't push to Airtable directly. (Easy to add later via Airtable's API.)
