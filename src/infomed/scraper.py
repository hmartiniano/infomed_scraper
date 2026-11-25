"""Playwright scraping, AJAX synchronization, retry, and backfill routines."""

import gc
import logging
import os
import re
import shutil
import sqlite3
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from playwright.sync_api import Locator, Page

from infomed.config import (
    ATC_DROPDOWN_SELECTOR,
    BROWSER_RECYCLE_INTERVAL,
    CHROMIUM_LOW_MEM_ARGS,
    DB_PATH,
    DISABLED_NEXT_PAGE_SELECTOR,
    DOWNLOAD_DIR_LEAFLETS,
    DOWNLOAD_DIR_MMR,
    DOWNLOAD_DIR_RCMS,
    DRUG_NAME_INPUT_SELECTOR,
    FI_ICON_SELECTOR,
    HOMEPAGE_URL,
    MMR_ICON_SELECTOR,
    NEXT_PAGE_SELECTOR,
    RCM_ICON_SELECTOR,
    RESULTS_TABLE_SELECTOR,
    SEARCH_BUTTON_SELECTOR,
    TABLE_BODY_SELECTOR,
    TARGET_URL,
)
from infomed.db import (
    export_db_to_datasets,
    load_progress_table,
    mark_progress_item,
    save_sweep_metrics,
    upsert_medicamentos_batch,
)
from infomed.ema import (
    fetch_ema_epar_document,
    sanitize_filename,
    strip_infomed_badges,
    validate_pdf,
)

logger = logging.getLogger("infomed")


def fetch_portal_benchmark_stats(page: Page) -> Dict[str, Any]:
    """Extract official published totals and update date from INFOMED homepage.

    Args:
        page: Playwright Page instance.

    Returns:
        Dict with benchmark statistics from ``index.xhtml``. Unparsed numeric
        fields remain ``None`` rather than falling back to stale constants.

    """
    benchmark: Dict[str, Any] = {
        "portal_url": HOMEPAGE_URL,
        "portal_last_updated": "Unknown",
        "official_active_substances_dci": None,
        "official_marketed_medicines": None,
        "official_marketed_presentations": None,
    }
    try:
        page.goto(HOMEPAGE_URL, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2500)  # Allow JS countup animations to settle

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


def extract_dropdown_options(
    page: Page, selector: str, desc: str = "options"
) -> List[Dict[str, str]]:
    """Extract options from a select dropdown.

    Args:
        page: Playwright Page instance.
        selector: CSS selector for select element.
        desc: Description for logging.

    Returns:
        List of dicts with ``'value'`` and ``'label'``.

    """
    try:
        if "pesquisa-avancada.xhtml" not in page.url:
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_selector(selector, state="attached", timeout=15000)
    except Exception:
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_selector(selector, state="attached", timeout=20000)

    eval_js = (
        "options => options.map(opt => "
        "({value: opt.value, label: opt.innerText.trim()}))"
    )
    opts = page.locator(f"{selector} option").evaluate_all(eval_js)
    valid_opts = [opt for opt in opts if opt["value"].strip()]
    logger.info(f"Extracted {len(valid_opts)} {desc} from DOM.")
    return valid_opts


def wait_for_primefaces_ajax(page: Page, timeout_ms: int = 15000) -> None:
    """Wait until PrimeFaces AJAX queue is completely idle and DOM is settled.

    Args:
        page: Playwright Page instance.
        timeout_ms: Maximum wait time in milliseconds.

    """
    try:
        page.wait_for_function(
            "typeof PrimeFaces === 'undefined' || PrimeFaces.ajax.Queue.isEmpty()",
            timeout=timeout_ms,
        )
    except Exception:
        pass


def _submit_and_wait_ajax(
    page: Page, trigger_locator: Locator, timeout_ms: int = 20000
) -> None:
    """Click a PrimeFaces trigger and wait for the JSF AJAX response once.

    Does not re-click if ``expect_response`` times out after the click has been
    dispatched, preventing accidental double pagination jumps.

    Args:
        page: Playwright Page instance.
        trigger_locator: Locator to click (search or paginator button).
        timeout_ms: Response wait timeout in milliseconds.

    """
    try:
        with page.expect_response(
            lambda r: "pesquisa-avancada.xhtml" in r.url and r.request.method == "POST",
            timeout=timeout_ms,
        ):
            trigger_locator.click(timeout=10000)
    except Exception as err:
        logger.debug(f"AJAX response wait note after click (not re-clicking): {err}")
    wait_for_primefaces_ajax(page)


