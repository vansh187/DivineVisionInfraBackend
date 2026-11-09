"""Shared output helpers for the prod data-migration scripts: reports and
backups go to ./migration_reports/ (git-ignored - they contain customer
personal data)."""
import csv
import json
import os

REPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migration_reports")


def _path(name: str) -> str:
    os.makedirs(REPORT_DIR, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return os.path.join(REPORT_DIR, safe)


def save_text(name: str, content: str) -> str:
    path = _path(name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def save_json(name: str, data) -> str:
    path = _path(name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
    return path


def save_csv(name: str, header, rows) -> str:
    path = _path(name)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


def finish(report, name_prefix: str, apply: bool) -> int:
    rendered = report.render()
    print(rendered)
    path = save_text(f"{name_prefix}_{report.batch_id}{'' if apply else '_dryrun'}.txt", rendered)
    print(f"\nreport saved: {path}")
    if report.errors:
        print(f"\n{len(report.errors)} ERROR(s) - nothing was written. Fix them and run again.")
        return 1
    if not apply:
        print("\nDRY RUN - nothing was written. Re-run with --apply to make these changes.")
    return 0
