# Plaintiff PI Firm CMS-Detection Pipeline

Automates the Week 1 brief: source ~150 plaintiff personal-injury firms in
NJ/NY/PA, detect which case-management system (CMS) each one runs, and
rank them by fit_score. Runs as a one-time batch you trigger yourself —
not a scheduled job.

## What's automated vs. what isn't

**Fully automated:**
- Firm sourcing (Apollo Organization Search, filtered by industry keyword, location, employee count)
- CMS site fingerprinting (free — direct HTTP fetch + pattern match, no API cost)
- Apollo job-postings + technology-tag lookup
- Exa neural search for CMS evidence (job boards, case studies, reviews)
- Final CMS call + plaintiff-PI-focus judgment (Claude, synthesizing all of the above)
- Decision-maker lookup (Apollo People Search) — *if* your Apollo key is a "master" key tier
- fit_score, sorting, Bonus-tab logic

**Not automated / known gaps:**
- `ads_running` (Google/TV/billboard ads) isn't in this version. None of the
  three APIs in scope (Apollo, Exa, Claude) have a clean, cheap signal for
  this — it would need a paid ad-intelligence API (SEMrush, SpyFu, etc.).
  Left as a manual spot-check column if you want to add it later.
- Apollo's company database may simply not have every small PI firm. If
  Stage 1 returns far fewer than expected, that's a coverage gap, not a bug
  — see "Extending firm sourcing" below.
- Direct LinkedIn/Indeed scraping was deliberately not built — both sites'
  ToS prohibit it. Apollo's job-postings data is the substitute signal here.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env and add your APOLLO_API_KEY, EXA_API_KEY, ANTHROPIC_API_KEY
```

## Usage — start cheap, then scale

```bash
# 1. Free wiring check — no API keys even required, uses mock data
python run_pipeline.py --dry-run --limit 5

# 2. Cheap real test — small batch, real APIs, see actual output quality and cost
python run_pipeline.py --limit 5

# 3. Look at data/final_output.csv. If it looks right, scale up:
python run_pipeline.py --limit 150
```

The pipeline is resumable: each stage skips firms already present in its
output CSV, keyed by domain. If you stop after `--limit 5` and rerun with
`--limit 150`, it picks up from where it left off rather than re-spending
credits on the first 5.

You can also run stages individually if you want to inspect one step's
output before letting the next stage spend money on it:

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
| `--skip-exa` | Stage 2 skips Exa search (the priciest per-firm signal); relies on free site fingerprinting + Apollo job postings only |
| `--skip-apollo-jobs` | Stage 2 skips the Apollo job-postings call per firm |
| `--skip-contacts` | Skips Stage 3 (decision-maker lookup) entirely |

Realistic per-firm cost shape: Stage 1 (Apollo) is paid per *page* of
results, not per firm, so it's cheap regardless of firm count. Stage 2 is
where most spend happens — 1 free site fetch + 1 Apollo job-postings call +
1 Exa search + 1 small Claude call, per firm. At ~150 firms that's ~150
Apollo job-posting calls, ~150 Exa searches, ~150 tiny Claude calls — each
individually cheap, but worth running `--limit 5` first to see your actual
per-firm cost on your plan before committing to the full batch.

Each stage prints an API-usage summary at the end so you can track this as
you go, rather than discovering total spend only after the full run.

## Known caveats to sanity-check before trusting the output

- **`decision_maker` may be blank for everyone.** Apollo's People API
  Search endpoint is credit-free but requires a "master" API key tier.
  If every row says "requires Apollo master API key," that's the cause —
  check Settings → API Keys in your Apollo dashboard, or fall back to
  manual lookup for this one column.
- **`est_attorneys` is actually Apollo's total-employee estimate**, not an
  attorney-specific count (paralegals/staff included). Treated as a rough
  proxy since no API in scope reports attorney headcount specifically.
  Worth a manual gut-check on the firms you're about to actually contact.
- **CMS confidence still deserves a skim, not blind trust**, especially
  anything marked "Medium" or "Low." The brief's own guardrail applies:
  a wrong CMS tag is worse than a blank one. Spot-check a sample of Hot/Warm
  results against `cms_evidence` before handing the list to sales.
- **Apollo's tracked-technologies list** (1,500+ SaaS products) probably
  doesn't include niche legal-only platforms like CloudLex or SmartAdvocate.
  Stage 2 checks this automatically and tells you in its console output
  whether any of the 5 target CMSes are even tech-trackable in Apollo —
  don't be surprised if none are; fingerprinting/jobs/Exa carry the weight.

## Extending firm sourcing if Apollo's coverage is thin

If Stage 1 returns well under 150 qualifying firms, options in rough order
of effort:
1. Widen `organization_locations` in `config.py` (e.g. add specific major
   cities if state-level HQ matching is missing some firms).
2. Loosen `q_organization_keyword_tags` (e.g. add "trial lawyer", "injury
   attorney") — Apollo's keyword tagging is its own classification and may
   not perfectly match how a firm describes itself.
3. Add a second sourcing path using Exa to search the trial-lawyer
   association directories named in the original brief (NJAJ, NYSTLA, PA
   Association for Justice) and extract firm names/domains from results —
   this is real engineering work, not a config tweak, so only worth it if
   Apollo's coverage gap turns out to be large.

## Output

- `data/final_output.csv` — main deliverable, sorted Hot → Warm → Cold/Unknown
- `data/bonus.csv` — firms outside the 5–50 employee box that still scored
  Hot/Warm (per the brief's instruction not to throw these away)
- `data/firms_raw.csv`, `data/cms_results.csv`, `data/contacts.csv` —
  intermediate per-stage data, kept so you can audit any single firm's
  evidence trail without rerunning anything

Import `final_output.csv` into Airtable/Sheets as the brief specifies — this
pipeline produces the CSV; it doesn't push to Airtable directly. (Easy to
add later via Airtable's API if you want that automated too.)
