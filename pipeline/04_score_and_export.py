"""
Stage 4 — Merge Stage 1-3 outputs, apply fit_score, sort, and export the
final deliverable matching the brief's output schema. No API calls.

Outputs:
  data/final_output.csv  — main list, sorted by fit_score (Hot > Warm > Cold/Unknown)
  data/bonus.csv          — firms outside the 5-100 employee box that still
                             scored Hot/Warm (per the brief's "log it in a
                             separate Bonus tab, don't throw it away" rule)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import fit_score, attorney_count_in_range, ATTORNEY_MIN_COUNT, ATTORNEY_MAX_COUNT
from utils import DATA_DIR, read_csv_rows, write_csv_rows

FIRMS_PATH = os.path.join(DATA_DIR, "firms_raw.csv")
CMS_PATH = os.path.join(DATA_DIR, "cms_results.csv")
CONTACTS_PATH = os.path.join(DATA_DIR, "contacts.csv")
FINAL_PATH = os.path.join(DATA_DIR, "final_output.csv")
BONUS_PATH = os.path.join(DATA_DIR, "bonus.csv")
OUT_OF_RANGE_PATH = os.path.join(DATA_DIR, "out_of_range.csv")

FIELDNAMES = [
    "firm_name", "website", "city", "state", "plaintiff_pi_focus", "trial_focused",
    "est_attorneys", "cms_detected", "cms_confidence", "cms_evidence", "decision_maker",
    "decision_maker_email", "hiring_signal", "hiring_signal_evidence", "data_source", "fit_score",
]

FIT_SORT_ORDER = {"Hot": 0, "Warm": 1, "Cold/Unknown": 2}

# Employee range used in Stage 1 sourcing — kept here only to flag bonus-tab
# logic if you widen sourcing later; with default sourcing every row is
# already inside the box, so bonus.csv will typically be empty.
SIZE_BOX_MIN, SIZE_BOX_MAX = 5, 100


def main():
    firms = {r["domain"]: r for r in read_csv_rows(FIRMS_PATH)}
    cms_results = {r["domain"]: r for r in read_csv_rows(CMS_PATH)}
    contacts = {r["domain"]: r for r in read_csv_rows(CONTACTS_PATH)}

    if not firms:
        print(f"No data in {FIRMS_PATH} — run earlier stages first.")
        return

    main_rows, bonus_rows, out_of_range_rows = [], [], []
    oor_low = oor_high = 0

    for domain, firm in firms.items():
        if domain not in cms_results:
            continue
        cms = cms_results[domain]
        contact = contacts.get(domain, {})

        cms_detected = cms.get("cms_detected", "Unknown")
        score = fit_score(cms_detected)

        decision_maker = ""
        if contact.get("decision_maker_name"):
            decision_maker = f"{contact['decision_maker_name']} ({contact.get('decision_maker_title', '')})"
        decision_maker_email = contact.get("decision_maker_email", "")

        row = {
            "firm_name": firm.get("firm_name", ""),
            "website": firm.get("website", ""),
            "city": firm.get("city", ""),
            "state": firm.get("state", ""),
            "plaintiff_pi_focus": cms.get("plaintiff_pi_focus", ""),
            "trial_focused": cms.get("trial_focused", ""),
            "est_attorneys": cms.get("est_attorneys", ""),
            "cms_detected": cms_detected,
            "cms_confidence": cms.get("cms_confidence", ""),
            "cms_evidence": cms.get("cms_evidence", ""),
            "decision_maker": decision_maker,
            "decision_maker_email": decision_maker_email,
            "hiring_signal": cms.get("hiring_signal", ""),
            "hiring_signal_evidence": cms.get("hiring_signal_evidence", ""),
            "data_source": firm.get("source", ""),
            "fit_score": score,
        }

        # Authoritative size gate: final_output only ever contains firms whose
        # est_attorneys (from Stage 2 classification) is within the 5-100 band.
        # Out-of-range firms are NOT dropped silently — they're written to
        # out_of_range.csv so you can see exactly how many replacements to source.
        if not attorney_count_in_range(cms.get("est_attorneys", "")):
            try:
                n = int(float(cms.get("est_attorneys", "")))
            except (TypeError, ValueError):
                n = 0
            if n > ATTORNEY_MAX_COUNT:
                oor_high += 1
            else:
                oor_low += 1
            out_of_range_rows.append(row)
            continue

        # is_outlier (bonus tab) is reserved for future size-banded sourcing;
        # with the hard gate above, every main row is already in the 5-100 box.
        is_outlier = False

        if is_outlier and score in ("Hot", "Warm"):
            bonus_rows.append(row)
        else:
            main_rows.append(row)

    main_rows.sort(key=lambda r: FIT_SORT_ORDER.get(r["fit_score"], 3))
    bonus_rows.sort(key=lambda r: FIT_SORT_ORDER.get(r["fit_score"], 3))

    write_csv_rows(FINAL_PATH, main_rows, FIELDNAMES, mode="w")
    write_csv_rows(BONUS_PATH, bonus_rows, FIELDNAMES, mode="w")
    write_csv_rows(OUT_OF_RANGE_PATH, out_of_range_rows, FIELDNAMES, mode="w")

    hot = sum(1 for r in main_rows if r["fit_score"] == "Hot")
    warm = sum(1 for r in main_rows if r["fit_score"] == "Warm")
    cold = sum(1 for r in main_rows if r["fit_score"] == "Cold/Unknown")

    print(f"Stage 4 complete.")
    print(f"  Main list (in range, {ATTORNEY_MIN_COUNT}-{ATTORNEY_MAX_COUNT} attorneys): "
          f"{len(main_rows)} firms -> {FINAL_PATH}")
    print(f"    Hot: {hot} | Warm: {warm} | Cold/Unknown: {cold}")
    print(f"  Out of range (excluded from final, need replacing): "
          f"{len(out_of_range_rows)} firms -> {OUT_OF_RANGE_PATH}")
    print(f"    <{ATTORNEY_MIN_COUNT} attorneys: {oor_low} | >{ATTORNEY_MAX_COUNT} attorneys: {oor_high}")
    print(f"  Bonus list (outliers, Hot/Warm only): {len(bonus_rows)} firms -> {BONUS_PATH}")


if __name__ == "__main__":
    main()
