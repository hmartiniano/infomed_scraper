"""Compare local and remote INFOMED SQLite scraping databases via SSH."""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from typing import Any, Dict

_REMOTE_QUERY_PY = """
import json, sqlite3, sys
db_path = sys.argv[1]
with sqlite3.connect(db_path, timeout=30.0) as conn:
    c = conn.cursor()
    tables = {
        r[0]
        for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    def _cnt(tbl):
        return (
            c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            if tbl in tables
            else 0
        )
    cft_tot, atc_tot = 380, 3193
    if "sweep_metrics" in tables:
        for name, tot in c.execute(
            "SELECT sweep_name, total_categories FROM sweep_metrics"
        ).fetchall():
            if "CFT" in name and tot:
                cft_tot = tot
            elif "ATC" in name and tot:
                atc_tot = tot
    res = {
        "total": c.execute("SELECT COUNT(*) FROM medicamentos").fetchone()[0],
        "as_count": c.execute(
            "SELECT COUNT(DISTINCT active_substance) FROM medicamentos "
            "WHERE active_substance IS NOT NULL AND length(trim(active_substance)) > 0"
        ).fetchone()[0],
        "names": c.execute(
            "SELECT COUNT(DISTINCT drug_name) FROM medicamentos "
            "WHERE drug_name IS NOT NULL"
        ).fetchone()[0],
        "rcms": c.execute(
            "SELECT COUNT(*) FROM medicamentos WHERE rcm_downloaded = 1"
        ).fetchone()[0],
        "fis": c.execute(
            "SELECT COUNT(*) FROM medicamentos WHERE fi_downloaded = 1"
        ).fetchone()[0],
        "both": c.execute(
            "SELECT COUNT(*) FROM medicamentos "
            "WHERE rcm_downloaded = 1 AND fi_downloaded = 1"
        ).fetchone()[0],
        "neither": c.execute(
            "SELECT COUNT(*) FROM medicamentos "
            "WHERE COALESCE(rcm_downloaded, 0) = 0 "
            "AND COALESCE(fi_downloaded, 0) = 0"
        ).fetchone()[0],
        "cft_done": _cnt("cft_progress"),
        "atc_done": _cnt("atc_progress"),
        "cft_total": cft_tot,
        "atc_total": atc_tot,
    }
    print(json.dumps(res))
"""


def _read_local_stats(local_db: str) -> Dict[str, Any]:
    """Read summary statistics from the local SQLite database."""
    with sqlite3.connect(local_db, timeout=30.0) as conn:
        c = conn.cursor()
        tables = {
            r[0]
            for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
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
        if "sweep_metrics" in tables:
            for name, tot in c.execute(
                "SELECT sweep_name, total_categories FROM sweep_metrics"
            ).fetchall():
                if "CFT" in name and tot:
                    cft_total = tot
                elif "ATC" in name and tot:
                    atc_total = tot

        return {
            "total": c.execute("SELECT COUNT(*) FROM medicamentos").fetchone()[0],
            "as_count": c.execute(
                "SELECT COUNT(DISTINCT active_substance) FROM medicamentos "
                "WHERE active_substance IS NOT NULL "
                "AND length(trim(active_substance)) > 0"
            ).fetchone()[0],
            "names": c.execute(
                "SELECT COUNT(DISTINCT drug_name) FROM medicamentos "
                "WHERE drug_name IS NOT NULL"
            ).fetchone()[0],
            "rcms": c.execute(
                "SELECT COUNT(*) FROM medicamentos WHERE rcm_downloaded = 1"
            ).fetchone()[0],
            "fis": c.execute(
                "SELECT COUNT(*) FROM medicamentos WHERE fi_downloaded = 1"
            ).fetchone()[0],
            "both": c.execute(
                "SELECT COUNT(*) FROM medicamentos "
                "WHERE rcm_downloaded = 1 AND fi_downloaded = 1"
            ).fetchone()[0],
            "neither": c.execute(
                "SELECT COUNT(*) FROM medicamentos "
                "WHERE COALESCE(rcm_downloaded, 0) = 0 "
                "AND COALESCE(fi_downloaded, 0) = 0"
            ).fetchone()[0],
            "cft_done": cft_done,
            "atc_done": atc_done,
            "cft_total": cft_total,
            "atc_total": atc_total,
        }


def _read_remote_stats(
    remote_host: str, remote_db: str, timeout: int
) -> Dict[str, Any]:
    """Query summary statistics from a remote SQLite database via SSH."""
    res = subprocess.run(
        ["ssh", remote_host, "python3", "-", remote_db],
        input=_REMOTE_QUERY_PY,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"Remote SSH query failed (exit {res.returncode}): {res.stderr.strip()}"
        )
    return json.loads(res.stdout.strip())


def main() -> None:
    """CLI entrypoint to compare local and remote INFOMED databases."""
    parser = argparse.ArgumentParser(
        description="Compare local and remote INFOMED SQLite run statistics."
    )
    parser.add_argument(
        "--local-db",
        default="medicamentos.db",
        help="Path to local SQLite database (default: medicamentos.db).",
    )
    parser.add_argument(
        "--remote-host",
        default="msc",
        help="SSH host alias for remote server (default: msc).",
    )
    parser.add_argument(
        "--remote-db",
        default="/home/ubuntu/work/infomed_scraper/medicamentos.db",
        help="Path to remote SQLite database.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=15,
        help="SSH command timeout in seconds (default: 15).",
    )
    args = parser.parse_args()

    if not os.path.exists(args.local_db):
        print(
            f"Error: Local database '{args.local_db}' not found.",
            file=sys.stderr,
        )
        sys.exit(1)

    local = _read_local_stats(args.local_db)
    try:
        remote = _read_remote_stats(args.remote_host, args.remote_db, args.timeout)
    except Exception as err:
        print(
            f"Error querying remote host '{args.remote_host}': {err}",
            file=sys.stderr,
        )
        sys.exit(1)

    print("=" * 90)
    print(
        f"{'METRIC':<30} {'LOCAL RUN':<20} "
        f"{'REMOTE (' + args.remote_host.upper() + ') RUN':<20} {'DIFFERENCE'}"
    )
    print("=" * 90)
    rows = [
        ("Total Presentations (Rows)", "total"),
        ("Distinct Active Substances", "as_count"),
        ("Distinct Drug / Brand Names", "names"),
        ("Downloaded SmPCs (RCMs)", "rcms"),
        ("Downloaded Leaflets (FIs)", "fis"),
        ("Both RCM + FI Downloaded", "both"),
        ("Neither Downloaded (Missing)", "neither"),
    ]
    for label, key in rows:
        diff = local[key] - remote[key]
        print(f"{label:<30} {local[key]:<20,} {remote[key]:<20,} {diff:+,}")

    print(
        f"{'CFT Categories Processed':<30} "
        f"{str(local['cft_done']) + ' / ' + str(local['cft_total']):<20} "
        f"{str(remote['cft_done']) + ' / ' + str(remote['cft_total']):<20} -"
    )
    print(
        f"{'ATC Categories Processed':<30} "
        f"{str(local['atc_done']) + ' / ' + str(local['atc_total']):<20} "
        f"{str(remote['atc_done']) + ' / ' + str(remote['atc_total']):<20} -"
    )
    print("=" * 90)


if __name__ == "__main__":
    main()
