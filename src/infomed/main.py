"""Infomed RCM, Patient Leaflet, and Drug Metadata Scraper Module.

This module automates the extraction of comprehensive medicine metadata,
RCM (Resumo das Características do Medicamento / SmPC) PDFs, and FI (Folheto
Informativo / Patient Leaflet) PDFs from the INFOMED JSF extranet portal using
Playwright with aggressive memory management and context recycling.
"""

import csv
import gc
import json
import logging
import os
import re
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
            if b"%PDF-" not in header:
                logger.warning(f"PDF file '{filepath}' is missing '%PDF-' header.")
                return False

            f.seek(max(0, file_size - 1024))
            trailer = f.read()
            if b"%%EOF" not in trailer:
                logger.warning(f"PDF file '{filepath}' is missing '%%EOF' trailer.")
                return False
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


def load_progress() -> Dict[str, Any]:
    """Load previously processed ATC codes and downloaded files.

    Returns:
        Dict containing sets of processed ATCs and downloaded files.

    """
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {
                    "processed_atcs": set(data.get("processed_atcs", [])),
                    "downloaded_files": set(data.get("downloaded_files", [])),
                }
        except Exception as err:
            logger.warning(f"Failed to load progress file: {err}")
    return {"processed_atcs": set(), "downloaded_files": set()}


def save_progress(
    processed_atcs: Set[str],
    downloaded_files: Optional[Set[str]] = None,
) -> None:
    """Save current execution progress to file.

    Args:
        processed_atcs: Set of ATC category values already processed.
        downloaded_files: Set of downloaded file names.

    """
    if downloaded_files is None:
        downloaded_files = set()
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "processed_atcs": sorted(list(processed_atcs)),
                    "downloaded_files": sorted(list(downloaded_files)),
                },
                f,
                indent=2,
            )
    except Exception as err:
        logger.error(f"Failed to save progress: {err}")


