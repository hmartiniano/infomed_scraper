"""Inspect live INFOMED pipeline process, SQLite counts, and recent logs."""

import argparse
import os
import sqlite3
import subprocess
from collections import deque
from typing import Optional


def _get_running_process_summary(pattern: str = "infomed.main") -> Optional[str]:
    """Return a concise status string if a scraper process is running."""
    try:
        res = subprocess.run(
            ["pgrep", "-af", pattern],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        lines = [
            ln.strip()
            for ln in res.stdout.splitlines()
            if ln.strip() and "pgrep" not in ln
        ]
        if lines:
            return f"RUNNING ({len(lines)} process(es): {lines[0]})"
    except Exception:
        pass
    return None


def _tail_lines(filepath: str, num_lines: int) -> list[str]:
    """Return the last ``num_lines`` lines from a UTF-8 log file."""
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        return [line.rstrip("\n") for line in deque(f, maxlen=num_lines)]


def main() -> None:
    """CLI entrypoint for inspecting local/remote INFOMED pipeline status."""
    parser = argparse.ArgumentParser(
        description="Display INFOMED scraper process, database, and log status."
    )
    parser.add_argument(
        "--db",
        default="/home/ubuntu/work/infomed_scraper/medicamentos.db",
        help="Path to SQLite database file.",
    )
    parser.add_argument(
        "--log",
        default="/home/ubuntu/work/infomed_scraper/pipeline.log",
        help="Path to pipeline log file.",
    )
    parser.add_argument(
        "--lines",
        type=int,
        default=10,
        help="Number of trailing log lines to print (default: 10).",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("             INFOMED PIPELINE REMOTE STATUS")
    print("=" * 60)

    proc_summary = _get_running_process_summary()
    if proc_summary:
        print(f"Process Status             : {proc_summary}")
    else:
        print("Process Status             : COMPLETED / STOPPED")

    if os.path.exists(args.db):
        with sqlite3.connect(args.db, timeout=30.0) as conn:
            c = conn.cursor()
            tables = {
                r[0]
                for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }

            total_records = c.execute("SELECT COUNT(*) FROM medicamentos").fetchone()[0]
            distinct_as = c.execute(
                "SELECT COUNT(DISTINCT active_substance) FROM medicamentos "
                "WHERE active_substance IS NOT NULL "
                "AND LENGTH(TRIM(active_substance)) > 0"
            ).fetchone()[0]
            distinct_names = c.execute(
                "SELECT COUNT(DISTINCT drug_name) FROM medicamentos "
                "WHERE drug_name IS NOT NULL"
            ).fetchone()[0]
            rcm_count = c.execute(
                "SELECT COUNT(*) FROM medicamentos WHERE rcm_downloaded = 1"
            ).fetchone()[0]
            fi_count = c.execute(
                "SELECT COUNT(*) FROM medicamentos WHERE fi_downloaded = 1"
            ).fetchone()[0]

            cft_done = (
                c.execute("SELECT COUNT(*) FROM cft_progress").fetchone()[0]
                if "cft_progress" in tables
                else 0
            )
            atc_done = (
                c.execute("SELECT COUNT(*) FROM atc_progress").fetchone()[0]
                if "atc_progress" in tables
                else 0
            )

            cft_total, atc_total = 380, 3193
            metrics = []
            if "sweep_metrics" in tables:
                metrics = c.execute(
                    "SELECT sweep_name, categories_processed, total_categories, "
                    "medicines_encountered, rcms_downloaded, leaflets_downloaded, "
                    "runtime_seconds FROM sweep_metrics"
                ).fetchall()
                for m in metrics:
                    if "CFT" in m[0] and m[2]:
                        cft_total = m[2]
                    elif "ATC" in m[0] and m[2]:
                        atc_total = m[2]

        cft_pct = (cft_done / cft_total * 100) if cft_total else 0.0
        atc_pct = (atc_done / atc_total * 100) if atc_total else 0.0

        print(f"Total Presentations in DB  : {total_records:,}")
        print(f"Distinct Active Substances : {distinct_as:,}")
        print(f"Distinct Drug Names        : {distinct_names:,}")
        print(f"Downloaded SmPCs / RCMs    : {rcm_count:,}")
        print(f"Downloaded Package Leaflets: {fi_count:,}")
        print(
            f"CFT Sweep Progress (Dim 1) : {cft_done} / {cft_total} "
            f"categories ({cft_pct:.1f}%)"
        )
        print(
            f"ATC Sweep Progress (Dim 2) : {atc_done} / {atc_total:,} "
            f"categories ({atc_pct:.1f}%)"
        )

        if metrics:
            print("-" * 60)
            print("Completed Sweeps:")
            for m in metrics:
                print(
                    f"  - {m[0]}: {m[1]}/{m[2]} categories, {m[3]:,} medicines, "
                    f"{m[4]:,} RCMs, {m[5]:,} Leaflets in {m[6] / 60:.1f}m"
                )
    else:
        print(f"Database file '{args.db}' not found.")

    print("-" * 60)
    print("Recent Pipeline Logs:")
    if os.path.exists(args.log):
        for line in _tail_lines(args.log, args.lines):
            print(f"  {line}")
    print("=" * 60)


if __name__ == "__main__":
    main()
