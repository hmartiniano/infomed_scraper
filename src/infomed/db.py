"""SQLite ACID persistence, progress tracking, and dataset export for INFOMED."""

import csv
import json
import logging
import os
import sqlite3
from typing import Any, Dict, List, Set, Tuple

from infomed.config import (
    ALLOWED_PROGRESS_TABLES,
    CSV_FIELDNAMES,
    DB_PATH,
    MEDICAMENTOS_CSV,
    MEDICAMENTOS_JSON,
    PROGRESS_FILE,
)

logger = logging.getLogger("infomed")


def _validate_progress_table(table_name: str) -> Tuple[str, str]:
    """Validate a progress table name against the allowlist and derive column names.

    Args:
        table_name: Progress table identifier.

    Returns:
        Tuple of (code_column_name, label_column_name).

    Raises:
        ValueError: If ``table_name`` is not in ``ALLOWED_PROGRESS_TABLES``.

    """
    if table_name not in ALLOWED_PROGRESS_TABLES:
        raise ValueError(f"Invalid progress table name: {table_name!r}")
    col_name = table_name.replace("_progress", "_code")
    lbl_col = table_name.replace("_progress", "_label")
    return col_name, lbl_col


def init_db(db_path: str = DB_PATH, auto_migrate: bool = True) -> None:
    """Initialize SQLite database with schema, progress tables, and metrics.

    Args:
        db_path: Path to SQLite database file.
        auto_migrate: Whether to auto-migrate existing JSON into empty database.

    """
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS medicamentos (
                id_key TEXT PRIMARY KEY,
                med_id TEXT,
                drug_name TEXT,
                active_substance TEXT,
                pharma_form TEXT,
                dosage TEXT,
                mah TEXT,
                commercialization TEXT,
                aim_status TEXT,
                atc_codes TEXT,
                atc_labels TEXT,
                first_seen_sweep TEXT,
                has_rcm INTEGER,
                rcm_filename TEXT,
                rcm_downloaded INTEGER,
                rcm_verified INTEGER,
                rcm_source_sweep TEXT,
                has_fi INTEGER,
                fi_filename TEXT,
                fi_downloaded INTEGER,
                fi_verified INTEGER,
                fi_source_sweep TEXT,
                has_mmr INTEGER,
                mmr_filename TEXT,
                mmr_downloaded INTEGER,
                mmr_verified INTEGER,
                mmr_source_sweep TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        # Create dimension progress tables
        for table_name in sorted(ALLOWED_PROGRESS_TABLES):
            col_name, lbl_col = _validate_progress_table(table_name)
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {table_name} (
                    {col_name} TEXT PRIMARY KEY,
                    {lbl_col} TEXT,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sweep_metrics (
                sweep_name TEXT PRIMARY KEY,
                total_categories INTEGER,
                categories_processed INTEGER,
                medicines_encountered INTEGER,
                new_medicines INTEGER DEFAULT 0,
                rcms_available INTEGER,
                rcms_downloaded INTEGER,
                new_rcms_downloaded INTEGER DEFAULT 0,
                leaflets_available INTEGER,
                leaflets_downloaded INTEGER,
                new_leaflets_downloaded INTEGER DEFAULT 0,
                runtime_seconds REAL DEFAULT 0.0,
                last_run TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()

        # Migrate any missing columns in existing tables
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(medicamentos)")
        columns = [row[1] for row in cursor.fetchall()]
        for col in (
            "first_seen_sweep",
            "rcm_source_sweep",
            "fi_source_sweep",
            "mmr_source_sweep",
        ):
            if col not in columns:
                cursor.execute(f"ALTER TABLE medicamentos ADD COLUMN {col} TEXT")

        cursor.execute("PRAGMA table_info(sweep_metrics)")
        sweep_cols = [row[1] for row in cursor.fetchall()]
        if "new_medicines" not in sweep_cols:
            cursor.execute(
                "ALTER TABLE sweep_metrics ADD COLUMN new_medicines INTEGER DEFAULT 0"
            )
        if "new_rcms_downloaded" not in sweep_cols:
            cursor.execute(
                "ALTER TABLE sweep_metrics ADD COLUMN new_rcms_downloaded INTEGER "
                "DEFAULT 0"
            )
        if "new_leaflets_downloaded" not in sweep_cols:
            cursor.execute(
                "ALTER TABLE sweep_metrics ADD COLUMN new_leaflets_downloaded "
                "INTEGER DEFAULT 0"
            )
        if "runtime_seconds" not in sweep_cols:
            cursor.execute(
                "ALTER TABLE sweep_metrics ADD COLUMN runtime_seconds REAL DEFAULT 0.0"
            )
        conn.commit()

        # 1. Auto-migrate medicamentos.json if table is empty
        cursor.execute("SELECT COUNT(*) FROM medicamentos")
        med_count = cursor.fetchone()[0]
        if (
            auto_migrate
            and db_path == DB_PATH
            and med_count == 0
            and os.path.exists(MEDICAMENTOS_JSON)
        ):
            try:
                with open(MEDICAMENTOS_JSON, "r", encoding="utf-8") as f:
                    json_records = json.load(f)
                if json_records:
                    logger.info(
                        f"Auto-migrating {len(json_records)} records from JSON "
                        f"into SQLite '{db_path}'..."
                    )
                    upsert_medicamentos_batch(json_records, db_path=db_path)
            except Exception as err:
                logger.warning(f"Failed to auto-migrate JSON to SQLite: {err}")

        # 2. Auto-migrate atc_progress.json if atc_progress table is empty
        cursor.execute("SELECT COUNT(*) FROM atc_progress")
        atc_count = cursor.fetchone()[0]
        if (
            auto_migrate
            and db_path == DB_PATH
            and atc_count == 0
            and os.path.exists(PROGRESS_FILE)
        ):
            try:
                with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    saved_atcs = data.get("processed_atcs", [])
                if saved_atcs:
                    logger.info(
                        f"Auto-migrating {len(saved_atcs)} processed ATCs from "
                        f"JSON into SQLite '{db_path}'..."
                    )
                    for code in saved_atcs:
                        cursor.execute(
                            "INSERT OR IGNORE INTO atc_progress "
                            "(atc_code, atc_label) VALUES (?, ?)",
                            (code, code),
                        )
                    conn.commit()
            except Exception as err:
                logger.warning(f"Failed to auto-migrate ATC progress: {err}")


def save_sweep_metrics(
    sweep_name: str,
    total_categories: int,
    categories_processed: int,
    medicines_encountered: int,
    rcms_available: int,
    rcms_downloaded: int,
    leaflets_available: int,
    leaflets_downloaded: int,
    new_medicines: int = 0,
    new_rcms_downloaded: int = 0,
    new_leaflets_downloaded: int = 0,
    runtime_seconds: float = 0.0,
    db_path: str = DB_PATH,
    accumulate: bool = False,
) -> None:
    """Save or update per-sweep document statistics in SQLite.

    Args:
        sweep_name: Display name of the sweep dimension.
        total_categories: Total categories present in the dropdown.
        categories_processed: Cumulative count of processed categories.
        medicines_encountered: Medicines encountered during this execution.
        rcms_available: RCMs available on portal during this execution.
        rcms_downloaded: RCMs downloaded during this execution.
        leaflets_available: Leaflets available on portal during this execution.
        leaflets_downloaded: Leaflets downloaded during this execution.
        new_medicines: Net-new medicines added in this execution.
        new_rcms_downloaded: Net-new RCMs downloaded in this execution.
        new_leaflets_downloaded: Net-new leaflets downloaded in this execution.
        runtime_seconds: Wall-clock runtime in seconds for this execution.
        db_path: Path to SQLite database file.
        accumulate: When True and an existing row exists, add execution yields
            and runtime onto previously stored totals (used on crash resume).

    """
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT medicines_encountered, new_medicines, rcms_available, "
            "rcms_downloaded, new_rcms_downloaded, leaflets_available, "
            "leaflets_downloaded, new_leaflets_downloaded, runtime_seconds "
            "FROM sweep_metrics WHERE sweep_name = ?",
            (sweep_name,),
        )
        existing = cursor.fetchone()

        if existing and medicines_encountered == 0 and existing[0] > 0:
            # Preserve existing non-zero historical data when re-running 0 remaining
            medicines_encountered = existing[0]
            new_medicines = existing[1]
            rcms_available = existing[2]
            rcms_downloaded = existing[3]
            new_rcms_downloaded = existing[4]
            leaflets_available = existing[5]
            leaflets_downloaded = existing[6]
            new_leaflets_downloaded = existing[7]
            runtime_seconds = existing[8]
        elif existing and accumulate:
            medicines_encountered += existing[0] or 0
            new_medicines += existing[1] or 0
            rcms_available += existing[2] or 0
            rcms_downloaded += existing[3] or 0
            new_rcms_downloaded += existing[4] or 0
            leaflets_available += existing[5] or 0
            leaflets_downloaded += existing[6] or 0
            new_leaflets_downloaded += existing[7] or 0
            runtime_seconds += existing[8] or 0.0

        conn.execute(
            """
            INSERT INTO sweep_metrics (
                sweep_name, total_categories, categories_processed,
                medicines_encountered, new_medicines, rcms_available,
                rcms_downloaded, new_rcms_downloaded, leaflets_available,
                leaflets_downloaded, new_leaflets_downloaded,
                runtime_seconds, last_run
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(sweep_name) DO UPDATE SET
                total_categories = excluded.total_categories,
                categories_processed = excluded.categories_processed,
                medicines_encountered = excluded.medicines_encountered,
                new_medicines = excluded.new_medicines,
                rcms_available = excluded.rcms_available,
                rcms_downloaded = excluded.rcms_downloaded,
                new_rcms_downloaded = excluded.new_rcms_downloaded,
                leaflets_available = excluded.leaflets_available,
                leaflets_downloaded = excluded.leaflets_downloaded,
                new_leaflets_downloaded = excluded.new_leaflets_downloaded,
                runtime_seconds = excluded.runtime_seconds,
                last_run = CURRENT_TIMESTAMP;
            """,
            (
                sweep_name,
                total_categories,
                categories_processed,
                medicines_encountered,
                new_medicines,
                rcms_available,
                rcms_downloaded,
                new_rcms_downloaded,
                leaflets_available,
                leaflets_downloaded,
                new_leaflets_downloaded,
                runtime_seconds,
            ),
        )
        conn.commit()


def load_all_sweep_metrics(db_path: str = DB_PATH) -> List[Dict[str, Any]]:
    """Load all recorded sweep metrics from SQLite.

    Args:
        db_path: Path to SQLite database file.

    Returns:
        List of dictionaries with sweep statistics.

    """
    if not os.path.exists(db_path):
        return []
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sweep_metrics ORDER BY sweep_name")
        return [dict(row) for row in cursor.fetchall()]


def load_progress_table(table_name: str, db_path: str = DB_PATH) -> Set[str]:
    """Load the set of processed codes from a specific progress table.

    Args:
        table_name: Name of progress table (must be in ``ALLOWED_PROGRESS_TABLES``).
        db_path: Path to SQLite database file.

    Returns:
        Set of processed option codes.

    Raises:
        ValueError: If ``table_name`` is not an allowed progress table.

    """
    col_name, _ = _validate_progress_table(table_name)
    if not os.path.exists(db_path):
        return set()
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(f"SELECT {col_name} FROM {table_name}")
            return {row[0] for row in cursor.fetchall()}
        except sqlite3.Error as err:
            logger.debug(f"Could not read progress table '{table_name}': {err}")
            return set()


def mark_progress_item(
    table_name: str,
    code: str,
    label: str = "",
    db_path: str = DB_PATH,
) -> None:
    """Record an item as processed in a specific progress table.

    Args:
        table_name: Name of progress table (must be in ``ALLOWED_PROGRESS_TABLES``).
        code: Option identifier string.
        label: Description label.
        db_path: Path to SQLite database file.

    Raises:
        ValueError: If ``table_name`` is not an allowed progress table.

    """
    col_name, lbl_col = _validate_progress_table(table_name)
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.execute(
            f"""
            INSERT OR REPLACE INTO {table_name} ({col_name}, {lbl_col}, processed_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
            (code, label),
        )
        conn.commit()


def load_atc_progress_from_db(db_path: str = DB_PATH) -> Set[str]:
    """Load the set of processed ATC codes from SQLite."""
    return load_progress_table("atc_progress", db_path=db_path)


def mark_atc_processed_in_db(
    atc_code: str,
    atc_label: str = "",
    db_path: str = DB_PATH,
) -> None:
    """Record an ATC category as processed in SQLite."""
    mark_progress_item("atc_progress", atc_code, atc_label, db_path=db_path)


def upsert_medicamentos_batch(
    records: List[Dict[str, Any]],
    current_sweep: str = "WHO ATC Traversal",
    db_path: str = DB_PATH,
) -> Tuple[int, int, int]:
    """Insert or update a batch of medicine records in SQLite atomically.

    Args:
        records: List of medicine record dictionaries.
        current_sweep: Identifier of the current sweep for provenance tagging.
        db_path: Path to SQLite database file.

    Returns:
        Tuple of (new_meds_count, new_rcms_count, new_leaflets_count).

    """
    if not records:
        return 0, 0, 0

    new_meds = 0
    new_rcms = 0
    new_leaflets = 0

    with sqlite3.connect(db_path, timeout=30.0) as conn:
        cursor = conn.cursor()

        for r in records:
            id_key = r["id_key"]

            cursor.execute(
                "SELECT atc_codes, atc_labels, rcm_downloaded, fi_downloaded, "
                "mmr_downloaded, rcm_source_sweep, fi_source_sweep, "
                "mmr_source_sweep, first_seen_sweep FROM medicamentos WHERE id_key = ?",
                (id_key,),
            )
            row = cursor.fetchone()

            atc_codes = list(r.get("atc_codes", []))
            atc_labels = list(r.get("atc_labels", []))
            rcm_downloaded = 1 if r.get("rcm_downloaded") else 0
            fi_downloaded = 1 if r.get("fi_downloaded") else 0
            mmr_downloaded = 1 if r.get("mmr_downloaded") else 0

            rcm_source = r.get("rcm_source_sweep") or (
                current_sweep if rcm_downloaded else None
            )
            fi_source = r.get("fi_source_sweep") or (
                current_sweep if fi_downloaded else None
            )
            mmr_source = r.get("mmr_source_sweep") or (
                current_sweep if mmr_downloaded else None
            )

            if row:
                first_seen = row[8] or current_sweep
                existing_codes = json.loads(row[0]) if row[0] else []
                existing_labels = json.loads(row[1]) if row[1] else []
                for code in existing_codes:
                    if code not in atc_codes:
                        atc_codes.append(code)
                for lbl in existing_labels:
                    if lbl not in atc_labels:
                        atc_labels.append(lbl)

                # Count net-new downloads while preserving explicit provenance
                if not row[2] and rcm_downloaded:
                    new_rcms += 1
                    rcm_source = r.get("rcm_source_sweep") or current_sweep
                else:
                    rcm_source = row[5] or rcm_source

                if not row[3] and fi_downloaded:
                    new_leaflets += 1
                    fi_source = r.get("fi_source_sweep") or current_sweep
                else:
                    fi_source = row[6] or fi_source

                if not row[4] and mmr_downloaded:
                    mmr_source = r.get("mmr_source_sweep") or current_sweep
                else:
                    mmr_source = row[7] or mmr_source

                rcm_downloaded = 1 if (row[2] or rcm_downloaded) else 0
                fi_downloaded = 1 if (row[3] or fi_downloaded) else 0
                mmr_downloaded = 1 if (row[4] or mmr_downloaded) else 0
            else:
                first_seen = current_sweep
                new_meds += 1
                if rcm_downloaded:
                    new_rcms += 1
                if fi_downloaded:
                    new_leaflets += 1

            cursor.execute(
                """
                INSERT INTO medicamentos (
                    id_key, med_id, drug_name, active_substance, pharma_form,
                    dosage, mah, commercialization, aim_status, atc_codes,
                    atc_labels, first_seen_sweep, has_rcm, rcm_filename,
                    rcm_downloaded, rcm_verified, rcm_source_sweep, has_fi,
                    fi_filename, fi_downloaded, fi_verified, fi_source_sweep,
                    has_mmr, mmr_filename, mmr_downloaded, mmr_verified,
                    mmr_source_sweep, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP
                )
                ON CONFLICT(id_key) DO UPDATE SET
                    med_id = excluded.med_id,
                    drug_name = excluded.drug_name,
                    active_substance = excluded.active_substance,
                    pharma_form = excluded.pharma_form,
                    dosage = excluded.dosage,
                    mah = excluded.mah,
                    commercialization = excluded.commercialization,
                    aim_status = excluded.aim_status,
                    atc_codes = excluded.atc_codes,
                    atc_labels = excluded.atc_labels,
                    first_seen_sweep = COALESCE(
                        medicamentos.first_seen_sweep, excluded.first_seen_sweep
                    ),
                    has_rcm = MAX(COALESCE(medicamentos.has_rcm, 0), excluded.has_rcm),
                    rcm_filename = COALESCE(
                        excluded.rcm_filename, medicamentos.rcm_filename
                    ),
                    rcm_downloaded = excluded.rcm_downloaded,
                    rcm_verified = excluded.rcm_verified,
                    rcm_source_sweep = excluded.rcm_source_sweep,
                    has_fi = MAX(COALESCE(medicamentos.has_fi, 0), excluded.has_fi),
                    fi_filename = COALESCE(
                        excluded.fi_filename, medicamentos.fi_filename
                    ),
                    fi_downloaded = excluded.fi_downloaded,
                    fi_verified = excluded.fi_verified,
                    fi_source_sweep = excluded.fi_source_sweep,
                    has_mmr = MAX(COALESCE(medicamentos.has_mmr, 0), excluded.has_mmr),
                    mmr_filename = COALESCE(
                        excluded.mmr_filename, medicamentos.mmr_filename
                    ),
                    mmr_downloaded = excluded.mmr_downloaded,
                    mmr_verified = excluded.mmr_verified,
                    mmr_source_sweep = excluded.mmr_source_sweep,
                    updated_at = CURRENT_TIMESTAMP;
                """,
                (
                    id_key,
                    r.get("med_id", ""),
                    r.get("drug_name", ""),
                    r.get("active_substance", ""),
                    r.get("pharma_form", ""),
                    r.get("dosage", ""),
                    r.get("mah", ""),
                    r.get("commercialization", ""),
                    r.get("aim_status", ""),
                    json.dumps(atc_codes, ensure_ascii=False),
                    json.dumps(atc_labels, ensure_ascii=False),
                    first_seen,
                    1 if r.get("has_rcm") else 0,
                    r.get("rcm_filename"),
                    rcm_downloaded,
                    rcm_downloaded,
                    rcm_source,
                    1 if r.get("has_fi") else 0,
                    r.get("fi_filename"),
                    fi_downloaded,
                    fi_downloaded,
                    fi_source,
                    1 if r.get("has_mmr") else 0,
                    r.get("mmr_filename"),
                    mmr_downloaded,
                    mmr_downloaded,
                    mmr_source,
                ),
            )
        conn.commit()

    return new_meds, new_rcms, new_leaflets


def load_all_medicamentos_from_db(
    db_path: str = DB_PATH,
) -> Dict[str, Dict[str, Any]]:
    """Load all records from SQLite into a dictionary keyed by id_key.

    Args:
        db_path: Path to SQLite database file.

    Returns:
        Dictionary of medicine records keyed by ``id_key``.

    """
    if not os.path.exists(db_path):
        return {}

    medicines = {}
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM medicamentos ORDER BY id_key")
        for row in cursor.fetchall():
            d = dict(row)
            d["atc_codes"] = json.loads(d["atc_codes"]) if d["atc_codes"] else []
            d["atc_labels"] = json.loads(d["atc_labels"]) if d["atc_labels"] else []
            d["has_rcm"] = bool(d["has_rcm"])
            d["rcm_downloaded"] = bool(d["rcm_downloaded"])
            d["rcm_verified"] = bool(d["rcm_verified"])
            d["has_fi"] = bool(d["has_fi"])
            d["fi_downloaded"] = bool(d["fi_downloaded"])
            d["fi_verified"] = bool(d["fi_verified"])
            d["has_mmr"] = bool(d["has_mmr"])
            d["mmr_downloaded"] = bool(d["mmr_downloaded"])
            d["mmr_verified"] = bool(d["mmr_verified"])
            medicines[d["id_key"]] = d
    return medicines


def export_db_to_datasets(
    db_path: str = DB_PATH,
    json_path: str = MEDICAMENTOS_JSON,
    csv_path: str = MEDICAMENTOS_CSV,
) -> None:
    """Export SQLite database to JSON and CSV datasets.

    Args:
        db_path: Path to SQLite database file.
        json_path: Output path for JSON export.
        csv_path: Output path for CSV export.

    """
    medicines = load_all_medicamentos_from_db(db_path=db_path)
    records = list(medicines.values())

    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
    except Exception as err:
        logger.error(f"Failed to export JSON dataset: {err}")

    try:
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES, extrasaction="ignore")
            writer.writeheader()
            for r in records:
                row = dict(r)
                if isinstance(row.get("atc_codes"), list):
                    row["atc_codes"] = "; ".join(row["atc_codes"])
                if isinstance(row.get("atc_labels"), list):
                    row["atc_labels"] = "; ".join(row["atc_labels"])
                writer.writerow(row)
    except Exception as err:
        logger.error(f"Failed to export CSV dataset: {err}")