def load_medicamentos() -> Dict[str, Dict[str, Any]]:
    """Load previously scraped medicine records from JSON file.

    Returns:
        Dict mapping unique medicine identifier keys to their drug record dicts.

    """
    if os.path.exists(MEDICAMENTOS_JSON):
        try:
            with open(MEDICAMENTOS_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
                cleaned = {}
                for item in data:
                    if "id_key" in item:
                        item.pop("rcm_url", None)
                        item.pop("fi_url", None)
                        item.pop("mmr_url", None)
                        cleaned[item["id_key"]] = item
                return cleaned
        except Exception as err:
            logger.warning(f"Failed to load existing medicamentos file: {err}")
    return {}


def save_dataset(
    medicines: Dict[str, Dict[str, Any]],
    json_path: str = MEDICAMENTOS_JSON,
    csv_path: str = MEDICAMENTOS_CSV,
) -> None:
    """Export complete medicine records into JSON and CSV files.

    Args:
        medicines: Dict mapping unique medicine keys to their metadata dicts.
        json_path: Target path for the JSON export.
        csv_path: Target path for the CSV export.

    """
    records = list(medicines.values())
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
    except Exception as err:
        logger.error(f"Failed to save JSON dataset: {err}")

    if not records:
        return

    fieldnames = [
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

    try:
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for r in records:
                row = dict(r)
                if isinstance(row.get("atc_codes"), list):
                    row["atc_codes"] = "; ".join(row["atc_codes"])
                if isinstance(row.get("atc_labels"), list):
                    row["atc_labels"] = "; ".join(row["atc_labels"])
                writer.writerow(row)
    except Exception as err:
        logger.error(f"Failed to save CSV dataset: {err}")


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
) -> bool:
    """Download and validate document if not already cached and valid on disk.

    Args:
        icon_locator: Locator pointing to the download icon element.
        target_filepath: Destination file path on local disk.
        page: Playwright Page instance.
        doc_type: Human-readable document type label for logging.

    Returns:
        True if download succeeded and passed integrity check, False otherwise.

    """
    if os.path.exists(target_filepath) and validate_pdf(target_filepath):
        return True

    try:
        with page.expect_download(timeout=10000) as download_info:
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


def retrieve_infomed_rcms(headless: bool = True) -> Dict[str, Any]:
    """Retrieve all RCMs, Leaflets, and metadata with aggressive memory recycling.

    Args:
        headless: Whether to run Playwright in headless mode.

    Returns:
        Audit report dict summarizing scraped data and file integrity.

    """
    progress = load_progress()
    processed_atcs: Set[str] = progress["processed_atcs"]
    downloaded_files: Set[str] = progress.get("downloaded_files", set())

    # Pre-populate downloaded_files with existing valid PDFs across folders
    for d in (DOWNLOAD_DIR_RCMS, DOWNLOAD_DIR_LEAFLETS, DOWNLOAD_DIR_MMR):
        if os.path.exists(d):
            for fname in os.listdir(d):
                if fname.lower().endswith(".pdf"):
                    downloaded_files.add(fname)

    medicines_dict = load_medicamentos()
    logger.info(
        f"Starting pipeline: {len(processed_atcs)} ATCs completed, "
        f"{len(medicines_dict)} drugs recorded, {len(downloaded_files)} PDFs saved."
    )

    with sync_playwright() as p:
        browser, context, page = create_browser_session(p, headless=headless)
        atc_categories = extract_atc_categories(page)

        atc_count_in_session = 0

        for atc in atc_categories:
            atc_val = atc["value"]
            if atc_val in processed_atcs:
                continue

            # 1. Periodic Browser Recycling (every 100 ATCs)
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
                    f"Recycling full browser process after {atc_count_in_session} "
                    "ATCs to release all Chromium memory..."
                )
                try:
                    page.close()
                    context.close()
                    browser.close()
                except Exception:
                    pass
                gc.collect()
                browser, context, page = create_browser_session(p, headless=headless)

            # 2. Periodic Context Recycling (every 25 ATCs)
            elif should_recycle_context:
                logger.info(
                    f"Recycling browser context after {atc_count_in_session} "
                    "ATCs to flush download buffers..."
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
                page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)

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

                for r in records:
                    k = r["id_key"]
                    if k in medicines_dict:
                        existing = medicines_dict[k]
                        for c in r.get("atc_codes", []):
                            if c not in existing.setdefault("atc_codes", []):
                                existing["atc_codes"].append(c)
                        for lbl in r.get("atc_labels", []):
                            if lbl not in existing.setdefault("atc_labels", []):
                                existing["atc_labels"].append(lbl)
                        for flag_key, ver_key in (
                            ("rcm_downloaded", "rcm_verified"),
                            ("fi_downloaded", "fi_verified"),
                            ("mmr_downloaded", "mmr_verified"),
                        ):
                            if r.get(flag_key):
                                existing[flag_key] = True
                                existing[ver_key] = True
                    else:
                        medicines_dict[k] = r

                processed_atcs.add(atc_val)
                atc_count_in_session += 1
                save_progress(processed_atcs, downloaded_files)
                save_dataset(medicines_dict)

            except Exception as err:
                logger.error(f"Error processing ATC '{atc_val}': {err}")
                try:
                    page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_selector(
                        SEARCH_BUTTON_SELECTOR, state="visible", timeout=15000
                    )
                except Exception as reload_err:
                    logger.error(
                        f"Failed to reload page: {reload_err}. Reopening fresh page..."
                    )
                    try:
                        page.close()
                        context.close()
                        gc.collect()
                        context = browser.new_context(accept_downloads=True)
                        page = context.new_page()
                        page.set_default_timeout(15000)
                        page.goto(
                            TARGET_URL, wait_until="domcontentloaded", timeout=20000
                        )
                        page.wait_for_selector(
                            SEARCH_BUTTON_SELECTOR, state="visible", timeout=15000
                        )
                    except Exception as page_err:
                        logger.error(f"Failed to re-initialize page: {page_err}")

        try:
            page.close()
            context.close()
            browser.close()
        except Exception:
            pass

    save_dataset(medicines_dict)
    audit = audit_documents_and_integrity(
        medicines_dict,
        download_dir_rcms=DOWNLOAD_DIR_RCMS,
        download_dir_leaflets=DOWNLOAD_DIR_LEAFLETS,
        download_dir_mmr=DOWNLOAD_DIR_MMR,
    )
    return audit


if __name__ == "__main__":
    retrieve_infomed_rcms(headless=True)
