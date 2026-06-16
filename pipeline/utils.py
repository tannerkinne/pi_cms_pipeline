"""
Small shared helpers for the pipeline stage scripts: CSV read/write with
resume-by-key support, and a call counter so each stage can print a cheap
cost-awareness summary at the end of a run.
"""
import csv
import os

try:
    from dotenv import load_dotenv
    # Looks for .env in the project root (one level up from pipeline/).
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass  # python-dotenv not installed; fine if keys are exported in the shell instead.

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def read_csv_rows(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv_rows(path: str, rows: list, fieldnames: list, mode: str = "w"):
    write_header = mode == "w" or not os.path.exists(path)
    with open(path, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def append_csv_row(path: str, row: dict, fieldnames: list):
    write_csv_rows(path, [row], fieldnames, mode="a")


def already_processed_keys(path: str, key_field: str) -> set:
    """Used for resume support: read whatever's already in an output CSV
    and return the set of key values (e.g. domains) already handled, so a
    rerun after a crash or for extending the batch doesn't redo paid calls."""
    return {row[key_field] for row in read_csv_rows(path) if row.get(key_field)}


class CallCounter:
    """Tracks how many calls were made to each paid API during a run, so the
    end-of-stage summary gives a rough cost-awareness signal without needing
    to look anything up after the fact."""
    def __init__(self):
        self.counts = {}

    def tick(self, label: str, n: int = 1):
        self.counts[label] = self.counts.get(label, 0) + n

    def summary(self) -> str:
        if not self.counts:
            return "No API calls made."
        return " | ".join(f"{k}: {v}" for k, v in self.counts.items())