def select_dropdown_option(page: Page, selector: str, value: str) -> None:
    """Select an option on a DOM select dropdown.

    Args:
        page: Playwright Page instance.
        selector: CSS selector for the select element.
        value: Option value attribute to select.

    """
    page.wait_for_selector(selector, state="attached", timeout=10000)
    page.select_option(selector, value, timeout=5000)


def close_secondary_pages(page: Page) -> None:
    """Close extra/popup pages in context to prevent renderer process leaks.

    Args:
        page: Primary Playwright Page instance to retain.

    """
    try:
        if page and page.context:
            for extra_page in page.context.pages:
                if extra_page is not page:
                    try:
                        extra_page.close()
                    except Exception:
                        pass
    except Exception:
        pass


def download_single_document(
    icon_locator: Locator,
    target_filepath: str,
    page: Page,
    doc_type: str = "Document",
    timeout_ms: int = 15000,
) -> bool:
    """Download and validate a document atomically if not already valid on disk.

    Saves into a temporary file first and only promotes to ``target_filepath``
    when ``validate_pdf`` passes, cleaning up failed downloads.

    Args:
        icon_locator: Playwright Locator for the download icon.
        target_filepath: Destination path for the verified file.
        page: Playwright Page instance.
        doc_type: Human-readable document label for logs.
        timeout_ms: Playwright download timeout in milliseconds.

    Returns:
        True if the file exists or was downloaded and verified, False otherwise.

    """
    if os.path.exists(target_filepath) and validate_pdf(target_filepath):
        return True

    parent_dir = os.path.dirname(target_filepath)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    tmp_path = f"{target_filepath}.tmp"

    try:
        with page.expect_download(timeout=timeout_ms) as download_info:
            icon_locator.first.click()
        download = download_info.value
        download.save_as(tmp_path)

        if validate_pdf(tmp_path):
            os.replace(tmp_path, target_filepath)
            logger.info(f"Downloaded & verified {doc_type}: '{target_filepath}'")
            return True
        logger.warning(
            f"Downloaded {doc_type} '{target_filepath}' failed integrity check."
        )
        return False
    except Exception as err:
        logger.debug(f"Download trigger note for {doc_type} '{target_filepath}': {err}")
        return False
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        close_secondary_pages(page)


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
    """Extract full drug metadata, RCM, Patient Leaflet (FI), and MMR documents.

    Args:
        row: Playwright Locator pointing to a results table ``<tr>``.
        atc: Optional ATC option dict when sweeping WHO ATC.
        page: Playwright Page instance.
        sweep_name: Originating sweep label for provenance attribution.
        download_dir_rcms: Directory to save SmPC PDFs.
        download_dir_leaflets: Directory to save Patient Leaflet PDFs.
        download_dir_mmr: Directory to save MMR PDFs.
        downloaded_files: Optional legacy tracking set updated when provided.

    Returns:
        Parsed medicine record dictionary, or ``None`` for empty/no-results rows.

    """
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

    def _handle_doc(
        available: bool,
        suffix: str,
        target_dir: str,
        icon: Locator,
        doc_type: str,
    ) -> Tuple[Optional[str], bool, bool, Optional[str]]:
        if not available:
            return None, False, False, None
        filename = f"{base_name}{suffix}"
        filepath = os.path.join(target_dir, filename)
        ok = download_single_document(
            icon_locator=icon,
            target_filepath=filepath,
            page=page,
            doc_type=doc_type,
        )
        if ok:
            if downloaded_files is not None:
                downloaded_files.add(filename)
            return filename, True, True, sweep_name
        return filename, False, False, None

    rcm_filename, rcm_downloaded, rcm_verified, rcm_source = _handle_doc(
        has_rcm, ".pdf", download_dir_rcms, rcm_icon, "RCM"
    )
    fi_filename, fi_downloaded, fi_verified, fi_source = _handle_doc(
        has_fi, "_FI.pdf", download_dir_leaflets, fi_icon, "Leaflet"
    )
    mmr_filename, mmr_downloaded, mmr_verified, mmr_source = _handle_doc(
        has_mmr, "_MMR.pdf", download_dir_mmr, mmr_icon, "MMR"
    )

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


