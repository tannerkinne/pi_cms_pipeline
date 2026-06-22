"""
Stage 5 (optional) — Push the Stage 4 deliverable to a Google Sheet instead of
(or in addition to) the local CSVs. No paid API calls; uses a Google service
account to write data/final_output.csv → a "Leads" worksheet and data/bonus.csv
→ a "Bonus" worksheet on a spreadsheet you own.

This is opt-in and NOT part of the base install. The pipeline still always writes
the CSVs in Stage 4 — this tool just mirrors them to a shared sheet so the
deliverable lives where the team actually works.

SETUP (one time):
  1. pip install -r requirements-sheets.txt
  2. In Google Cloud Console: create a service account, enable the Google Sheets
     API, and download its JSON key.
  3. Share your target Google Sheet with the service account's email
     (client_email in the JSON) as an Editor.
  4. Set env vars (or pass the flags below):
       GOOGLE_SHEETS_CREDENTIALS_FILE=/path/to/service-account.json
       GOOGLE_SHEET_ID=<the long id from the sheet URL: /d/<THIS>/edit>

USAGE:
  python pipeline/05_export_to_sheets.py
  python pipeline/05_export_to_sheets.py --sheet-id <id> --credentials /path/key.json
  python pipeline/05_export_to_sheets.py --worksheet Leads --bonus-worksheet Bonus
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import DATA_DIR, read_csv_rows

FINAL_PATH = os.path.join(DATA_DIR, "final_output.csv")
BONUS_PATH = os.path.join(DATA_DIR, "bonus.csv")

# Column order written to the sheet — kept in sync with Stage 4's FIELDNAMES so
# the sheet header matches the CSV exactly.
FIELDNAMES = [
    "firm_name", "website", "city", "state", "plaintiff_pi_focus", "trial_focused",
    "est_attorneys", "cms_detected", "cms_confidence", "cms_evidence", "decision_maker",
    "hiring_signal", "hiring_signal_evidence", "data_source", "fit_score",
]


def _rows_to_matrix(rows: list, fieldnames: list) -> list:
    """Header row + one list-per-row, in fieldnames order. Empty string for any
    missing key so the grid stays rectangular (Sheets requires uniform width)."""
    matrix = [list(fieldnames)]
    for r in rows:
        matrix.append([str(r.get(col, "")) for col in fieldnames])
    return matrix


def _open_spreadsheet(sheet_id: str, credentials_file: str):
    """Authorize via service account and open the spreadsheet by id.
    gspread is imported lazily so the base pipeline needs no new dependency."""
    try:
        import gspread
    except ImportError:
        raise RuntimeError(
            "gspread is not installed. This is an optional extra:\n"
            "    pip install -r requirements-sheets.txt"
        )

    if not credentials_file:
        raise RuntimeError(
            "No service-account credentials given. Set GOOGLE_SHEETS_CREDENTIALS_FILE "
            "or pass --credentials /path/to/service-account.json"
        )
    if not os.path.exists(credentials_file):
        raise RuntimeError(f"Credentials file not found: {credentials_file}")
    if not sheet_id:
        raise RuntimeError(
            "No spreadsheet id given. Set GOOGLE_SHEET_ID or pass --sheet-id. "
            "It's the long token in the sheet URL: docs.google.com/spreadsheets/d/<ID>/edit"
        )

    gc = gspread.service_account(filename=credentials_file)
    try:
        return gc.open_by_key(sheet_id)
    except Exception as e:
        raise RuntimeError(
            f"Could not open spreadsheet {sheet_id}: {e}\n"
            "Make sure you shared the sheet with the service account's client_email "
            "(found in the credentials JSON) as an Editor."
        )


def _write_worksheet(spreadsheet, title: str, matrix: list):
    """Clear (or create) a worksheet and write the full matrix in one batch update."""
    rows_needed = max(len(matrix), 1)
    cols_needed = max((len(matrix[0]) if matrix else 1), 1)
    try:
        ws = spreadsheet.worksheet(title)
        ws.clear()
    except Exception:
        ws = spreadsheet.add_worksheet(title=title, rows=rows_needed + 10, cols=cols_needed)
    # Single batched update keeps us well under the Sheets per-minute write quota.
    ws.update(range_name="A1", values=matrix)
    # Bold the header row for readability.
    try:
        ws.format("A1:1", {"textFormat": {"bold": True}})
    except Exception:
        pass  # formatting is cosmetic — never fail the export over it
    return len(matrix) - 1  # data rows written (excludes header)


def main():
    parser = argparse.ArgumentParser(description="Export Stage 4 output to a Google Sheet.")
    parser.add_argument("--sheet-id", default=os.environ.get("GOOGLE_SHEET_ID", ""),
                        help="Spreadsheet id (defaults to $GOOGLE_SHEET_ID).")
    parser.add_argument("--credentials",
                        default=os.environ.get("GOOGLE_SHEETS_CREDENTIALS_FILE", ""),
                        help="Path to service-account JSON (defaults to "
                             "$GOOGLE_SHEETS_CREDENTIALS_FILE).")
    parser.add_argument("--worksheet", default="Leads",
                        help="Worksheet/tab name for the main list (default 'Leads').")
    parser.add_argument("--bonus-worksheet", default="Bonus",
                        help="Worksheet/tab name for the bonus list (default 'Bonus').")
    args = parser.parse_args()

    main_rows = read_csv_rows(FINAL_PATH)
    if not main_rows:
        print(f"No data in {FINAL_PATH} — run Stage 4 first.")
        return
    bonus_rows = read_csv_rows(BONUS_PATH)

    try:
        spreadsheet = _open_spreadsheet(args.sheet_id, args.credentials)
    except RuntimeError as e:
        # Setup/config problem (missing extra, creds, or sharing) — surface the
        # actionable message without a noisy traceback and signal failure.
        print(f"[!] Google Sheets export skipped: {e}")
        sys.exit(1)

    n_main = _write_worksheet(spreadsheet, args.worksheet, _rows_to_matrix(main_rows, FIELDNAMES))
    print(f"Wrote {n_main} firms to '{args.worksheet}' tab.")

    # Always (re)write the bonus tab so a previously-populated tab gets cleared
    # when bonus is now empty, keeping the sheet consistent with the CSVs.
    n_bonus = _write_worksheet(spreadsheet, args.bonus_worksheet, _rows_to_matrix(bonus_rows, FIELDNAMES))
    print(f"Wrote {n_bonus} firms to '{args.bonus_worksheet}' tab.")

    print(f"Done. https://docs.google.com/spreadsheets/d/{args.sheet_id}/edit")


if __name__ == "__main__":
    main()
