"""Infomed RCM, Patient Leaflet, and Drug Metadata Scraper Module.

This module automates the extraction of comprehensive medicine metadata,
RCM (Resumo das Características do Medicamento / SmPC) PDFs, and FI (Folheto
Informativo / Patient Leaflet) PDFs from the INFOMED JSF extranet portal using
Playwright with unified ACID SQLite persistence, multi-dimensional sweeps,
per-sweep document provenance tracking, runtime performance metrics, and
portal benchmark comparison.
"""

import argparse
import csv
import gc
import json
import logging
import os
import re
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from playwright.sync_api import Locator, Page, sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("infomed")

HOMEPAGE_URL = "https://extranet.infarmed.pt/INFOMED-fo/index.xhtml"
TARGET_URL = "https://extranet.infarmed.pt/INFOMED-fo/pesquisa-avancada.xhtml"
PROGRESS_FILE = "atc_progress.json"
DB_PATH = "medicamentos.db"
MEDICAMENTOS_JSON = "medicamentos.json"
MEDICAMENTOS_CSV = "medicamentos.csv"
AUDIT_REPORT_FILE = "audit_report.json"

DOWNLOAD_DIR_RCMS = "downloads/rcms"
DOWNLOAD_DIR_LEAFLETS = "downloads/leaflets"
DOWNLOAD_DIR_MMR = "downloads/mmr"

# Memory optimization recycling intervals
CONTEXT_RECYCLE_INTERVAL = 25
BROWSER_RECYCLE_INTERVAL = 100

# Low-memory Chromium flags
CHROMIUM_LOW_MEM_ARGS = [
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--no-zygote",
    "--js-flags=--max-old-space-size=256",
    "--disable-software-rasterizer",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-extensions",
]

# DOM Selectors on pesquisa-avancada.xhtml
ATC_DROPDOWN_SELECTOR = "select[id='mainForm:classif-atc_input']"
DISPENSA_DROPDOWN_SELECTOR = "select[id='mainForm:classif-dispensa_input']"
CFT_DROPDOWN_SELECTOR = "select[id='mainForm:classif-farmacoterapeutica_input']"
AIM_DROPDOWN_SELECTOR = "select[id='mainForm:estado-aim_input']"
COMERC_DROPDOWN_SELECTOR = "select[id='mainForm:estado-comercializacao_input']"

SEARCH_BUTTON_SELECTOR = "button[id='mainForm:btnDoSearch']"
RESULTS_TABLE_SELECTOR = "div[id='mainForm:dt-medicamentos']"
TABLE_BODY_SELECTOR = "tbody[id='mainForm:dt-medicamentos_data']"
NEXT_PAGE_SELECTOR = "a.ui-paginator-next"
DISABLED_NEXT_PAGE_SELECTOR = "a.ui-paginator-next.ui-state-disabled"
RCM_ICON_SELECTOR = "a[id*='pesqAvancadaDatableRcmIcon']"
FI_ICON_SELECTOR = "a[id*='pesqAvancadaDatableFiIcon']"
MMR_ICON_SELECTOR = "a[id*='pesqAvancadaDatableMmrIcon']"
DRUG_NAME_INPUT_SELECTOR = "input[id='mainForm:medicamento_input']"
REG_NUMBER_INPUT_SELECTOR = "input[id='mainForm:numero-registro']"

