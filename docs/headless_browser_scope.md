# Scoping: Optional Headless-Browser Render Path

**Status:** Design proposal (not implemented). Seeking greenlight.
**Author/date:** Pipeline engineering, 2026-06-19
**Audience:** Senior engineer / reviewer who can approve or reject.

---

## 1. Problem

The site fingerprinter (`site_fingerprint.py`) fetches static HTML with `requests`
and scans it. Two signals degrade on JavaScript-rendered sites:

1. **JS-rendered portal links (CMS detection).** Some firms inject the
   "Client Login" / "Client Portal" button client-side. `_find_portal_links()`
   parses only the static anchor set, so the link — and the high-value redirect
   target (e.g. `firmname.cloudlex.com`) — is never seen.
2. **JS-rendered attorney listings (`est_attorneys`).** Some team pages render the
   attorney cards client-side, so no names reach the classifier and it correctly
   estimates 0.

**Important scoping note from the Task A investigation:** the *original* motivating
example, `jcinjurylaw.com`, turned out **not** to require a browser — the names
were present in the static HTML but buried under repeated Yext placeholder
boilerplate past the char cap. That class of failure is now fixed cheaply in
`site_fingerprint.py` (boilerplate stripping + JSON-LD parsing + honorific/CTA
name extraction over the full page text). The headless path described here is for
the **genuinely JS-rendered residue** that survives those cheap fixes — a minority
of firms. We should size it accordingly: a bounded fallback, not a default.

---

## 2. Recommendation

**Use Playwright (Python, sync API), Chromium channel.**

| Option | Verdict | Why |
| --- | --- | --- |
| **Playwright** | ✅ Recommended | Actively maintained; reliable auto-waiting (`wait_for_load_state("networkidle")`) which is exactly what we need for JS-injected content; clean `pip install playwright` + `playwright install chromium`; good headless support on macOS + Linux CI; sync API drops into our synchronous, per-firm loop without an asyncio rewrite. |
| Selenium | ➖ Workable, worse DX | Needs a separately managed driver (or Selenium Manager), flakier waits, more boilerplate. No advantage here. |
| requests-html / pyppeteer | ❌ Avoid | Effectively unmaintained; pyppeteer pins old Chromium and breaks on modern macOS/Python; requests-html wraps it. |

**Browser-binary footprint:** `playwright install chromium` downloads ~150 MB
(one browser only — do **not** run bare `playwright install`, which pulls Chromium
+ Firefox + WebKit, ~400–500 MB). Python wheel itself is small. This is the main
new install cost and the reason to gate it behind a packaging extra.

**Python 3.9 / macOS:** Playwright supports 3.9 and ships prebuilt Chromium for
macOS (Intel + Apple Silicon); no system Chrome required.

---

## 3. What it fixes

- **CMS detection:** after render, `_find_portal_links()` sees JS-injected
  "Client Login" anchors; `_follow_portal_links()` then resolves them to the CMS
  vendor host — our single strongest signal.
- **`est_attorneys`:** after render, attorney cards exist in the DOM, so the
  existing JSON-LD + name-extraction logic (already added in Task A) finds names.
  The render path reuses that extraction unchanged — it only changes *how the HTML
  is obtained*, not how names are parsed.

---

## 4. Where it plugs in

Keep the default path 100% as-is (fast, free, no browser). Introduce one new
internal helper and a single opt-in flag.

```python
# site_fingerprint.py
def _render_html(url: str, timeout_ms: int = 12000) -> str | None:
    """Return fully-rendered HTML via Playwright, or None on any failure.
    Imported lazily so the dependency is only needed when render is enabled."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            html = page.content()
            browser.close()
            return html
    except Exception:
        return None   # never raise — same contract as the rest of the module
```

**Opt-in surface:**

- `fingerprint_site(domain, render="fallback")` where `render ∈ {"off", "fallback", "always"}`
  (default `"off"`).
- Plumb a `--render {off,fallback,always}` flag through `pipeline/02_detect_cms.py`
  into the call. Default `off` so existing runs are byte-for-byte unchanged.

**Fallback-only trigger (the key cost control).** Render *only* when the cheap
static fetch came back low-signal, so we don't pay the render cost on firms that
already worked. Concretely, after the static pass, render a page if **both**:

- no portal links were found on the static homepage, **and**
- `attorney_names` is empty (or below a small threshold, e.g. < 2) after the
  static attorney scrape.

