"""Infomed RCM Scraper Module.

This module automates the extraction of RCM download links and files
from the INFOMED extranet JSF application using Playwright.
"""

import json
import logging
import os
import re
import sys
from typing import Dict, List, Optional, Set

from playwright.sync_api import Page, sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("infomed")

TARGET_URL = "https://extranet.infarmed.pt/INFOMED-fo/pesquisa-avancada.xhtml"
PROGRESS_FILE = "atc_progress.json"
OUTPUT_FILE = "rcm_links.txt"
DOWNLOAD_DIR = "downloads/rcms"

# DOM Selectors for PrimeFaces JSF
ATC_DROPDOWN_SELECTOR = "select[id='mainForm:classif-atc_input']"
SEARCH_BUTTON_SELECTOR = "button[id='mainForm:btnDoSearch']"
RESULTS_TABLE_SELECTOR = "div[id='mainForm:dt-medicamentos']"
TABLE_BODY_SELECTOR = "tbody[id='mainForm:dt-medicamentos_data']"
NEXT_PAGE_SELECTOR = "a.ui-paginator-next"
DISABLED_NEXT_PAGE_SELECTOR = "a.ui-paginator-next.ui-state-disabled"
RCM_ICON_SELECTOR = "a[id*='pesqAvancadaDatableRcmIcon']"


def load_progress() -> Dict[str, Set[str]]:
    """Load previously processed ATC codes, extracted URLs, and downloaded files.

    Returns:
        Dict containing sets of processed ATCs, URLs, and downloaded files.

    """
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {
                    "processed_atcs": set(data.get("processed_atcs", [])),
                    "urls": set(data.get("urls", [])),
                    "downloaded_files": set(data.get("downloaded_files", [])),
                }
        except Exception as err:
            logger.warning(f"Failed to load progress file: {err}")
    return {"processed_atcs": set(), "urls": set(), "downloaded_files": set()}


def save_progress(
    processed_atcs: Set[str],
    urls: Set[str],
    downloaded_files: Optional[Set[str]] = None,
) -> None:
    """Save current execution progress to file.

    Args:
        processed_atcs: Set of ATC category values already processed.
        urls: Set of all extracted RCM URLs.
        downloaded_files: Set of downloaded file names.

    """
    if downloaded_files is None:
        downloaded_files = set()
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "processed_atcs": list(processed_atcs),
                    "urls": list(urls),
                    "downloaded_files": list(downloaded_files),
                },
                f,
                indent=2,
            )
    except Exception as err:
        logger.error(f"Failed to save progress: {err}")


def save_output_urls(urls: Set[str]) -> None:
    """Write extracted RCM URLs to target output file.

    Args:
        urls: Set of unique RCM document URLs.

    """
    try:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            for url in sorted(urls):
                f.write(f"{url}\n")
        logger.info(f"Saved {len(urls)} unique RCM URLs to '{OUTPUT_FILE}'.")
    except Exception as err:
        logger.error(f"Failed to save output URLs to file: {err}")


def sanitize_filename(name: str) -> str:
    """Sanitize string to create a safe cross-platform filename.

    Args:
        name: Raw input text string.

    Returns:
        Sanitized string suitable for filenames.

    """
    clean = re.sub(r"[^\w\.-]", "_", name.strip())
    clean = re.sub(r"_+", "_", clean).strip("_")
    return clean or "rcm_document"


def extract_atc_categories(page: Page) -> List[Dict[str, str]]:
    """Extract all available ATC categories from the PrimeFaces select dropdown.

    Args:
        page: Playwright Page instance.

    Returns:
        List of dicts with 'value' and 'label' for valid ATC options.

    """
    page.wait_for_selector(ATC_DROPDOWN_SELECTOR, state="attached")
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
        # Standard select attempt
        page.select_option(ATC_DROPDOWN_SELECTOR, atc_value, timeout=5000)
    except Exception:
        # Fallback to PrimeFaces client-side API call
        page.evaluate(
            f"if (window.PF && PF('widget_mainForm_classif_atc')) {{ "
            f"  PF('widget_mainForm_classif_atc').selectValue('{atc_value}'); "
            f"}}"
        )


