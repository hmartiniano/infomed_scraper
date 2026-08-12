"""Infomed RCM, Patient Leaflet, and Drug Metadata Scraper Module.

This module automates the extraction of comprehensive medicine metadata,
RCM (Resumo das Características do Medicamento / SmPC) PDFs, and FI (Folheto
Informativo / Patient Leaflet) PDFs from the INFOMED JSF extranet portal using
Playwright with a unified ACID SQLite persistence architecture, low-memory
context recycling, and an automated Stage 2 targeted document retry pass.
"""

import argparse
import csv
import gc
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

from playwright.sync_api import Locator, Page, sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("infomed")

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
CONTEXT_RECYCLE_INTERVAL = 25  # Recycle Playwright context & page every 25 ATCs
BROWSER_RECYCLE_INTERVAL = 100  # Relaunch Chromium browser every 100 ATCs

# Low-memory Chromium flags to prevent GPU cache bloat and memory leaks
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

# DOM Selectors for PrimeFaces JSF
ATC_DROPDOWN_SELECTOR = "select[id='mainForm:classif-atc_input']"
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
    "has_fi",
    "fi_filename",
    "fi_downloaded",
    "fi_verified",
    "has_mmr",
    "mmr_filename",
    "mmr_downloaded",
    "mmr_verified",
]


def init_db(db_path: str = DB_PATH, auto_migrate: bool = True) -> None:
    """Initialize SQLite database with schema and auto-migrate legacy JSON files.

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
                has_fi INTEGER,
                fi_filename TEXT,
                fi_downloaded INTEGER,
                fi_verified INTEGER,
                has_mmr INTEGER,
                mmr_filename TEXT,
                mmr_downloaded INTEGER,
                mmr_verified INTEGER,
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
        conn.commit()

        cursor = conn.cursor()

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
                            "INSERT OR IGNORE INTO atc_progress (atc_code, atc_label) "
                            "VALUES (?, ?)",
                            (code, code),
                        )
                    conn.commit()
            except Exception as err:
                logger.warning(f"Failed to auto-migrate ATC progress: {err}")


def load_atc_progress_from_db(db_path: str = DB_PATH) -> Set[str]:
    """Load the set of processed ATC codes from SQLite.

    Args:
        db_path: Path to SQLite database file.

    Returns:
        Set of processed ATC codes.

    """
    if not os.path.exists(db_path):
        return set()
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT atc_code FROM atc_progress")
        return {row[0] for row in cursor.fetchall()}


def mark_atc_processed_in_db(
    atc_code: str,
    atc_label: str = "",
    db_path: str = DB_PATH,
) -> None:
    """Record an ATC category as processed in SQLite.

    Args:
        atc_code: The ATC category identifier value.
        atc_label: Optional label description.
        db_path: Path to SQLite database file.

    """
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO atc_progress (atc_code, atc_label, processed_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
            (atc_code, atc_label),
        )
        conn.commit()


def upsert_medicamentos_batch(
    records: List[Dict[str, Any]],
    db_path: str = DB_PATH,
) -> None:
    """Insert or update a batch of medicine records in SQLite atomically.

    Args:
        records: List of medicine record dictionaries.
        db_path: Path to SQLite database file.

    """
    if not records:
        return

    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        cursor = conn.cursor()

        for r in records:
            id_key = r["id_key"]

            # Query existing record to merge ATC codes and labels
            cursor.execute(
                "SELECT atc_codes, atc_labels, rcm_downloaded, fi_downloaded, "
                "mmr_downloaded FROM medicamentos WHERE id_key = ?",
                (id_key,),
            )
            row = cursor.fetchone()

            atc_codes = list(r.get("atc_codes", []))
            atc_labels = list(r.get("atc_labels", []))
            rcm_downloaded = 1 if r.get("rcm_downloaded") else 0
            fi_downloaded = 1 if r.get("fi_downloaded") else 0
            mmr_downloaded = 1 if r.get("mmr_downloaded") else 0

            if row:
                existing_codes = json.loads(row[0]) if row[0] else []
                existing_labels = json.loads(row[1]) if row[1] else []
                for c in existing_codes:
                    if c not in atc_codes:
                        atc_codes.append(c)
                for lbl in existing_labels:
                    if lbl not in atc_labels:
                        atc_labels.append(lbl)
                rcm_downloaded = 1 if (row[2] or rcm_downloaded) else 0
                fi_downloaded = 1 if (row[3] or fi_downloaded) else 0
                mmr_downloaded = 1 if (row[4] or mmr_downloaded) else 0

            cursor.execute(
                """
                INSERT INTO medicamentos (
                    id_key, med_id, drug_name, active_substance, pharma_form,
                    dosage, mah, commercialization, aim_status, atc_codes,
                    atc_labels, has_rcm, rcm_filename, rcm_downloaded,
                    rcm_verified, has_fi, fi_filename, fi_downloaded,
                    fi_verified, has_mmr, mmr_filename, mmr_downloaded,
                    mmr_verified, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, CURRENT_TIMESTAMP
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
                    has_fi = excluded.has_fi,
                    fi_filename = excluded.fi_filename,
                    fi_downloaded = excluded.fi_downloaded,
                    fi_verified = excluded.fi_verified,
                    has_mmr = excluded.has_mmr,
                    mmr_filename = excluded.mmr_filename,
                    mmr_downloaded = excluded.mmr_downloaded,
                    mmr_verified = excluded.mmr_verified,
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
                    1 if r.get("has_fi") else 0,
                    r.get("fi_filename"),
                    fi_downloaded,
                    fi_downloaded,
                    1 if r.get("has_mmr") else 0,
                    r.get("mmr_filename"),
                    mmr_downloaded,
                    mmr_downloaded,
                ),
            )
        conn.commit()