Render at most the homepage (for portal links) and the first reachable attorney
path (for names) — not every path — to bound the cost. Feed the rendered HTML
back through the *existing* `_find_portal_links` / `_extract_jsonld_names` /
`_extract_attorney_names` helpers; no parsing logic is duplicated.

---

## 5. Cost / performance

| Dimension | Static (today) | Rendered page |
| --- | --- | --- |
| Latency / page | ~0.1–0.5 s + politeness delay | **~2–6 s** (cold browser launch dominates; networkidle wait adds 1–3 s) |
| Memory | negligible | **~150–300 MB** per live Chromium context |
| Failure mode | HTTP error | timeout / navigation error (handled → None) |

**200-firm batch math (fallback-only).** If ~15–25% of firms trip the fallback
heuristic and each renders ~1–2 pages at ~4 s, that's roughly 30–50 firms ×
~6–8 s ≈ **3–7 extra minutes** on top of the static run — acceptable for a batch
job. If we ever rendered *every* firm × multiple pages, we'd add 15–25+ minutes
and far more memory; that's why **default-on is explicitly out of scope for v1.**

**Parallelism caution.** Each Chromium context is heavy. Do **not** fan out one
browser per firm across many threads — memory will spike. If we later parallelize,
launch **one** browser and reuse it across `new_page()`/`new_context()` calls with
a small bounded pool (2–4), and keep the existing per-fetch politeness delay so we
don't hammer a single firm's origin.

---

## 6. Risks / tradeoffs

- **Bot detection.** Headless Chromium is more fingerprintable than `requests`.
  Some sites serve a challenge/block to it. Mitigation: set a realistic
  `user_agent`, accept that a fraction will still fail, and always fall back to the
  static result rather than erroring.
- **Timeouts / flakiness.** `networkidle` can hang on sites with long-polling or
  ad/analytics beacons. Mitigation: hard `timeout_ms` cap (~12 s) and treat
  timeout as "no extra signal," not a batch failure.
- **CI / headless-server install.** On Linux CI, Chromium needs system libs;
  `playwright install --with-deps chromium` handles that but requires root/apt.
  This must be wired into the CI image, not assumed. macOS dev machines are fine
  out of the box.
- **Determinism.** Rendered output varies run-to-run (A/B content, lazy loads),
  unlike the static fetch. Keep `--dry-run` on the static path only.
- **Environment quirk already present (LibreSSL/urllib3).** This repo's macOS
  Python links against LibreSSL, so urllib3 v2 emits a `NotOpenSSLWarning` on
  every `requests` call (seen throughout test runs). Playwright does **not** use
  urllib3 for page navigation, so it sidesteps that warning — but it is one more
  reason to keep the static path as the default and the browser as a narrow
  fallback rather than betting the whole pipeline on a second networking stack on
  an already-quirky SSL setup. Suppress the warning at startup so render logs stay
  readable.

---

## 7. Rollout plan (phased)

1. **Dependency behind an extra.** Add `playwright` to an optional
   `[render]` extra (e.g. `pip install -e .[render]` / a separate
   `requirements-render.txt`), not the base install. Document
   `playwright install chromium`. Base pipeline keeps zero new required deps.
2. **Add `_render_html` + `render="off"` default.** Lazy import; helper returns
   `None` if Playwright isn't installed. Zero behavior change when the extra is
   absent. Ship with unit coverage that the helper degrades to `None` cleanly.
3. **Fallback heuristic (opt-in).** Wire `--render fallback`. Render only when the
   static pass is low-signal (no portal links AND no/low attorney names), homepage
   + first attorney page only. Measure on a labeled sample: how many extra
   portal-link hits and non-zero `est_attorneys` does it recover, at what added
   minutes/batch?
4. **Tune + bound.** Adjust the trigger threshold and per-firm page cap from the
   sample data. Add the single-browser reuse pool if batch latency matters.
5. **Default-on decision.** Only if the recovered-signal rate clearly justifies the
   latency/footprint *and* CI install is solved, consider flipping the default to
   `fallback`. `always` stays a manual debugging option, never the default.

---

## 8. One-paragraph recommendation

Add Playwright (Chromium-only, ~150 MB) behind an optional `[render]` extra and an
opt-in `--render fallback` flag. Use it strictly as a fallback that fires only when
the cheap static fetch yields no portal link and no attorney names, rendering at
most two pages per firm and reusing the existing parse helpers. This recovers the
genuinely JS-rendered minority (which the Task A boilerplate/JSON-LD fixes do not
cover) while keeping the default path fast, free, deterministic, and dependency-light.
Prove the signal lift on a labeled sample before considering default-on.