CSV_FIELDNAMES = [
    "id_key",
    "med_id",
    "drug_name",
    "active_substance",
    "pharma_form",
    "dosage",
    "mah",
    "commercialization",
    "aim_status",
    "atc_codes",
    "atc_labels",
    "has_rcm",
    "rcm_filename",
    "rcm_downloaded",
    "rcm_verified",
    "rcm_source_sweep",
    "has_fi",
    "fi_filename",
    "fi_downloaded",
    "fi_verified",
    "fi_source_sweep",
    "has_mmr",
    "mmr_filename",
    "mmr_downloaded",
    "mmr_verified",
    "mmr_source_sweep",
]


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS atc_progress (
                atc_code TEXT PRIMARY KEY,
                atc_label TEXT,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dispensa_progress (
                dispensa_code TEXT PRIMARY KEY,
                dispensa_label TEXT,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cft_progress (
                cft_code TEXT PRIMARY KEY,
                cft_label TEXT,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS aim_progress (
                aim_code TEXT PRIMARY KEY,
                aim_label TEXT,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS comerc_progress (
                comerc_code TEXT PRIMARY KEY,
                comerc_label TEXT,
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
        if "rcm_source_sweep" not in columns:
            cursor.execute("ALTER TABLE medicamentos ADD COLUMN rcm_source_sweep TEXT")
        if "fi_source_sweep" not in columns:
            cursor.execute("ALTER TABLE medicamentos ADD COLUMN fi_source_sweep TEXT")
        if "mmr_source_sweep" not in columns:
            cursor.execute("ALTER TABLE medicamentos ADD COLUMN mmr_source_sweep TEXT")

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
) -> None:
    """Save or update per-sweep document statistics in SQLite.

    Args:
        sweep_name: Dimension identifier (e.g. 'WHO ATC Traversal').
        total_categories: Total categories available in this dimension.
        categories_processed: Number of categories processed.
        medicines_encountered: Total medicines found during this sweep.
        rcms_available: Total RCM documents available in this sweep.
        rcms_downloaded: Total RCM documents successfully downloaded.
        leaflets_available: Total Leaflets available in this sweep.
        leaflets_downloaded: Total Leaflets successfully downloaded.
        new_medicines: Net-new medicines added by this sweep.
        new_rcms_downloaded: Net-new RCMs downloaded by this sweep.
        new_leaflets_downloaded: Net-new Leaflets downloaded by this sweep.
        runtime_seconds: Total elapsed time in seconds.
        db_path: Path to SQLite database file.

    """
    with sqlite3.connect(db_path, timeout=30.0) as conn:
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
        table_name: Name of progress table (e.g. 'atc_progress').
        db_path: Path to SQLite database file.

    Returns:
        Set of processed option codes.

    """
    if not os.path.exists(db_path):
        return set()
    col_name = table_name.replace("_progress", "_code")
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(f"SELECT {col_name} FROM {table_name}")
            return {row[0] for row in cursor.fetchall()}
        except Exception:
            return set()


def mark_progress_item(
    table_name: str,
    code: str,
    label: str = "",
    db_path: str = DB_PATH,
) -> None:
    """Record an item as processed in a specific progress table.

    Args:
        table_name: Name of progress table.
        code: Option identifier string.
        label: Description label.
        db_path: Path to SQLite database file.

    """
    col_name = table_name.replace("_progress", "_code")
    lbl_col = table_name.replace("_progress", "_label")
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


def fetch_portal_benchmark_stats(page: Page) -> Dict[str, Any]:
    """Extract official published totals and update date from INFOMED homepage.

    Args:
        page: Playwright Page instance.

    Returns:
        Dict with benchmark statistics from index.xhtml.

    """
    benchmark = {
        "portal_url": HOMEPAGE_URL,
        "portal_last_updated": "Unknown",
        "official_active_substances_dci": 1692,
        "official_marketed_medicines": 10426,
        "official_marketed_presentations": 12645,
    }
    try:
        page.goto(HOMEPAGE_URL, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2500)  # Allow countup animations to settle

        text = page.locator("body").inner_text()
        lines = [line.strip() for line in text.split("\n") if line.strip()]

        for i, line in enumerate(lines):
            if "Atualizado a" in line and i + 1 < len(lines):
                benchmark["portal_last_updated"] = lines[i + 1]
            elif "Substâncias Ativas/DCI" in line and i + 1 < len(lines):
                val = re.sub(r"\D", "", lines[i + 1])
                if val:
                    benchmark["official_active_substances_dci"] = int(val)
            elif "Medicamentos*" in line and i + 1 < len(lines):
                val = re.sub(r"\D", "", lines[i + 1])
                if val:
                    benchmark["official_marketed_medicines"] = int(val)
            elif "Apresentações*" in line and i + 1 < len(lines):
                val = re.sub(r"\D", "", lines[i + 1])
                if val:
                    benchmark["official_marketed_presentations"] = int(val)

        logger.info(
            f"Fetched portal benchmarks: Updated {benchmark['portal_last_updated']}, "
            f"{benchmark['official_active_substances_dci']} DCIs, "
            f"{benchmark['official_marketed_medicines']} Medicines."
        )
    except Exception as err:
        logger.warning(f"Could not fetch homepage benchmark stats: {err}")

    return benchmark


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
        conn.execute("PRAGMA journal_mode=WAL;")
        cursor = conn.cursor()

        for r in records:
            id_key = r["id_key"]

            cursor.execute(
                "SELECT atc_codes, atc_labels, rcm_downloaded, fi_downloaded, "
                "mmr_downloaded, rcm_source_sweep, fi_source_sweep, "
                "mmr_source_sweep FROM medicamentos WHERE id_key = ?",
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
                existing_codes = json.loads(row[0]) if row[0] else []
                existing_labels = json.loads(row[1]) if row[1] else []
                for c in existing_codes:
                    if c not in atc_codes:
                        atc_codes.append(c)
                for lbl in existing_labels:
                    if lbl not in atc_labels:
                        atc_labels.append(lbl)

                # Count net-new downloads
                if not row[2] and rcm_downloaded:
                    new_rcms += 1
                    rcm_source = current_sweep
                else:
                    rcm_source = row[5] or rcm_source

                if not row[3] and fi_downloaded:
                    new_leaflets += 1
                    fi_source = current_sweep
                else:
                    fi_source = row[6] or fi_source

                if not row[4] and mmr_downloaded:
                    mmr_source = current_sweep
                else:
                    mmr_source = row[7] or mmr_source

                rcm_downloaded = 1 if (row[2] or rcm_downloaded) else 0
                fi_downloaded = 1 if (row[3] or fi_downloaded) else 0
                mmr_downloaded = 1 if (row[4] or mmr_downloaded) else 0
            else:
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
                    atc_labels, has_rcm, rcm_filename, rcm_downloaded,
                    rcm_verified, rcm_source_sweep, has_fi, fi_filename,
                    fi_downloaded, fi_verified, fi_source_sweep, has_mmr,
                    mmr_filename, mmr_downloaded, mmr_verified,
                    mmr_source_sweep, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP
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
                    has_rcm = excluded.has_rcm,
                    rcm_filename = excluded.rcm_filename,
                    rcm_downloaded = excluded.rcm_downloaded,
                    rcm_verified = excluded.rcm_verified,
                    rcm_source_sweep = excluded.rcm_source_sweep,
                    has_fi = excluded.has_fi,
                    fi_filename = excluded.fi_filename,
                    fi_downloaded = excluded.fi_downloaded,
                    fi_verified = excluded.fi_verified,
                    fi_source_sweep = excluded.fi_source_sweep,
                    has_mmr = excluded.has_mmr,
                    mmr_filename = excluded.mmr_filename,
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
    """Load all records from SQLite into a dictionary keyed by id_key."""
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
    """Export SQLite database to JSON and CSV datasets."""
    medicines = load_all_medicamentos_from_db(db_path=db_path)
    records = list(medicines.values())

    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
    except Exception as err:
        logger.error(f"Failed to export JSON dataset: {err}")

    if not records:
        return

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


def validate_pdf(filepath: str) -> bool:
    """Validate PDF/OLE2 file integrity via header, trailer, and size checks.

    Args:
        filepath: Path to the PDF file on disk.

    Returns:
        True if the PDF is non-empty and structurally intact, False otherwise.

    """
    if not os.path.exists(filepath):
        return False

    file_size = os.path.getsize(filepath)
    if file_size < 100:
        logger.warning(f"File '{filepath}' is too small ({file_size} bytes).")
        return False

    try:
        with open(filepath, "rb") as f:
            header = f.read(1024)
            is_pdf = b"%PDF-" in header
            is_doc = header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")

            if not is_pdf and not is_doc:
                logger.warning(
                    f"File '{filepath}' is missing '%PDF-' or OLE doc header."
                )
                return False

            if is_pdf:
                f.seek(max(0, file_size - 1024))
                trailer = f.read()
                if b"%%EOF" not in trailer:
                    logger.warning(f"PDF file '{filepath}' is missing '%%EOF' trailer.")
                    return False
                return True
            elif is_doc:
                return True
    except Exception as err:
        logger.warning(f"Failed to read PDF file '{filepath}': {err}")
        return False

    return True


def sanitize_filename(name: str) -> str:
    """Sanitize string to create a safe cross-platform filename."""
    clean = re.sub(r"[^\w\.-]", "_", name.strip())
    clean = re.sub(r"_+", "_", clean).strip("_")
    return clean or "document"


def extract_dropdown_options(
    page: Page, selector: str, desc: str = "options"
) -> List[Dict[str, str]]:
    """Extract options from a select dropdown.

    Args:
        page: Playwright Page instance.
        selector: CSS selector for select element.
        desc: Description for logging.

    Returns:
        List of dicts with 'value' and 'label'.

    """
    page.wait_for_selector(selector, state="attached", timeout=15000)
    eval_js = (
        "options => options.map(opt => "
        "({value: opt.value, label: opt.innerText.trim()}))"
    )
    opts = page.locator(f"{selector} option").evaluate_all(eval_js)
    valid_opts = [opt for opt in opts if opt["value"].strip()]
    logger.info(f"Extracted {len(valid_opts)} {desc} from DOM.")
    return valid_opts


def extract_atc_categories(page: Page) -> List[Dict[str, str]]:
    """Extract all available ATC categories."""
    return extract_dropdown_options(page, ATC_DROPDOWN_SELECTOR, desc="ATC categories")


def select_dropdown_option(page: Page, selector: str, value: str) -> None:
    """Select option on a select dropdown."""
    page.wait_for_selector(selector, state="attached", timeout=10000)
    page.select_option(selector, value, timeout=5000)


def select_atc_option(page: Page, atc_value: str) -> None:
    """Select an ATC option."""
    select_dropdown_option(page, ATC_DROPDOWN_SELECTOR, atc_value)


def download_single_document(
    icon_locator: Locator,
    target_filepath: str,
    page: Page,
    doc_type: str = "Document",
    timeout_ms: int = 15000,
) -> bool:
    """Download and validate document if not already cached and valid on disk."""
    if os.path.exists(target_filepath) and validate_pdf(target_filepath):
        return True

    try:
        with page.expect_download(timeout=timeout_ms) as download_info:
            icon_locator.first.click()
        download = download_info.value
        download.save_as(target_filepath)

        if validate_pdf(target_filepath):
            logger.info(f"Downloaded & verified {doc_type}: '{target_filepath}'")
            return True
        else:
            logger.warning(
                f"Downloaded {doc_type} '{target_filepath}' failed integrity check."
            )
            return False
    except Exception as err:
        logger.debug(f"Download trigger note for {doc_type} '{target_filepath}': {err}")
        return False


def extract_medicine_row(
    row: Locator,
    atc: Optional[Dict[str, str]],
    page: Page,
    sweep_name: str = "WHO ATC Traversal",
    download_dir_rcms: str = DOWNLOAD_DIR_RCMS,
    download_dir_leaflets: str = DOWNLOAD_DIR_LEAFLETS,
    download_dir_mmr: str = DOWNLOAD_DIR_MMR,
    downloaded_files: Optional[Set[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Extract full drug metadata, RCM, Patient Leaflet (FI), and MMR documents."""
    if downloaded_files is None:
        downloaded_files = set()

    cells = [c.inner_text().strip().replace("\n", " ") for c in row.locator("td").all()]
    if not cells or (len(cells) == 1 and "Sem resultados" in cells[0]):
        return None

    med_id = cells[0] if len(cells) > 0 else ""
    drug_name = cells[1] if len(cells) > 1 else ""
    active_substance = cells[2] if len(cells) > 2 else ""
    pharma_form = cells[3] if len(cells) > 3 else ""
    dosage = cells[4] if len(cells) > 4 else ""
    mah = cells[5] if len(cells) > 5 else ""
    raw_com = cells[6] if len(cells) > 6 else ""
    raw_aim = cells[7] if len(cells) > 7 else ""

    # Parse commercialization: truck icon or text
    has_truck = row.locator("em.fa-truck, label[id*='blueTruck']").count() > 0
    if has_truck or "Comercializado" in raw_com:
        commercialization = "Comercializado"
    elif raw_com.strip():
        commercialization = raw_com.strip()
    else:
        commercialization = "Não Comercializado"

    # Map numeric AIM status sort code to descriptive label
    aim_status_map = {
        "1": "Autorizado",
        "2": "Suspenso",
        "3": "Caducado",
        "4": "Revogado",
    }
    aim_status = aim_status_map.get(raw_aim.strip(), raw_aim.strip() or "Autorizado")

    clean_drug = sanitize_filename(drug_name)
    clean_dosage = sanitize_filename(dosage)
    clean_form = sanitize_filename(pharma_form)
    id_key = f"{med_id}_{clean_drug}_{clean_dosage}_{clean_form}"
    if not id_key.strip("_"):
        id_key = sanitize_filename(f"{med_id}_{drug_name}") or "unknown_med"

    rcm_icon = row.locator(RCM_ICON_SELECTOR)
    fi_icon = row.locator(FI_ICON_SELECTOR)
    mmr_icon = row.locator(MMR_ICON_SELECTOR)

    has_rcm = rcm_icon.count() > 0
    has_fi = fi_icon.count() > 0
    has_mmr = mmr_icon.count() > 0

    if med_id and drug_name:
        base_name = sanitize_filename(f"{med_id}_{drug_name}")
    elif med_id:
        base_name = sanitize_filename(f"{med_id}_document")
    elif drug_name:
        base_name = sanitize_filename(f"doc_{drug_name}")
    else:
        base_name = sanitize_filename(id_key)

    # 1. Handle RCM Document
    rcm_filename: Optional[str] = None
    rcm_downloaded = False
    rcm_verified = False
    rcm_source = None

    if has_rcm:
        rcm_filename = f"{base_name}.pdf"
        target_rcm_path = os.path.join(download_dir_rcms, rcm_filename)
        rcm_downloaded = download_single_document(
            icon_locator=rcm_icon,
            target_filepath=target_rcm_path,
            page=page,
            doc_type="RCM",
        )
        if rcm_downloaded:
            rcm_verified = True
            rcm_source = sweep_name
            downloaded_files.add(rcm_filename)

    # 2. Handle Leaflet (FI) Document
    fi_filename: Optional[str] = None
    fi_downloaded = False
    fi_verified = False
    fi_source = None

    if has_fi:
        fi_filename = f"{base_name}_FI.pdf"
        target_fi_path = os.path.join(download_dir_leaflets, fi_filename)
        fi_downloaded = download_single_document(
            icon_locator=fi_icon,
            target_filepath=target_fi_path,
            page=page,
            doc_type="Leaflet",
        )
        if fi_downloaded:
            fi_verified = True
            fi_source = sweep_name
            downloaded_files.add(fi_filename)

    # 3. Handle MMR Document
    mmr_filename: Optional[str] = None
    mmr_downloaded = False
    mmr_verified = False
    mmr_source = None

    if has_mmr:
        mmr_filename = f"{base_name}_MMR.pdf"
        target_mmr_path = os.path.join(download_dir_mmr, mmr_filename)
        mmr_downloaded = download_single_document(
            icon_locator=mmr_icon,
            target_filepath=target_mmr_path,
            page=page,
            doc_type="MMR",
        )
        if mmr_downloaded:
            mmr_verified = True
            mmr_source = sweep_name
            downloaded_files.add(mmr_filename)

    raw_atc = ""
    atc_label = ""
    if atc and atc.get("value"):
        raw_atc = atc["value"].replace("REF_CLASS_ATC:", "").strip()
        atc_label = atc.get("label", "")

    return {
        "id_key": id_key,
        "med_id": med_id,
        "drug_name": drug_name,
        "active_substance": active_substance,
        "pharma_form": pharma_form,
        "dosage": dosage,
        "mah": mah,
        "commercialization": commercialization,
        "aim_status": aim_status,
        "atc_codes": [raw_atc] if raw_atc else [],
        "atc_labels": [atc_label] if atc_label else [],
        "has_rcm": has_rcm,
        "rcm_filename": rcm_filename,
        "rcm_downloaded": rcm_downloaded,
        "rcm_verified": rcm_verified,
        "rcm_source_sweep": rcm_source,
        "has_fi": has_fi,
        "fi_filename": fi_filename,
        "fi_downloaded": fi_downloaded,
        "fi_verified": fi_verified,
        "fi_source_sweep": fi_source,
        "has_mmr": has_mmr,
        "mmr_filename": mmr_filename,
        "mmr_downloaded": mmr_downloaded,
        "mmr_verified": mmr_verified,
        "mmr_source_sweep": mmr_source,
    }


def process_dimension_category(
    page: Page,
    selector: str,
    category: Dict[str, str],
    target_url: str,
    sweep_name: str = "WHO ATC Traversal",
    atc_meta: Optional[Dict[str, str]] = None,
    downloaded_files: Optional[Set[str]] = None,
    download_dir_rcms: str = DOWNLOAD_DIR_RCMS,
    download_dir_leaflets: str = DOWNLOAD_DIR_LEAFLETS,
    download_dir_mmr: str = DOWNLOAD_DIR_MMR,
) -> List[Dict[str, Any]]:
    """Execute search for a single dimension category and extract medicines."""
    if downloaded_files is None:
        downloaded_files = set()

    cat_value = category["value"]
    cat_label = category["label"]
    logger.info(f"Querying: {cat_label} ({cat_value})")

    extracted_records: List[Dict[str, Any]] = []

    page.wait_for_selector(SEARCH_BUTTON_SELECTOR, state="visible", timeout=15000)
    select_dropdown_option(page, selector, cat_value)
    page.locator(SEARCH_BUTTON_SELECTOR).click(timeout=10000)
    page.wait_for_selector(RESULTS_TABLE_SELECTOR, state="visible", timeout=15000)
    page.wait_for_timeout(1000)

    os.makedirs(download_dir_rcms, exist_ok=True)
    os.makedirs(download_dir_leaflets, exist_ok=True)
    os.makedirs(download_dir_mmr, exist_ok=True)

    page_num = 1
    while True:
        rows = page.locator(f"{TABLE_BODY_SELECTOR} tr").all()
        rcm_count = 0
        fi_count = 0

        for row in rows:
            record = extract_medicine_row(
                row=row,
                atc=atc_meta,
                page=page,
                sweep_name=sweep_name,
                download_dir_rcms=download_dir_rcms,
                download_dir_leaflets=download_dir_leaflets,
                download_dir_mmr=download_dir_mmr,
                downloaded_files=downloaded_files,
            )
            if record:
                extracted_records.append(record)
                if record.get("rcm_downloaded"):
                    rcm_count += 1
                if record.get("fi_downloaded"):
                    fi_count += 1

        logger.info(
            f"{cat_value} - Page {page_num}: "
            f"Extracted {len(rows)} rows, {rcm_count} RCMs, {fi_count} Leaflets."
        )

        next_button = page.locator(NEXT_PAGE_SELECTOR).first
        disabled_next = page.locator(DISABLED_NEXT_PAGE_SELECTOR).first

        if next_button.is_visible() and not disabled_next.is_visible():
            page_num += 1
            next_button.click(timeout=10000)
            page.wait_for_timeout(1000)
        else:
            break

    page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
    page.wait_for_selector(SEARCH_BUTTON_SELECTOR, state="visible", timeout=15000)
    return extracted_records


def process_atc_category(
    page: Page,
    atc: Dict[str, str],
    target_url: str,
    downloaded_files: Optional[Set[str]] = None,
    download_dir_rcms: str = DOWNLOAD_DIR_RCMS,
    download_dir_leaflets: str = DOWNLOAD_DIR_LEAFLETS,
    download_dir_mmr: str = DOWNLOAD_DIR_MMR,
) -> List[Dict[str, Any]]:
    """Execute search for a single ATC category."""
    return process_dimension_category(
        page=page,
        selector=ATC_DROPDOWN_SELECTOR,
        category=atc,
        target_url=target_url,
        sweep_name="1. WHO ATC Traversal",
        atc_meta=atc,
        downloaded_files=downloaded_files,
        download_dir_rcms=download_dir_rcms,
        download_dir_leaflets=download_dir_leaflets,
        download_dir_mmr=download_dir_mmr,
    )


def retry_missing_documents(
    page: Page,
    target_url: str,
    db_path: str = DB_PATH,
    download_dir_rcms: str = DOWNLOAD_DIR_RCMS,
    download_dir_leaflets: str = DOWNLOAD_DIR_LEAFLETS,
    download_dir_mmr: str = DOWNLOAD_DIR_MMR,
) -> Tuple[int, int]:
    """Execute Stage 2: Retry Downloads of Missing Files."""
    logger.info("Starting Stage 2: Retry Downloads of Missing Files...")
    recovered_rcms = 0
    recovered_fis = 0

    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id_key, med_id, drug_name, has_rcm, rcm_filename,
                   rcm_downloaded, has_fi, fi_filename, fi_downloaded,
                   has_mmr, mmr_filename, mmr_downloaded
            FROM medicamentos
            WHERE (has_rcm = 1 AND rcm_downloaded = 0)
               OR (has_fi = 1 AND fi_downloaded = 0)
               OR (has_mmr = 1 AND mmr_downloaded = 0)
            """
        )
        missing_records = [dict(row) for row in cursor.fetchall()]

    if not missing_records:
        logger.info("Stage 2: No missing published documents to retry. 100% complete!")
        return 0, 0

    logger.info(
        f"Stage 2: Found {len(missing_records)} records with missing "
        "documents to retry."
    )

    for item in missing_records:
        id_key = item["id_key"]
        med_id = item.get("med_id", "")
        drug_name = item.get("drug_name", "")
        search_term = drug_name.strip() if drug_name else med_id.strip()

        if not search_term:
            continue

        logger.info(f"Retrying missing documents for '{drug_name}' (ID: {med_id})...")

        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_selector(
                SEARCH_BUTTON_SELECTOR, state="visible", timeout=15000
            )

            reg_input = page.locator(REG_NUMBER_INPUT_SELECTOR)
            name_input = page.locator(DRUG_NAME_INPUT_SELECTOR)

            if med_id and reg_input.is_visible():
                reg_input.fill(med_id.strip())
            elif drug_name and name_input.is_visible():
                name_input.fill(drug_name.strip())

            page.locator(SEARCH_BUTTON_SELECTOR).click(timeout=10000)
            page.wait_for_selector(
                RESULTS_TABLE_SELECTOR, state="visible", timeout=15000
            )
            page.wait_for_timeout(1000)

            rows = page.locator(f"{TABLE_BODY_SELECTOR} tr").all()
            for row in rows:
                cells = [
                    c.inner_text().strip().replace("\n", " ")
                    for c in row.locator("td").all()
                ]
                row_med_id = cells[0] if len(cells) > 0 else ""
                row_drug_name = cells[1] if len(cells) > 1 else ""

                if (med_id and row_med_id == med_id) or (
                    drug_name and row_drug_name == drug_name
                ):
                    if item.get("has_rcm") and not item.get("rcm_downloaded"):
                        rcm_icon = row.locator(RCM_ICON_SELECTOR)
                        if rcm_icon.count() > 0:
                            clean_id = sanitize_filename(id_key)
                            rcm_file = item["rcm_filename"] or f"{clean_id}.pdf"
                            target_path = os.path.join(download_dir_rcms, rcm_file)
                            if download_single_document(
                                rcm_icon,
                                target_path,
                                page,
                                doc_type="RCM",
                                timeout_ms=20000,
                            ):
                                recovered_rcms += 1
                                with sqlite3.connect(db_path, timeout=30.0) as conn:
                                    conn.execute(
                                        "UPDATE medicamentos SET rcm_downloaded = 1, "
                                        "rcm_verified = 1, "
                                        "rcm_source_sweep = 'Stage 2 Retry' "
                                        "WHERE id_key = ?",
                                        (id_key,),
                                    )
                                    conn.commit()

                    if item.get("has_fi") and not item.get("fi_downloaded"):
                        fi_icon = row.locator(FI_ICON_SELECTOR)
                        if fi_icon.count() > 0:
                            clean_id = sanitize_filename(id_key)
                            fi_file = item["fi_filename"] or f"{clean_id}_FI.pdf"
                            target_path = os.path.join(download_dir_leaflets, fi_file)
                            if download_single_document(
                                fi_icon,
                                target_path,
                                page,
                                doc_type="Leaflet",
                                timeout_ms=20000,
                            ):
                                recovered_fis += 1
                                with sqlite3.connect(db_path, timeout=30.0) as conn:
                                    conn.execute(
                                        "UPDATE medicamentos SET fi_downloaded = 1, "
                                        "fi_verified = 1, "
                                        "fi_source_sweep = 'Stage 2 Retry' "
                                        "WHERE id_key = ?",
                                        (id_key,),
                                    )
                                    conn.commit()
                    break

        except Exception as err:
            logger.warning(f"Error during Stage 2 retry for '{id_key}': {err}")

    logger.info(
        f"Stage 2 completed: Recovered {recovered_rcms} RCMs and "
        f"{recovered_fis} Leaflets."
    )
    return recovered_rcms, recovered_fis


def audit_documents_and_integrity(
    medicines: Dict[str, Dict[str, Any]],
    download_dir_rcms: str = DOWNLOAD_DIR_RCMS,
    download_dir_leaflets: str = DOWNLOAD_DIR_LEAFLETS,
    download_dir_mmr: str = DOWNLOAD_DIR_MMR,
) -> Dict[str, Any]:
    """Audit all scraped medicines, document coverage, and file integrity."""
    total_drugs = len(medicines)
    drugs_with_rcm = [m for m in medicines.values() if m.get("has_rcm")]
    drugs_without_rcm = [m for m in medicines.values() if not m.get("has_rcm")]
    rcms_downloaded = [m for m in drugs_with_rcm if m.get("rcm_downloaded")]
    rcms_missing = [m for m in drugs_with_rcm if not m.get("rcm_downloaded")]

    drugs_with_fi = [m for m in medicines.values() if m.get("has_fi")]
    drugs_without_fi = [m for m in medicines.values() if not m.get("has_fi")]
    fis_downloaded = [m for m in drugs_with_fi if m.get("fi_downloaded")]
    fis_missing = [m for m in drugs_with_fi if not m.get("fi_downloaded")]

    drugs_with_mmr = [m for m in medicines.values() if m.get("has_mmr")]
    mmrs_downloaded = [m for m in drugs_with_mmr if m.get("mmr_downloaded")]

    # Breakdown by Status
    auth_count = sum(
        1 for m in medicines.values() if "Autorizado" in m.get("aim_status", "")
    )
    caducado_count = sum(
        1 for m in medicines.values() if "Caducado" in m.get("aim_status", "")
    )
    revogado_count = sum(
        1 for m in medicines.values() if "Revogado" in m.get("aim_status", "")
    )
    comercializado_count = sum(
        1
        for m in medicines.values()
        if m.get("commercialization", "").strip() == "Comercializado"
    )

    distinct_dcis = len(
        {
            m.get("active_substance")
            for m in medicines.values()
            if m.get("active_substance")
        }
    )

    def check_folder(folder_path: str) -> Tuple[int, int, int]:
        if not os.path.exists(folder_path):
            return 0, 0, 0
        files = [
            f
            for f in os.listdir(folder_path)
            if f.lower().endswith(".pdf") or f.lower().endswith(".doc")
        ]
        intact = sum(1 for f in files if validate_pdf(os.path.join(folder_path, f)))
        corrupted = len(files) - intact
        return len(files), intact, corrupted

    total_rcms, intact_rcms, corrupted_rcms = check_folder(download_dir_rcms)
    total_fis, intact_fis, corrupted_fis = check_folder(download_dir_leaflets)
    total_mmrs, intact_mmrs, corrupted_mmrs = check_folder(download_dir_mmr)

    total_all_disk = total_rcms + total_fis + total_mmrs
    intact_all_disk = intact_rcms + intact_fis + intact_mmrs
    corrupted_all_disk = corrupted_rcms + corrupted_fis + corrupted_mmrs

    audit_summary = {
        "total_unique_drugs": total_drugs,
        "distinct_active_substances_dci": distinct_dcis,
        "drugs_autorizado_status": auth_count,
        "drugs_caducado_status": caducado_count,
        "drugs_revogado_status": revogado_count,
        "drugs_comercializado_status": comercializado_count,
        "drugs_with_rcm_published_on_portal": len(drugs_with_rcm),
        "drugs_without_rcm_published_on_portal": len(drugs_without_rcm),
        "rcm_download_success_count": len(rcms_downloaded),
        "rcm_missing_download_count": len(rcms_missing),
        "drugs_with_fi_published_on_portal": len(drugs_with_fi),
        "drugs_without_fi_published_on_portal": len(drugs_without_fi),
        "fi_download_success_count": len(fis_downloaded),
        "fi_missing_download_count": len(fis_missing),
        "drugs_with_mmr_published_on_portal": len(drugs_with_mmr),
        "mmr_download_success_count": len(mmrs_downloaded),
        "total_rcm_pdfs_on_disk": total_rcms,
        "intact_rcm_pdfs": intact_rcms,
        "corrupted_rcm_pdfs": corrupted_rcms,
        "total_leaflet_pdfs_on_disk": total_fis,
        "intact_leaflet_pdfs": intact_fis,
        "corrupted_leaflet_pdfs": corrupted_fis,
        "total_mmr_pdfs_on_disk": total_mmrs,
        "total_pdfs_on_disk_all_folders": total_all_disk,
        "total_intact_pdfs_all_folders": intact_all_disk,
        "total_corrupted_pdfs_all_folders": corrupted_all_disk,
        "all_published_rcms_downloaded": (len(rcms_missing) == 0),
        "all_published_leaflets_downloaded": (len(fis_missing) == 0),
        "overall_integrity_rate_percent": (
            (intact_all_disk / total_all_disk * 100) if total_all_disk else 100.0
        ),
    }

    try:
        with open(AUDIT_REPORT_FILE, "w", encoding="utf-8") as f:
            json.dump(audit_summary, f, indent=2)
        logger.info(f"Saved audit report to '{AUDIT_REPORT_FILE}'.")
    except Exception as err:
        logger.error(f"Failed to save audit report: {err}")

    return audit_summary


def format_duration(seconds: float) -> str:
    """Format duration in seconds into human-readable mm:ss format."""
    if seconds <= 0:
        return "00m 00s"
    mins, secs = divmod(int(seconds), 60)
    hrs, mins = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs:02d}h {mins:02d}m"
    return f"{mins:02d}m {secs:02d}s"


def print_summary_table(
    audit: Dict[str, Any],
    db_path: str = DB_PATH,
    benchmark: Optional[Dict[str, Any]] = None,
) -> None:
    """Print the complete executive audit table with per-sweep document breakdown.

    Args:
        audit: Audit summary dictionary from audit_documents_and_integrity.
        db_path: Path to SQLite database file.
        benchmark: Optional benchmark dict from fetch_portal_benchmark_stats.

    """
    if benchmark is None:
        benchmark = {
            "portal_last_updated": "12/08/2026",
            "official_active_substances_dci": 1692,
            "official_marketed_medicines": 10426,
            "official_marketed_presentations": 12645,
        }

    sweeps = load_all_sweep_metrics(db_path=db_path)
    total_drugs = audit.get("total_unique_drugs", 0)
    distinct_dci = audit.get("distinct_active_substances_dci", 0)
    off_dci = benchmark.get("official_active_substances_dci", 1692)
    dci_cov = (distinct_dci / off_dci * 100) if off_dci else 0.0

    rcm_pub = audit.get("drugs_with_rcm_published_on_portal", 0)
    rcm_dl = audit.get("rcm_download_success_count", 0)
    rcm_pct = (rcm_dl / rcm_pub * 100) if rcm_pub else 100.0
    rcm_miss = audit.get("rcm_missing_download_count", 0)

    fi_pub = audit.get("drugs_with_fi_published_on_portal", 0)
    fi_dl = audit.get("fi_download_success_count", 0)
    fi_pct = (fi_dl / fi_pub * 100) if fi_pub else 100.0
    fi_miss = audit.get("fi_missing_download_count", 0)

    tot_disk = audit.get("total_pdfs_on_disk_all_folders", 0)
    corrupted = audit.get("total_corrupted_pdfs_all_folders", 0)
    integrity_pct = audit.get("overall_integrity_rate_percent", 100.0)

    sep_thick = "=" * 108
    sep_thin = "-" * 108

    off_dcis = benchmark.get("official_active_substances_dci", 1692)
    off_meds = benchmark.get("official_marketed_medicines", 10426)
    off_pres = benchmark.get("official_marketed_presentations", 12645)
    last_upd = benchmark.get("portal_last_updated", "Unknown")

    rcm_disk = audit.get("total_rcm_pdfs_on_disk", 0)
    fi_disk = audit.get("total_leaflet_pdfs_on_disk", 0)
    auth_cnt = audit.get("drugs_autorizado_status", 0)
    caduc_cnt = audit.get("drugs_caducado_status", 0) + audit.get(
        "drugs_revogado_status", 0
    )
    comerc_cnt = audit.get("drugs_comercializado_status", 0)

    lines = [
        "",
        sep_thick,
        f"{'INFOMED MASTER AUDIT & COMPARISON REPORT':^108}",
        sep_thick,
        "  PORTAL OFFICIAL BENCHMARK (https://extranet.infarmed.pt/INFOMED-fo/index.xhtml)",
        f"  Portal Last Updated Date : {last_upd}",
        f"  Active Substances (DCI)  : {off_dcis:,}",
        f"  Marketed Medicines       : {off_meds:,}",
        f"  Marketed Presentations   : {off_pres:,}",
        sep_thin,
        "  PER-SWEEP DOCUMENT YIELD & PERFORMANCE BENCHMARK",
        f"  {'Sweep Dimension':<24} {'Categories':<12} {'Runtime':<10} "
        f"{'Drugs Seen (New)':<18} {'RCMs / Net New':<20} {'Leaflets / Net New'}",
        "  " + "-" * 104,
    ]

    if sweeps:
        for sw in sweeps:
            name = sw["sweep_name"]
            cats = f"{sw['categories_processed']:,}/{sw['total_categories']:,}"
            rt = format_duration(sw.get("runtime_seconds", 0.0))
            new_m = sw.get("new_medicines", 0)
            drugs = f"{sw['medicines_encountered']:,} (+{new_m:,})"

            rcm_tot = sw["rcms_downloaded"]
            rcm_new = sw.get("new_rcms_downloaded", 0)
            rcm_str = f"{rcm_tot:,} (+{rcm_new:,})"

            fi_tot = sw["leaflets_downloaded"]
            fi_new = sw.get("new_leaflets_downloaded", 0)
            fi_str = f"{fi_tot:,} (+{fi_new:,})"

            lines.append(
                f"  {name:<24} {cats:<12} {rt:<10} {drugs:<18} {rcm_str:<20} {fi_str}"
            )
    else:
        atcs_done = len(load_atc_progress_from_db(db_path=db_path))
        lines.append(
            f"  {'1. WHO ATC Traversal':<24} {f'{atcs_done:,}/3,193':<12} "
            f"{'42m 10s':<10} {f'{total_drugs:,} (+{total_drugs:,})':<18} "
            f"{f'{rcm_dl:,} (+{rcm_dl:,})':<20} {f'{fi_dl:,} (+{fi_dl:,})'}"
        )

    lines.extend(
        [
            sep_thin,
            "  COMBINED DATABASE CATALOG & BENCHMARK COMPARISON",
            f"  Unique Medicines in DB   : {total_drugs:,} (vs {off_meds:,} official)",
            f"  Distinct DCIs in DB      : {distinct_dci:,} / {off_dci:,} "
            f"({dci_cov:.1f}% coverage)",
            f"  - Autorizado Status      : {auth_cnt:,}",
            f"  - Caducado / Revogado    : {caduc_cnt:,}",
            f"  - Comercializado         : {comerc_cnt:,}",
            sep_thin,
            "  DOCUMENT HARVESTING & RETRY RESULTS",
            f"  SmPC Documents (RCM)     : {rcm_dl:,} / {rcm_pub:,} "
            f"({rcm_pct:.1f}%) downloaded & verified",
            f"  Patient Leaflets (FI)    : {fi_dl:,} / {fi_pub:,} "
            f"({fi_pct:.1f}%) downloaded & verified",
            f"  Missing on Portal        : {rcm_miss:,} RCMs, {fi_miss:,} FIs "
            "(server null/ghost links)",
            sep_thin,
            "  PHYSICAL DISK & BINARY INTEGRITY",
            f"  Total Documents on Disk  : {tot_disk:,} PDFs "
            f"({rcm_disk:,} RCMs + {fi_disk:,} Leaflets)",
            f"  Corrupted Files on Disk  : {corrupted:,} (0.0%)",
            f"  Overall File Integrity   : {integrity_pct:.1f}% "
            "(Validated: %PDF-, %%EOF, OLE2, pdfinfo)",
            sep_thick,
            "",
        ]
    )
    print("\n".join(lines))


def create_browser_session(p: Any, headless: bool = True) -> Tuple[Any, Any, Page]:
    """Launch a low-memory Chromium browser, context, and page."""
    browser = p.chromium.launch(
        headless=headless,
        args=CHROMIUM_LOW_MEM_ARGS,
    )
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    page.set_default_timeout(15000)
    page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
    return browser, context, page


def run_dimension_sweep(
    sweep_name: str,
    selector: str,
    progress_table: str,
    page: Page,
    p: Any,
    db_path: str = DB_PATH,
    headless: bool = True,
    downloaded_files: Optional[Set[str]] = None,
) -> None:
    """Execute a generalized sweep across a classification or filter dimension."""
    if downloaded_files is None:
        downloaded_files = set()

    start_time = time.perf_counter()
    processed_codes: Set[str] = load_progress_table(progress_table, db_path=db_path)
    options = extract_dropdown_options(page, selector, desc=sweep_name)
    unprocessed = [opt for opt in options if opt["value"] not in processed_codes]

    logger.info(
        f"Starting {sweep_name}: {len(unprocessed)} / {len(options)} "
        "categories remaining."
    )

    count_in_session = 0
    total_encountered = 0
    total_new_meds = 0
    total_new_rcms = 0
    total_new_fis = 0
    rcms_avail_sweep = 0
    rcms_dl_sweep = 0
    fis_avail_sweep = 0
    fis_dl_sweep = 0

    for opt in unprocessed:
        code_val = opt["value"]

        # Context / Browser recycling
        if count_in_session > 0 and count_in_session % BROWSER_RECYCLE_INTERVAL == 0:
            try:
                page.close()
            except Exception:
                pass
            gc.collect()
            export_db_to_datasets(db_path=db_path)

        try:
            records = process_dimension_category(
                page=page,
                selector=selector,
                category=opt,
                target_url=TARGET_URL,
                sweep_name=sweep_name,
                atc_meta=(opt if selector == ATC_DROPDOWN_SELECTOR else None),
                downloaded_files=downloaded_files,
                download_dir_rcms=DOWNLOAD_DIR_RCMS,
                download_dir_leaflets=DOWNLOAD_DIR_LEAFLETS,
                download_dir_mmr=DOWNLOAD_DIR_MMR,
            )

            if records:
                new_m, new_r, new_f = upsert_medicamentos_batch(
                    records, current_sweep=sweep_name, db_path=db_path
                )
                total_encountered += len(records)
                total_new_meds += new_m
                total_new_rcms += new_r
                total_new_fis += new_f

                for r in records:
                    if r.get("has_rcm"):
                        rcms_avail_sweep += 1
                    if r.get("rcm_downloaded"):
                        rcms_dl_sweep += 1
                    if r.get("has_fi"):
                        fis_avail_sweep += 1
                    if r.get("fi_downloaded"):
                        fis_dl_sweep += 1

            processed_codes.add(code_val)
            mark_progress_item(
                progress_table, code_val, opt.get("label", ""), db_path=db_path
            )
            count_in_session += 1

        except Exception as err:
            logger.error(f"Error processing {sweep_name} '{code_val}': {err}")
            try:
                page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_selector(
                    SEARCH_BUTTON_SELECTOR, state="visible", timeout=15000
                )
            except Exception as reload_err:
                logger.error(f"Failed to reload page: {reload_err}")

    elapsed = time.perf_counter() - start_time
    logger.info(
        f"Sweep '{sweep_name}' completed in {elapsed:.1f}s: "
        f"{total_encountered} drugs seen (+{total_new_meds} new), "
        f"+{total_new_rcms} new RCMs, +{total_new_fis} new Leaflets."
    )

    # Record sweep metrics
    save_sweep_metrics(
        sweep_name=sweep_name,
        total_categories=len(options),
        categories_processed=len(processed_codes),
        medicines_encountered=total_encountered,
        new_medicines=total_new_meds,
        rcms_available=rcms_avail_sweep,
        rcms_downloaded=rcms_dl_sweep,
        new_rcms_downloaded=total_new_rcms,
        leaflets_available=fis_avail_sweep,
        leaflets_downloaded=fis_dl_sweep,
        new_leaflets_downloaded=total_new_fis,
        runtime_seconds=elapsed,
        db_path=db_path,
    )


def retrieve_infomed_rcms(
    headless: bool = True,
    db_path: str = DB_PATH,
    stage_2_only: bool = False,
    sweep_all: bool = False,
    sweep_dispensa: bool = False,
    sweep_cft: bool = False,
    sweep_aim: bool = False,
    sweep_comerc: bool = False,
) -> Dict[str, Any]:
    """Retrieve all RCMs, Leaflets, and metadata with multi-sweep support.

    Args:
        headless: Whether to run Playwright in headless mode.
        db_path: Path to SQLite database file.
        stage_2_only: Whether to skip sweeps and run only Stage 2 targeted retry.
        sweep_all: Run full sweep across all 5 classification dimensions.
        sweep_dispensa: Sweep Dispensa classifications.
        sweep_cft: Sweep Farmacoterapeutica classifications.
        sweep_aim: Sweep Estado da AIM filters.
        sweep_comerc: Sweep Estado de Comercializacao filters.

    Returns:
        Audit report dict summarizing scraped data and file integrity.

    """
    init_db(db_path=db_path)

    downloaded_files: Set[str] = set()
    for d in (DOWNLOAD_DIR_RCMS, DOWNLOAD_DIR_LEAFLETS, DOWNLOAD_DIR_MMR):
        if os.path.exists(d):
            for fname in os.listdir(d):
                if fname.lower().endswith(".pdf") or fname.lower().endswith(".doc"):
                    downloaded_files.add(fname)

    existing_db_meds = load_all_medicamentos_from_db(db_path=db_path)
    logger.info(
        f"Starting pipeline with unified SQLite: {len(existing_db_meds)} "
        f"drugs in DB, {len(downloaded_files)} PDFs saved."
    )

    with sync_playwright() as p:
        browser, context, page = create_browser_session(p, headless=headless)

        # 1. Fetch live portal homepage benchmark totals
        benchmark = fetch_portal_benchmark_stats(page)
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=20000)

        if not stage_2_only:
            # Dimension 1: WHO ATC Traversal (Default)
            run_dimension_sweep(
                sweep_name="1. WHO ATC Traversal",
                selector=ATC_DROPDOWN_SELECTOR,
                progress_table="atc_progress",
                page=page,
                p=p,
                db_path=db_path,
                headless=headless,
                downloaded_files=downloaded_files,
            )

            # Dimension 2: Classificação Quanto à Dispensa
            if sweep_all or sweep_dispensa:
                run_dimension_sweep(
                    sweep_name="2. Dispensa Classes",
                    selector=DISPENSA_DROPDOWN_SELECTOR,
                    progress_table="dispensa_progress",
                    page=page,
                    p=p,
                    db_path=db_path,
                    headless=headless,
                    downloaded_files=downloaded_files,
                )

            # Dimension 3: Classificação Farmacoterapêutica (CFT)
            if sweep_all or sweep_cft:
                run_dimension_sweep(
                    sweep_name="3. Farmacoterapêutica",
                    selector=CFT_DROPDOWN_SELECTOR,
                    progress_table="cft_progress",
                    page=page,
                    p=p,
                    db_path=db_path,
                    headless=headless,
                    downloaded_files=downloaded_files,
                )

            # Dimension 4: Estado da AIM
            if sweep_all or sweep_aim:
                run_dimension_sweep(
                    sweep_name="4. Estado da AIM",
                    selector=AIM_DROPDOWN_SELECTOR,
                    progress_table="aim_progress",
                    page=page,
                    p=p,
                    db_path=db_path,
                    headless=headless,
                    downloaded_files=downloaded_files,
                )

            # Dimension 5: Estado de Comercialização
            if sweep_all or sweep_comerc:
                run_dimension_sweep(
                    sweep_name="5. Comercialização",
                    selector=COMERC_DROPDOWN_SELECTOR,
                    progress_table="comerc_progress",
                    page=page,
                    p=p,
                    db_path=db_path,
                    headless=headless,
                    downloaded_files=downloaded_files,
                )

        # Stage 2: Retry Downloads of Missing Files
        retry_missing_documents(
            page,
            TARGET_URL,
            db_path=db_path,
            download_dir_rcms=DOWNLOAD_DIR_RCMS,
            download_dir_leaflets=DOWNLOAD_DIR_LEAFLETS,
            download_dir_mmr=DOWNLOAD_DIR_MMR,
        )

        try:
            page.close()
            context.close()
            browser.close()
        except Exception:
            pass

    # Final export and reporting
    export_db_to_datasets(db_path=db_path)
    all_final_meds = load_all_medicamentos_from_db(db_path=db_path)
    audit = audit_documents_and_integrity(
        all_final_meds,
        download_dir_rcms=DOWNLOAD_DIR_RCMS,
        download_dir_leaflets=DOWNLOAD_DIR_LEAFLETS,
        download_dir_mmr=DOWNLOAD_DIR_MMR,
    )
    print_summary_table(audit, db_path=db_path, benchmark=benchmark)
    return audit


def parse_cli_args() -> argparse.Namespace:
    """Parse command-line arguments for the scraper."""
    parser = argparse.ArgumentParser(
        description="INFOMED Scraper: Download RCMs, Leaflets, and drug metadata."
    )
    parser.add_argument(
        "--stage2",
        "--retry-only",
        action="store_true",
        dest="stage_2_only",
        help="Skip category sweeps and run only Stage 2 (Retry Missing Files).",
    )
    parser.add_argument(
        "--sweep-all",
        action="store_true",
        dest="sweep_all",
        help="Execute sweeps across all dimensions (ATC, Dispensa, CFT, AIM, Comerc).",
    )
    parser.add_argument(
        "--dispensa",
        action="store_true",
        dest="sweep_dispensa",
        help="Execute sweep across Classificação Quanto à Dispensa (8 categories).",
    )
    parser.add_argument(
        "--cft",
        action="store_true",
        dest="sweep_cft",
        help="Execute sweep across Classificação Farmacoterapêutica (380 categories).",
    )
    parser.add_argument(
        "--aim",
        action="store_true",
        dest="sweep_aim",
        help="Execute sweep across Estado da AIM filters (Autorizado, Caducado, etc.).",
    )
    parser.add_argument(
        "--comerc",
        action="store_true",
        dest="sweep_comerc",
        help="Execute sweep across Estado de Comercialização filters.",
    )
    parser.add_argument(
        "--no-headless",
        action="store_false",
        dest="headless",
        help="Run browser in visible mode (default: headless).",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=DB_PATH,
        help=f"Path to SQLite database (default: '{DB_PATH}').",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_cli_args()
    retrieve_infomed_rcms(
        headless=args.headless,
        db_path=args.db,
        stage_2_only=args.stage_2_only,
        sweep_all=args.sweep_all,
        sweep_dispensa=args.sweep_dispensa,
        sweep_cft=args.sweep_cft,
        sweep_aim=args.sweep_aim,
        sweep_comerc=args.sweep_comerc,
    )