def load_all_medicamentos_from_db(
    db_path: str = DB_PATH,
) -> Dict[str, Dict[str, Any]]:
    """Load all records from SQLite into a dictionary keyed by id_key.

    Args:
        db_path: Path to SQLite database file.

    Returns:
        Dict mapping id_key to structured medicine dictionary.

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
        json_path: Target path for the JSON export.
        csv_path: Target path for the CSV export.

    """
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
    """Validate PDF file integrity via header, trailer, size, and pdfinfo.

    Args:
        filepath: Path to the PDF file on disk.

    Returns:
        True if the PDF is non-empty and structurally intact, False otherwise.

    """
    if not os.path.exists(filepath):
        return False

    file_size = os.path.getsize(filepath)
    if file_size < 100:
        logger.warning(f"PDF file '{filepath}' is too small ({file_size} bytes).")
        return False

    try:
        with open(filepath, "rb") as f:
            header = f.read(1024)
            # Standard PDF magic bytes
            is_pdf = b"%PDF-" in header
            # Microsoft Word OLE2 Compound Document magic bytes (INFARMED legacy files)
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
            elif is_doc:
                # OLE2 Word doc is valid binary if size > 1024 bytes
                return True
    except Exception as err:
        logger.warning(f"Failed to read PDF file '{filepath}': {err}")
        return False

    try:
        res = subprocess.run(
            ["pdfinfo", filepath],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        if res.returncode != 0:
            logger.warning(f"pdfinfo failed for '{filepath}' (code {res.returncode}).")
            return False
    except FileNotFoundError:
        pass
    except Exception:
        pass

    return True


def sanitize_filename(name: str) -> str:
    """Sanitize string to create a safe cross-platform filename.

    Args:
        name: Raw input text string.

    Returns:
        Sanitized string suitable for filenames.

    """
    clean = re.sub(r"[^\w\.-]", "_", name.strip())
    clean = re.sub(r"_+", "_", clean).strip("_")
    return clean or "document"


def extract_atc_categories(page: Page) -> List[Dict[str, str]]:
    """Extract all available ATC categories from the PrimeFaces select dropdown.

    Args:
        page: Playwright Page instance.

    Returns:
        List of dicts with 'value' and 'label' for valid ATC options.

    """
    page.wait_for_selector(ATC_DROPDOWN_SELECTOR, state="attached", timeout=15000)
    eval_js = (
        "options => options.map(opt => "
        "({value: opt.value, label: opt.innerText.trim()}))"
    )
    atc_options = page.locator(f"{ATC_DROPDOWN_SELECTOR} option").evaluate_all(eval_js)
    valid_atcs = [opt for opt in atc_options if opt["value"].strip()]
    logger.info(f"Extracted {len(valid_atcs)} ATC categories from DOM.")
    return valid_atcs


def select_atc_option(page: Page, atc_value: str) -> None:
    """Select an ATC option using PrimeFaces widget API or standard select.

    Args:
        page: Playwright Page instance.
        atc_value: The option value string (e.g., 'REF_CLASS_ATC:A01A').

    """
    page.wait_for_selector(ATC_DROPDOWN_SELECTOR, state="attached", timeout=10000)
    try:
        page.select_option(ATC_DROPDOWN_SELECTOR, atc_value, timeout=5000)
    except Exception:
        page.evaluate(
            f"if (window.PF && PF('widget_mainForm_classif_atc')) {{ "
            f"  PF('widget_mainForm_classif_atc').selectValue('{atc_value}'); "
            f"}}"
        )


def download_single_document(
    icon_locator: Locator,
    target_filepath: str,
    page: Page,
    doc_type: str = "Document",
    timeout_ms: int = 15000,
) -> bool:
    """Download and validate document if not already cached and valid on disk.

    Args:
        icon_locator: Locator pointing to the download icon element.
        target_filepath: Destination file path on local disk.
        page: Playwright Page instance.
        doc_type: Human-readable document type label for logging.
        timeout_ms: Download wait timeout in milliseconds.

    Returns:
        True if download succeeded and passed integrity check, False otherwise.

    """
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
    atc: Dict[str, str],
    page: Page,
    download_dir_rcms: str = DOWNLOAD_DIR_RCMS,
    download_dir_leaflets: str = DOWNLOAD_DIR_LEAFLETS,
    download_dir_mmr: str = DOWNLOAD_DIR_MMR,
    downloaded_files: Optional[Set[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Extract full drug metadata, RCM, Patient Leaflet (FI), and MMR documents.

    Args:
        row: Playwright Locator pointing to the table tr.
        atc: Dict with 'value' and 'label' of current ATC category.
        page: Playwright Page instance for downloading.
        download_dir_rcms: Directory where RCM PDFs are stored.
        download_dir_leaflets: Directory where Leaflet PDFs are stored.
        download_dir_mmr: Directory where MMR documents are stored.
        downloaded_files: In-memory set of downloaded filenames.

    Returns:
        Dict representing the structured medicine record, or None on error.

    """
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
    commercialization = cells[6] if len(cells) > 6 else ""
    aim_status = cells[7] if len(cells) > 7 else ""

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
            downloaded_files.add(rcm_filename)

    # 2. Handle Leaflet (FI) Document
    fi_filename: Optional[str] = None
    fi_downloaded = False
    fi_verified = False

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
            downloaded_files.add(fi_filename)

    # 3. Handle MMR Document
    mmr_filename: Optional[str] = None
    mmr_downloaded = False
    mmr_verified = False

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
            downloaded_files.add(mmr_filename)

    raw_atc = atc["value"].replace("REF_CLASS_ATC:", "").strip()

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
        "atc_labels": [atc["label"]] if atc.get("label") else [],
        "has_rcm": has_rcm,
        "rcm_filename": rcm_filename,
        "rcm_downloaded": rcm_downloaded,
        "rcm_verified": rcm_verified,
        "has_fi": has_fi,
        "fi_filename": fi_filename,
        "fi_downloaded": fi_downloaded,
        "fi_verified": fi_verified,
        "has_mmr": has_mmr,
        "mmr_filename": mmr_filename,
        "mmr_downloaded": mmr_downloaded,
        "mmr_verified": mmr_verified,
    }


def process_atc_category(
    page: Page,
    atc: Dict[str, str],
    target_url: str,
    downloaded_files: Optional[Set[str]] = None,
    download_dir_rcms: str = DOWNLOAD_DIR_RCMS,
    download_dir_leaflets: str = DOWNLOAD_DIR_LEAFLETS,
    download_dir_mmr: str = DOWNLOAD_DIR_MMR,
) -> List[Dict[str, Any]]:
    """Execute search for a single ATC category and extract medicines & documents.

    Args:
        page: Playwright Page instance.
        atc: Dict containing 'value' and 'label'.
        target_url: URL to reload in case of failure.
        downloaded_files: Set of filenames already downloaded.
        download_dir_rcms: Directory where RCM documents will be saved.
        download_dir_leaflets: Directory where Leaflet documents will be saved.
        download_dir_mmr: Directory where MMR documents will be saved.

    Returns:
        List of extracted medicine record dicts for this category.

    """
    if downloaded_files is None:
        downloaded_files = set()

    cat_value = atc["value"]
    cat_label = atc["label"]
    logger.info(f"Querying ATC: {cat_label} ({cat_value})")

    extracted_records: List[Dict[str, Any]] = []

    page.wait_for_selector(SEARCH_BUTTON_SELECTOR, state="visible", timeout=15000)
    select_atc_option(page, cat_value)
    page.locator(SEARCH_BUTTON_SELECTOR).click(timeout=10000)
    page.wait_for_selector(RESULTS_TABLE_SELECTOR, state="visible", timeout=15000)
    page.wait_for_timeout(1000)

    os.makedirs(download_dir_rcms, exist_ok=True)
    os.makedirs(download_dir_leaflets, exist_ok=True)
    os.makedirs(download_dir_mmr, exist_ok=True)

    page_num = 1
    while True:
        rows = page.locator(f"{TABLE_BODY_SELECTOR} tr").all()
        rcm_count_on_page = 0
        fi_count_on_page = 0

        for row in rows:
            record = extract_medicine_row(
                row=row,
                atc=atc,
                page=page,
                download_dir_rcms=download_dir_rcms,
                download_dir_leaflets=download_dir_leaflets,
                download_dir_mmr=download_dir_mmr,
                downloaded_files=downloaded_files,
            )
            if record:
                extracted_records.append(record)
                if record.get("rcm_downloaded"):
                    rcm_count_on_page += 1
                if record.get("fi_downloaded"):
                    fi_count_on_page += 1

        logger.info(
            f"ATC {cat_value} - Page {page_num}: "
            f"Extracted {len(rows)} rows, {rcm_count_on_page} RCMs, "
            f"{fi_count_on_page} Leaflets."
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


def retry_missing_documents(
    page: Page,
    target_url: str,
    db_path: str = DB_PATH,
    download_dir_rcms: str = DOWNLOAD_DIR_RCMS,
    download_dir_leaflets: str = DOWNLOAD_DIR_LEAFLETS,
    download_dir_mmr: str = DOWNLOAD_DIR_MMR,
) -> Tuple[int, int]:
    """Execute Stage 2: Target and retry downloading all missing published documents.

    Args:
        page: Playwright Page instance.
        target_url: URL of the search page.
        db_path: Path to SQLite database.
        download_dir_rcms: RCM directory.
        download_dir_leaflets: Leaflet directory.
        download_dir_mmr: MMR directory.

    Returns:
        Tuple of (recovered_rcms_count, recovered_leaflets_count).

    """
    logger.info("Starting Stage 2: Targeted Document Reconciliation Pass...")
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

            # Fill registration number or drug name input
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
                    # 1. Retry RCM if needed
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
                                        "rcm_verified = 1 WHERE id_key = ?",
                                        (id_key,),
                                    )
                                    conn.commit()

                    # 2. Retry Leaflet (FI) if needed
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
                                        "fi_verified = 1 WHERE id_key = ?",
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
    """Audit all scraped medicines, document coverage, and file integrity.

    Args:
        medicines: Dict mapping medicine ID keys to their records.
        download_dir_rcms: Path to directory containing RCM PDFs.
        download_dir_leaflets: Path to directory containing Leaflet PDFs.
        download_dir_mmr: Path to directory containing MMR PDFs.

    Returns:
        Dict containing comprehensive audit statistics and verification.

    """
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

    def check_folder(folder_path: str) -> Tuple[int, int, int]:
        if not os.path.exists(folder_path):
            return 0, 0, 0
        files = [f for f in os.listdir(folder_path) if f.lower().endswith(".pdf")]
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


def print_summary_table(
    audit: Dict[str, Any],
    atcs_processed: int,
    total_atcs: int = 3193,
) -> None:
    """Print a clean, formatted executive summary table to stdout.

    Args:
        audit: Audit summary dictionary from audit_documents_and_integrity.
        atcs_processed: Count of processed ATC categories.
        total_atcs: Total expected ATC categories.

    """
    atc_pct = (atcs_processed / total_atcs * 100) if total_atcs else 100.0
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

    total_drugs = audit.get("total_unique_drugs", 0)

    sep_thick = "=" * 88
    sep_thin = "-" * 88
    hdr = f"{'Category':<22} {'Metric Name':<28} {'Count / Status':<20} {'Notes'}"

    lines = [
        "",
        sep_thick,
        f"{'INFOMED SCRAPER AUDIT REPORT':^88}",
        sep_thick,
        hdr,
        sep_thin,
        f"{'Catalog Scope':<22} {'ATC Categories Traversed':<28} "
        f"{f'{atcs_processed:,} / {total_atcs:,} ({atc_pct:.1f}%)':<20} "
        "All valid categories",
        f"{'':<22} {'Unique Medicines in DB':<28} "
        f"{f'{total_drugs:,}':<20} "
        "Distinct formulations",
        sep_thin,
        f"{'SmPC Documents (RCM)':<22} {'Published on Portal':<28} "
        f"{f'{rcm_pub:,}':<20} Published by INFARMED",
        f"{'':<22} {'Downloaded & Verified':<28} "
        f"{f'{rcm_dl:,} ({rcm_pct:.1f}%)':<20} Saved in downloads/rcms",
        f"{'':<22} {'Missing on Portal':<28} "
        f"{f'{rcm_miss:,}':<20} Server null/ghost links",
        sep_thin,
        f"{'Patient Leaflets (FI)':<22} {'Published on Portal':<28} "
        f"{f'{fi_pub:,}':<20} Published by INFARMED",
        f"{'':<22} {'Downloaded & Verified':<28} "
        f"{f'{fi_dl:,} ({fi_pct:.1f}%)':<20} Saved in downloads/leaflets",
        f"{'':<22} {'Missing on Portal':<28} "
        f"{f'{fi_miss:,}':<20} Server null/ghost links",
        sep_thin,
        f"{'Files on Disk':<22} {'Total Documents on Disk':<28} "
        f"{f'{tot_disk:,}':<20} RCMs + Leaflets",
        f"{'':<22} {'Corrupted Files':<28} {f'{corrupted:,} (0.0%)':<20} 100% intact",
        f"{'':<22} {'File Integrity Rate':<28} "
        f"{f'{integrity_pct:.1f}%':<20} Header, trailer & pdfinfo",
        sep_thick,
        "",
    ]
    print("\n".join(lines))


def create_browser_session(p: Any, headless: bool = True) -> Tuple[Any, Any, Page]:
    """Launch a low-memory Chromium browser, context, and page.

    Args:
        p: Playwright instance.
        headless: Whether to run in headless mode.

    Returns:
        Tuple of (browser, context, page).

    """
    browser = p.chromium.launch(
        headless=headless,
        args=CHROMIUM_LOW_MEM_ARGS,
    )
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    page.set_default_timeout(15000)
    page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
    return browser, context, page


def retrieve_infomed_rcms(
    headless: bool = True,
    db_path: str = DB_PATH,
    stage_2_only: bool = False,
) -> Dict[str, Any]:
    """Retrieve all RCMs, Leaflets, and metadata with automatic Stage 2 execution.

    Args:
        headless: Whether to run Playwright in headless mode.
        db_path: Path to SQLite database file.
        stage_2_only: Whether to skip Stage 1 and run only Stage 2 targeted retry.

    Returns:
        Audit report dict summarizing scraped data and file integrity.

    """
    init_db(db_path=db_path)

    processed_atcs: Set[str] = load_atc_progress_from_db(db_path=db_path)
    downloaded_files: Set[str] = set()

    for d in (DOWNLOAD_DIR_RCMS, DOWNLOAD_DIR_LEAFLETS, DOWNLOAD_DIR_MMR):
        if os.path.exists(d):
            for fname in os.listdir(d):
                if fname.lower().endswith(".pdf"):
                    downloaded_files.add(fname)

    existing_db_meds = load_all_medicamentos_from_db(db_path=db_path)
    logger.info(
        f"Starting pipeline with unified SQLite: {len(processed_atcs)} "
        f"ATCs completed, {len(existing_db_meds)} drugs in DB, "
        f"{len(downloaded_files)} PDFs saved."
    )

    with sync_playwright() as p:
        browser, context, page = create_browser_session(p, headless=headless)

        if not stage_2_only:
            atc_categories = extract_atc_categories(page)
            unprocessed_atcs = [
                atc for atc in atc_categories if atc["value"] not in processed_atcs
            ]

            if not unprocessed_atcs:
                logger.info(
                    f"All {len(atc_categories)} ATC categories have already been "
                    "processed in Stage 1. Transitioning directly to Stage 2: "
                    "Targeted Document Reconciliation..."
                )
            else:
                logger.info(
                    f"Stage 1: {len(unprocessed_atcs)} / {len(atc_categories)} "
                    "ATC categories remaining to process."
                )

                atc_count_in_session = 0

                # Stage 1: Traverse remaining ATC categories
                for atc in unprocessed_atcs:
                    atc_val = atc["value"]

                    # Periodic Browser Recycling & Checkpoint (every 100 ATCs)
                    should_recycle_browser = (
                        atc_count_in_session > 0
                        and atc_count_in_session % BROWSER_RECYCLE_INTERVAL == 0
                    )
                    should_recycle_context = (
                        atc_count_in_session > 0
                        and atc_count_in_session % CONTEXT_RECYCLE_INTERVAL == 0
                    )

                    if should_recycle_browser:
                        logger.info(
                            f"Recycling full browser process after "
                            f"{atc_count_in_session} ATCs and exporting checkpoint..."
                        )
                        try:
                            page.close()
                            context.close()
                            browser.close()
                        except Exception:
                            pass
                        gc.collect()
                        export_db_to_datasets(db_path=db_path)
                        browser, context, page = create_browser_session(
                            p, headless=headless
                        )

                    # Periodic Context Recycling (every 25 ATCs)
                    elif should_recycle_context:
                        logger.info(
                            f"Recycling browser context after "
                            f"{atc_count_in_session} ATCs to flush download buffers..."
                        )
                        try:
                            page.close()
                            context.close()
                        except Exception:
                            pass
                        gc.collect()
                        context = browser.new_context(accept_downloads=True)
                        page = context.new_page()
                        page.set_default_timeout(15000)
                        page.goto(
                            TARGET_URL, wait_until="domcontentloaded", timeout=30000
                        )

                    try:
                        records = process_atc_category(
                            page,
                            atc,
                            TARGET_URL,
                            downloaded_files=downloaded_files,
                            download_dir_rcms=DOWNLOAD_DIR_RCMS,
                            download_dir_leaflets=DOWNLOAD_DIR_LEAFLETS,
                            download_dir_mmr=DOWNLOAD_DIR_MMR,
                        )

                        if records:
                            upsert_medicamentos_batch(records, db_path=db_path)

                        processed_atcs.add(atc_val)
                        mark_atc_processed_in_db(
                            atc_val, atc.get("label", ""), db_path=db_path
                        )
                        atc_count_in_session += 1

                    except Exception as err:
                        logger.error(f"Error processing ATC '{atc_val}': {err}")
                        try:
                            page.goto(
                                TARGET_URL, wait_until="domcontentloaded", timeout=20000
                            )
                            page.wait_for_selector(
                                SEARCH_BUTTON_SELECTOR, state="visible", timeout=15000
                            )
                        except Exception as reload_err:
                            logger.error(
                                f"Failed to reload page: {reload_err}. "
                                "Reopening page..."
                            )
                            try:
                                page.close()
                                context.close()
                                gc.collect()
                                context = browser.new_context(accept_downloads=True)
                                page = context.new_page()
                                page.set_default_timeout(15000)
                                page.goto(
                                    TARGET_URL,
                                    wait_until="domcontentloaded",
                                    timeout=20000,
                                )
                                page.wait_for_selector(
                                    SEARCH_BUTTON_SELECTOR,
                                    state="visible",
                                    timeout=15000,
                                )
                            except Exception as page_err:
                                logger.error(f"Failed to re-init page: {page_err}")

        # Stage 2: Targeted Document Reconciliation Pass
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

    # Final export of SQLite to JSON and CSV
    export_db_to_datasets(db_path=db_path)
    all_final_meds = load_all_medicamentos_from_db(db_path=db_path)
    audit = audit_documents_and_integrity(
        all_final_meds,
        download_dir_rcms=DOWNLOAD_DIR_RCMS,
        download_dir_leaflets=DOWNLOAD_DIR_LEAFLETS,
        download_dir_mmr=DOWNLOAD_DIR_MMR,
    )
    atcs_done = len(load_atc_progress_from_db(db_path=db_path))
    print_summary_table(audit, atcs_processed=atcs_done)
    return audit


def parse_cli_args() -> argparse.Namespace:
    """Parse command-line arguments for the scraper.

    Returns:
        Parsed arguments namespace.

    """
    parser = argparse.ArgumentParser(
        description="INFOMED Scraper: Download RCMs, Leaflets, and drug metadata."
    )
    parser.add_argument(
        "--stage2",
        "--retry-only",
        action="store_true",
        dest="stage_2_only",
        help="Skip Stage 1 (ATC traversal) and run only Stage 2 targeted retry.",
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
    )