def _collect_paginated_rows(
    page: Page,
    log_prefix: str,
    atc_meta: Optional[Dict[str, str]],
    sweep_name: str,
    download_dir_rcms: str,
    download_dir_leaflets: str,
    download_dir_mmr: str,
    downloaded_files: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Iterate across all PrimeFaces paginator pages and extract medicine rows."""
    extracted_records: List[Dict[str, Any]] = []
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
            f"{log_prefix} - Page {page_num}: "
            f"Extracted {len(rows)} rows, {rcm_count} RCMs, {fi_count} Leaflets."
        )

        next_button = page.locator(NEXT_PAGE_SELECTOR).first
        disabled_next = page.locator(DISABLED_NEXT_PAGE_SELECTOR).first

        if next_button.is_visible() and not disabled_next.is_visible():
            page_num += 1
            _submit_and_wait_ajax(page, next_button)
        else:
            break

    return extracted_records


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
    """Execute search for a single dimension category and extract medicines.

    Args:
        page: Playwright Page instance.
        selector: CSS selector for the category dropdown.
        category: Dict containing ``'value'`` and ``'label'``.
        target_url: Search portal URL to return to after extracting category.
        sweep_name: Sweep dimension name for provenance tracking.
        atc_meta: Optional ATC category metadata dict.
        downloaded_files: Optional legacy set updated with downloaded filenames.
        download_dir_rcms: Directory for RCM PDFs.
        download_dir_leaflets: Directory for FI PDFs.
        download_dir_mmr: Directory for MMR PDFs.

    Returns:
        List of extracted medicine dictionaries across all result pages.

    """
    cat_value = category["value"]
    cat_label = category["label"]
    logger.info(f"Querying: {cat_label} ({cat_value})")

    page.wait_for_selector(SEARCH_BUTTON_SELECTOR, state="visible", timeout=15000)
    select_dropdown_option(page, selector, cat_value)
    _submit_and_wait_ajax(page, page.locator(SEARCH_BUTTON_SELECTOR))
    page.wait_for_selector(RESULTS_TABLE_SELECTOR, state="visible", timeout=15000)

    os.makedirs(download_dir_rcms, exist_ok=True)
    os.makedirs(download_dir_leaflets, exist_ok=True)
    os.makedirs(download_dir_mmr, exist_ok=True)

    extracted_records = _collect_paginated_rows(
        page=page,
        log_prefix=cat_value,
        atc_meta=atc_meta,
        sweep_name=sweep_name,
        download_dir_rcms=download_dir_rcms,
        download_dir_leaflets=download_dir_leaflets,
        download_dir_mmr=download_dir_mmr,
        downloaded_files=downloaded_files,
    )

    page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
    page.wait_for_selector(SEARCH_BUTTON_SELECTOR, state="visible", timeout=15000)
    return extracted_records


def create_browser_session(p: Any, headless: bool = True) -> Tuple[Any, Any, Page]:
    """Launch a low-memory Chromium browser, context, and page.

    Args:
        p: Active Playwright instance.
        headless: Whether to launch Chromium headless.

    Returns:
        Tuple of ``(browser, context, page)``.

    """
    browser = p.chromium.launch(
        headless=headless,
        args=CHROMIUM_LOW_MEM_ARGS,
    )
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    page.set_default_timeout(15000)

    for attempt in range(3):
        try:
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector(
                SEARCH_BUTTON_SELECTOR, state="attached", timeout=15000
            )
            break
        except Exception as e:
            if attempt == 2:
                logger.error(f"Failed to load {TARGET_URL} after 3 attempts: {e}")
                raise e
            logger.warning(f"Initial page navigation retry {attempt + 1}/3: {e}")
            time.sleep(2.0)

    return browser, context, page


def _close_browser_session(browser: Any, context: Any, page: Any) -> None:
    """Close Playwright page, context, and browser safely."""
    for obj in (page, context, browser):
        if obj is not None:
            try:
                obj.close()
            except Exception:
                pass


def run_dimension_sweep(
    sweep_name: str,
    selector: str,
    progress_table: str,
    browser: Any,
    context: Any,
    page: Page,
    p: Any,
    db_path: str = DB_PATH,
    headless: bool = True,
    downloaded_files: Optional[Set[str]] = None,
) -> Tuple[Any, Any, Page]:
    """Execute a generalized sweep across a classification or filter dimension.

    Args:
        sweep_name: Display name for the dimension sweep.
        selector: CSS selector for the dimension dropdown.
        progress_table: SQLite progress table name.
        browser: Playwright Browser instance.
        context: Playwright BrowserContext instance.
        page: Playwright Page instance.
        p: Active Playwright instance for browser recycling.
        db_path: Path to SQLite database file.
        headless: Whether recycled sessions run headless.
        downloaded_files: Optional legacy set updated with downloaded filenames.

    Returns:
        Possibly recycled ``(browser, context, page)`` tuple.

    """
    start_time = time.time()
    processed_codes: Set[str] = load_progress_table(progress_table, db_path=db_path)
    previously_processed = len(processed_codes)
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

        # Recycle Chromium periodically to bound renderer memory usage
        if count_in_session > 0 and count_in_session % BROWSER_RECYCLE_INTERVAL == 0:
            _close_browser_session(browser, context, page)
            gc.collect()
            export_db_to_datasets(db_path=db_path)
            browser, context, page = create_browser_session(p, headless=headless)

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
            close_secondary_pages(page)

        except Exception as err:
            logger.error(f"Error processing {sweep_name} '{code_val}': {err}")
            try:
                if page.is_closed():
                    browser, context, page = create_browser_session(
                        p, headless=headless
                    )
                else:
                    page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_selector(
                        SEARCH_BUTTON_SELECTOR, state="visible", timeout=15000
                    )
            except Exception as reload_err:
                logger.error(f"Failed to reload page, recreating session: {reload_err}")
                _close_browser_session(browser, context, page)
                browser, context, page = create_browser_session(p, headless=headless)

    elapsed = time.time() - start_time
    logger.info(
        f"Sweep '{sweep_name}' completed in {elapsed:.1f}s: "
        f"{total_encountered} drugs seen (+{total_new_meds} new), "
        f"+{total_new_rcms} new RCMs, +{total_new_fis} new Leaflets."
    )

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
        accumulate=(previously_processed > 0 and count_in_session > 0),
    )
    return browser, context, page


def _build_clean_search_term(drug_name: str) -> str:
    """Strip INFOMED badges and parentheticals to improve portal name matching."""
    no_parens = re.sub(r"\(.*?\)", "", drug_name or "").strip()
    return strip_infomed_badges(no_parens) or (drug_name or "").strip()


def retry_missing_documents(
    page: Page,
    target_url: str,
    db_path: str = DB_PATH,
    download_dir_rcms: str = DOWNLOAD_DIR_RCMS,
    download_dir_leaflets: str = DOWNLOAD_DIR_LEAFLETS,
    download_dir_mmr: str = DOWNLOAD_DIR_MMR,
) -> Tuple[int, int]:
    """Execute Stage 2: Retry Downloads of Missing Files.

    Uses PrimeFaces AJAX queue synchronization, strips UI badges before
    searching by medicine name, handles result pagination, and retries missing
    RCMs, Patient Leaflets, and MMR documents.

    Args:
        page: Playwright Page instance.
        target_url: INFOMED advanced search URL.
        db_path: Path to SQLite database file.
        download_dir_rcms: Directory for RCM PDFs.
        download_dir_leaflets: Directory for FI PDFs.
        download_dir_mmr: Directory for MMR PDFs.

    Returns:
        Tuple of ``(recovered_rcms_count, recovered_fis_count)``.

    """
    logger.info("Starting Stage 2: Retry Downloads of Missing Files...")
    recovered_rcms = 0
    recovered_fis = 0
    recovered_mmrs = 0

    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id_key, med_id, drug_name, dosage, has_rcm, rcm_filename,
                   rcm_downloaded, has_fi, fi_filename, fi_downloaded,
                   has_mmr, mmr_filename, mmr_downloaded
            FROM medicamentos
            WHERE (has_rcm = 1 AND COALESCE(rcm_downloaded, 0) = 0)
               OR (has_fi = 1 AND COALESCE(fi_downloaded, 0) = 0)
               OR (has_mmr = 1 AND COALESCE(mmr_downloaded, 0) = 0)
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
        med_id = (item.get("med_id") or "").strip()
        drug_name = (item.get("drug_name") or "").strip()
        dosage = (item.get("dosage") or "").strip()
        search_term = _build_clean_search_term(drug_name)

        if not search_term:
            continue

        logger.info(f"Retrying missing documents for '{drug_name}' (ID: {med_id})...")

        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_selector(
                DRUG_NAME_INPUT_SELECTOR, state="visible", timeout=15000
            )
            page.locator(DRUG_NAME_INPUT_SELECTOR).fill(search_term)
            _submit_and_wait_ajax(page, page.locator(SEARCH_BUTTON_SELECTOR))
            page.wait_for_selector(
                RESULTS_TABLE_SELECTOR, state="visible", timeout=15000
            )

            matched = False
            while not matched:
                rows = page.locator(f"{TABLE_BODY_SELECTOR} tr").all()
                for row in rows:
                    cells = [
                        c.inner_text().strip().replace("\n", " ")
                        for c in row.locator("td").all()
                    ]
                    if not cells or (len(cells) == 1 and "Sem resultados" in cells[0]):
                        continue

                    row_med_id = cells[0] if len(cells) > 0 else ""
                    row_drug_name = cells[1] if len(cells) > 1 else ""
                    row_dosage = cells[4] if len(cells) > 4 else ""

                    is_match = (med_id and row_med_id == med_id) or (
                        not med_id
                        and drug_name
                        and row_drug_name == drug_name
                        and (not dosage or row_dosage == dosage)
                    )
                    if not is_match:
                        continue

                    matched = True
                    clean_id = sanitize_filename(id_key)

                    if item.get("has_rcm") and not item.get("rcm_downloaded"):
                        rcm_icon = row.locator(RCM_ICON_SELECTOR)
                        if rcm_icon.count() > 0:
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
                                        "UPDATE medicamentos SET rcm_filename = ?, "
                                        "rcm_downloaded = 1, rcm_verified = 1, "
                                        "rcm_source_sweep = 'Stage 2 Retry' "
                                        "WHERE id_key = ?",
                                        (rcm_file, id_key),
                                    )
                                    conn.commit()

                    if item.get("has_fi") and not item.get("fi_downloaded"):
                        fi_icon = row.locator(FI_ICON_SELECTOR)
                        if fi_icon.count() > 0:
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
                                        "UPDATE medicamentos SET fi_filename = ?, "
                                        "fi_downloaded = 1, fi_verified = 1, "
                                        "fi_source_sweep = 'Stage 2 Retry' "
                                        "WHERE id_key = ?",
                                        (fi_file, id_key),
                                    )
                                    conn.commit()

                    if item.get("has_mmr") and not item.get("mmr_downloaded"):
                        mmr_icon = row.locator(MMR_ICON_SELECTOR)
                        if mmr_icon.count() > 0:
                            mmr_file = item["mmr_filename"] or f"{clean_id}_MMR.pdf"
                            target_path = os.path.join(download_dir_mmr, mmr_file)
                            if download_single_document(
                                mmr_icon,
                                target_path,
                                page,
                                doc_type="MMR",
                                timeout_ms=20000,
                            ):
                                recovered_mmrs += 1
                                with sqlite3.connect(db_path, timeout=30.0) as conn:
                                    conn.execute(
                                        "UPDATE medicamentos SET mmr_filename = ?, "
                                        "mmr_downloaded = 1, mmr_verified = 1, "
                                        "mmr_source_sweep = 'Stage 2 Retry' "
                                        "WHERE id_key = ?",
                                        (mmr_file, id_key),
                                    )
                                    conn.commit()
                    break

                if matched:
                    break
                next_btn = page.locator(NEXT_PAGE_SELECTOR).first
                disabled_next = page.locator(DISABLED_NEXT_PAGE_SELECTOR).first
                if next_btn.is_visible() and not disabled_next.is_visible():
                    _submit_and_wait_ajax(page, next_btn)
                else:
                    break

        except Exception as err:
            logger.warning(f"Error during Stage 2 retry for '{id_key}': {err}")

    logger.info(
        f"Stage 2 completed: Recovered {recovered_rcms} RCMs, "
        f"{recovered_fis} Leaflets, and {recovered_mmrs} MMRs."
    )
    return recovered_rcms, recovered_fis


def backfill_ema_and_missing_documents(
    page: Page,
    db_path: str = DB_PATH,
    target_url: str = TARGET_URL,
    download_dir_rcms: str = DOWNLOAD_DIR_RCMS,
    download_dir_leaflets: str = DOWNLOAD_DIR_LEAFLETS,
    download_dir_mmr: str = DOWNLOAD_DIR_MMR,
    limit: Optional[int] = None,
    substance: Optional[str] = None,
) -> Tuple[int, int]:
    """Search and download missing EMA and National documents.

    Pass 1 searches INFOMED by badge-stripped drug name across all paginator
    pages and upserts recoveries with ``current_sweep='EMA CAP Backfill'``.
    Pass 2 queries remaining missing documents (respecting ``substance`` and
    ``limit``) directly from EMA using INN-normalized EPAR URLs, copying an
    EPAR PDF to Leaflets only when the RCM itself was just fetched from EMA.

    Args:
        page: Playwright Page instance.
        db_path: Path to SQLite database file.
        target_url: INFOMED advanced search URL.
        download_dir_rcms: Directory for RCM PDFs.
        download_dir_leaflets: Directory for FI PDFs.
        download_dir_mmr: Directory for MMR PDFs.
        limit: Optional cap on distinct search names (Pass 1) and rows (Pass 2).
        substance: Optional active substance substring filter.

    Returns:
        Tuple of ``(recovered_rcms_count, recovered_fis_count)``.

    """
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if substance:
            cursor.execute(
                """
                SELECT DISTINCT drug_name, active_substance
                FROM medicamentos
                WHERE (COALESCE(has_rcm, 0) = 0 OR COALESCE(has_fi, 0) = 0
                       OR COALESCE(rcm_downloaded, 0) = 0
                       OR COALESCE(fi_downloaded, 0) = 0)
                  AND LOWER(active_substance) LIKE LOWER(?)
                ORDER BY drug_name
                """,
                (f"%{substance}%",),
            )
        else:
            cursor.execute(
                """
                SELECT DISTINCT drug_name, active_substance
                FROM medicamentos
                WHERE (COALESCE(has_rcm, 0) = 0 OR COALESCE(has_fi, 0) = 0
                       OR COALESCE(rcm_downloaded, 0) = 0
                       OR COALESCE(fi_downloaded, 0) = 0)
                ORDER BY drug_name
                """
            )
        rows = [dict(r) for r in cursor.fetchall()]

    # Deduplicate clean search terms while preserving order
    search_terms: List[str] = []
    seen_terms: Set[str] = set()
    for item in rows:
        raw_name = (item.get("drug_name") or "").strip()
        term = _build_clean_search_term(raw_name)
        if term and term.lower() not in seen_terms:
            seen_terms.add(term.lower())
            search_terms.append(term)

    if limit:
        search_terms = search_terms[:limit]

    logger.info(
        f"EMA Backfill: Found {len(search_terms)} distinct drug names to search "
        "on INFOMED."
    )

    recovered_rcms = 0
    recovered_fis = 0

    for idx, search_term in enumerate(search_terms, 1):
        logger.info(
            f"[{idx}/{len(search_terms)}] Searching '{search_term}' "
            "for EMA / missing documents..."
        )

        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_selector(
                DRUG_NAME_INPUT_SELECTOR, state="visible", timeout=15000
            )
            page.locator(DRUG_NAME_INPUT_SELECTOR).fill(search_term)
            _submit_and_wait_ajax(page, page.locator(SEARCH_BUTTON_SELECTOR))
            page.wait_for_selector(TABLE_BODY_SELECTOR, timeout=20000)

            extracted_records = _collect_paginated_rows(
                page=page,
                log_prefix=f"Backfill '{search_term}'",
                atc_meta=None,
                sweep_name="EMA CAP Backfill",
                download_dir_rcms=download_dir_rcms,
                download_dir_leaflets=download_dir_leaflets,
                download_dir_mmr=download_dir_mmr,
            )

            if extracted_records:
                _, new_r, new_f = upsert_medicamentos_batch(
                    extracted_records,
                    current_sweep="EMA CAP Backfill",
                    db_path=db_path,
                )
                recovered_rcms += new_r
                recovered_fis += new_f

        except Exception as err:
            logger.warning(f"Error backfilling '{search_term}': {err}")

    # Pass 2: Direct INN-normalized EMA EPAR Resolver for remaining missing documents
    logger.info(
        "Executing Direct EMA EPAR Resolver pass for remaining missing "
        "SmPCs/Leaflets..."
    )
    query_parts = [
        """
        SELECT id_key, med_id, drug_name, active_substance, mah,
               has_rcm, rcm_filename, rcm_downloaded,
               has_fi, fi_filename, fi_downloaded
        FROM medicamentos
        WHERE ((has_rcm = 1 AND COALESCE(rcm_downloaded, 0) = 0)
            OR (has_fi = 1 AND COALESCE(fi_downloaded, 0) = 0))
        """
    ]
    params: List[Any] = []
    if substance:
        query_parts.append("AND LOWER(active_substance) LIKE LOWER(?)")
        params.append(f"%{substance}%")
    query_parts.append("ORDER BY id_key")
    if limit:
        query_parts.append("LIMIT ?")
        params.append(limit)

    with sqlite3.connect(db_path, timeout=30.0) as conn:
        unresolved = conn.execute("\n".join(query_parts), tuple(params)).fetchall()

        for item in unresolved:
            (
                id_k,
                m_id,
                d_name,
                a_sub,
                mah_val,
                h_rcm,
                rcm_fname_db,
                r_dl,
                h_fi,
                fi_fname_db,
                f_dl,
            ) = item
            if not d_name and not a_sub:
                continue

            base_name = (
                sanitize_filename(f"{m_id}_{d_name}")
                if m_id and d_name
                else sanitize_filename(id_k)
            )
            rcm_fname = rcm_fname_db or f"{base_name}.pdf"
            fi_fname = fi_fname_db or f"{base_name}_FI.pdf"
            rcm_path = os.path.join(download_dir_rcms, rcm_fname)
            fi_path = os.path.join(download_dir_leaflets, fi_fname)

            rcm_ok = r_dl == 1
            fi_ok = f_dl == 1
            rcm_fetched_from_ema = False

            if h_rcm and not rcm_ok:
                if fetch_ema_epar_document(
                    drug_name=d_name or "",
                    active_substance=a_sub,
                    mah=mah_val,
                    target_filepath=rcm_path,
                    doc_type="RCM",
                ):
                    rcm_ok = True
                    rcm_fetched_from_ema = True
                    recovered_rcms += 1

            if h_fi and not fi_ok:
                # Only copy rcm_path when freshly fetched as a combined EMA EPAR PDF
                if (
                    rcm_fetched_from_ema
                    and os.path.exists(rcm_path)
                    and not os.path.exists(fi_path)
                ):
                    parent_dir = os.path.dirname(fi_path)
                    if parent_dir:
                        os.makedirs(parent_dir, exist_ok=True)
                    tmp_fi = f"{fi_path}.tmp"
                    try:
                        shutil.copy2(rcm_path, tmp_fi)
                        if validate_pdf(tmp_fi):
                            os.replace(tmp_fi, fi_path)
                            fi_ok = True
                            recovered_fis += 1
                    except Exception:
                        pass
                    finally:
                        if os.path.exists(tmp_fi):
                            try:
                                os.remove(tmp_fi)
                            except OSError:
                                pass
                elif fetch_ema_epar_document(
                    drug_name=d_name or "",
                    active_substance=a_sub,
                    mah=mah_val,
                    target_filepath=fi_path,
                    doc_type="Leaflet",
                ):
                    fi_ok = True
                    recovered_fis += 1

            rcm_newly_recovered = rcm_ok and (r_dl != 1)
            fi_newly_recovered = fi_ok and (f_dl != 1)
            if rcm_newly_recovered or fi_newly_recovered:
                conn.execute(
                    """
                    UPDATE medicamentos
                    SET rcm_filename = CASE WHEN ? = 1 THEN ?
                                            ELSE rcm_filename END,
                        rcm_downloaded = ?,
                        rcm_verified = ?,
                        rcm_source_sweep = CASE WHEN ? = 1
                                                THEN 'EMA Direct EPAR Resolver'
                                                ELSE rcm_source_sweep END,
                        fi_filename = CASE WHEN ? = 1 THEN ?
                                           ELSE fi_filename END,
                        fi_downloaded = ?,
                        fi_verified = ?,
                        fi_source_sweep = CASE WHEN ? = 1
                                               THEN 'EMA Direct EPAR Resolver'
                                               ELSE fi_source_sweep END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id_key = ?
                    """,
                    (
                        1 if rcm_newly_recovered else 0,
                        rcm_fname,
                        1 if rcm_ok else 0,
                        1 if rcm_ok else 0,
                        1 if rcm_newly_recovered else 0,
                        1 if fi_newly_recovered else 0,
                        fi_fname,
                        1 if fi_ok else 0,
                        1 if fi_ok else 0,
                        1 if fi_newly_recovered else 0,
                        id_k,
                    ),
                )
        conn.commit()

    logger.info(
        f"EMA Backfill finished: Downloaded {recovered_rcms} RCMs and "
        f"{recovered_fis} FIs."
    )
    return recovered_rcms, recovered_fis