def process_atc_category(
    page: Page,
    atc: Dict[str, str],
    target_url: str,
    downloaded_files: Optional[Set[str]] = None,
    download_dir: str = DOWNLOAD_DIR,
) -> List[str]:
    """Execute search for a single ATC category and extract all RCM links across pages.

    Args:
        page: Playwright Page instance.
        atc: Dict containing 'value' and 'label'.
        target_url: URL to reload in case of failure.
        downloaded_files: Set of filenames already downloaded.
        download_dir: Directory where downloaded RCM documents will be saved.

    Returns:
        List of extracted RCM link URLs for this category.

    """
    if downloaded_files is None:
        downloaded_files = set()

    cat_value = atc["value"]
    cat_label = atc["label"]
    logger.info(f"Querying ATC: {cat_label} ({cat_value})")

    found_links: List[str] = []

    # Ensure search form is ready
    page.wait_for_selector(SEARCH_BUTTON_SELECTOR, state="visible", timeout=15000)

    # Select ATC value
    select_atc_option(page, cat_value)

    # Click search button
    page.locator(SEARCH_BUTTON_SELECTOR).click(timeout=10000)

    # Wait for results table to load
    page.wait_for_selector(RESULTS_TABLE_SELECTOR, state="visible", timeout=15000)
    page.wait_for_timeout(1000)

    os.makedirs(download_dir, exist_ok=True)

    page_num = 1
    while True:
        # Find table rows in current page table
        rows = page.locator(f"{TABLE_BODY_SELECTOR} tr").all()
        rcm_count_on_page = 0

        for row in rows:
            rcm_icon = row.locator(RCM_ICON_SELECTOR)
            if rcm_icon.count() == 0:
                continue

            rcm_count_on_page += 1
            try:
                cells = [
                    c.inner_text().strip().replace("\n", " ")
                    for c in row.locator("td").all()
                ]
                reg_num = cells[0] if len(cells) > 0 else ""
                med_name = cells[1] if len(cells) > 1 else ""

                if reg_num and med_name:
                    base_name = sanitize_filename(f"{reg_num}_{med_name}")
                elif reg_num:
                    base_name = sanitize_filename(f"{reg_num}_RCM")
                elif med_name:
                    base_name = sanitize_filename(f"RCM_{med_name}")
                else:
                    base_name = f"rcm_{len(found_links) + 1}"

                filename = f"{base_name}.pdf"
                target_filepath = os.path.join(download_dir, filename)

                if os.path.exists(target_filepath) or filename in downloaded_files:
                    logger.info(f"Skipping already downloaded file: '{filename}'")
                else:
                    with page.expect_download(timeout=10000) as download_info:
                        rcm_icon.first.click()
                    download = download_info.value
                    if download.url:
                        found_links.append(download.url)
                    download.save_as(target_filepath)
                    downloaded_files.add(filename)
                    logger.info(f"Downloaded RCM document to '{target_filepath}'")
            except Exception as err:
                logger.debug(f"Download trigger note: {err}")

        logger.info(
            f"ATC {cat_value} - Page {page_num}: "
            f"Processed {rcm_count_on_page} RCM items."
        )

        # Check if next page button exists and is active
        next_button = page.locator(NEXT_PAGE_SELECTOR).first
        disabled_next = page.locator(DISABLED_NEXT_PAGE_SELECTOR).first

        if next_button.is_visible() and not disabled_next.is_visible():
            page_num += 1
            # Click next page and wait for AJAX update
            next_button.click(timeout=10000)
            page.wait_for_timeout(1000)
        else:
            break

    # Reload page to reset form state for next iteration
    page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
    page.wait_for_selector(SEARCH_BUTTON_SELECTOR, state="visible", timeout=15000)
    return found_links


def retrieve_infomed_rcms(headless: bool = True) -> Set[str]:
    """Retrieve all RCM links from INFOMED extranet across all ATC categories.

    Args:
        headless: Whether to run Playwright in headless mode.

    Returns:
        Set of unique RCM document URLs.

    """
    progress = load_progress()
    processed_atcs: Set[str] = progress["processed_atcs"]
    all_rcm_urls: Set[str] = progress["urls"]
    downloaded_files: Set[str] = progress.get("downloaded_files", set())

    logger.info(
        f"Resuming scraping: {len(processed_atcs)} ATCs processed, "
        f"{len(all_rcm_urls)} URLs found, {len(downloaded_files)} files saved so far."
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.set_default_timeout(15000)

        logger.info(f"Opening target URL: {TARGET_URL}")
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)

        atc_categories = extract_atc_categories(page)

        for atc in atc_categories:
            atc_val = atc["value"]
            if atc_val in processed_atcs:
                continue

            try:
                links = process_atc_category(
                    page,
                    atc,
                    TARGET_URL,
                    downloaded_files=downloaded_files,
                )
                all_rcm_urls.update(links)
                processed_atcs.add(atc_val)
                save_progress(processed_atcs, all_rcm_urls, downloaded_files)
            except Exception as err:
                logger.error(f"Error processing ATC '{atc_val}': {err}")
                # Reset ViewState by reloading page with clean session recovery
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

        browser.close()

    save_output_urls(all_rcm_urls)
    return all_rcm_urls


if __name__ == "__main__":
    retrieve_infomed_rcms(headless=True)
