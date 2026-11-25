"""Infomed RCM, Patient Leaflet, and Drug Metadata Scraper Orchestrator & CLI.

Re-exports all public symbols from the modularized submodules (``config``,
``db``, ``ema``, ``scraper``, ``audit``) for backwards compatibility.
"""

import argparse
import logging
import sys
import time
from typing import Any, Dict, Optional

from playwright.sync_api import sync_playwright

from infomed.audit import (
    audit_documents_and_integrity as _audit_documents_and_integrity,
)
from infomed.audit import format_duration, print_summary_table
from infomed.config import (
    AIM_DROPDOWN_SELECTOR,
    ALLOWED_PROGRESS_TABLES,
    ATC_DROPDOWN_SELECTOR,
    AUDIT_REPORT_FILE,
    BROWSER_RECYCLE_INTERVAL,
    CFT_DROPDOWN_SELECTOR,
    CHROMIUM_LOW_MEM_ARGS,
    COMERC_DROPDOWN_SELECTOR,
    CSV_FIELDNAMES,
    DB_PATH,
    DIMENSIONS,
    DISABLED_NEXT_PAGE_SELECTOR,
    DISPENSA_DROPDOWN_SELECTOR,
    DOWNLOAD_DIR_LEAFLETS,
    DOWNLOAD_DIR_MMR,
    DOWNLOAD_DIR_RCMS,
    DRUG_NAME_INPUT_SELECTOR,
    EXIST_MMR_DROPDOWN_SELECTOR,
    FF_DROPDOWN_SELECTOR,
    FI_ICON_SELECTOR,
    GENERICO_DROPDOWN_SELECTOR,
    GRUPO_PRODUTO_DROPDOWN_SELECTOR,
    HOMEPAGE_URL,
    MARGEM_TERAP_DROPDOWN_SELECTOR,
    MEDICAMENTOS_CSV,
    MEDICAMENTOS_JSON,
    MMR_ICON_SELECTOR,
    MONIT_ADICIONAL_DROPDOWN_SELECTOR,
    NEXT_PAGE_SELECTOR,
    PROGRESS_FILE,
    RCM_ICON_SELECTOR,
    REG_NUMBER_INPUT_SELECTOR,
    RESULTS_TABLE_SELECTOR,
    SEARCH_BUTTON_SELECTOR,
    TABLE_BODY_SELECTOR,
    TARGET_URL,
    VIAS_ADMIN_DROPDOWN_SELECTOR,
)
from infomed.db import (
    export_db_to_datasets,
    init_db,
    load_all_medicamentos_from_db,
    load_all_sweep_metrics,
    load_atc_progress_from_db,
    load_progress_table,
    mark_atc_processed_in_db,
    mark_progress_item,
    save_sweep_metrics,
    upsert_medicamentos_batch,
)
from infomed.ema import (
    fetch_ema_epar_document,
    generate_ema_candidate_slugs,
    sanitize_filename,
    strip_infomed_badges,
    translate_portuguese_as_to_inn,
    validate_pdf,
)
from infomed.scraper import (
    backfill_ema_and_missing_documents,
    close_secondary_pages,
    create_browser_session,
    download_single_document,
    extract_dropdown_options,
    extract_medicine_row,
    fetch_portal_benchmark_stats,
    process_dimension_category,
    retry_missing_documents,
    run_dimension_sweep,
    select_dropdown_option,
    wait_for_primefaces_ajax,
)

logger = logging.getLogger("infomed")

__all__ = [
    "AIM_DROPDOWN_SELECTOR",
    "ALLOWED_PROGRESS_TABLES",
    "ATC_DROPDOWN_SELECTOR",
    "AUDIT_REPORT_FILE",
    "BROWSER_RECYCLE_INTERVAL",
    "CFT_DROPDOWN_SELECTOR",
    "CHROMIUM_LOW_MEM_ARGS",
    "COMERC_DROPDOWN_SELECTOR",
    "CSV_FIELDNAMES",
    "DB_PATH",
    "DIMENSIONS",
    "DISABLED_NEXT_PAGE_SELECTOR",
    "DISPENSA_DROPDOWN_SELECTOR",
    "DOWNLOAD_DIR_LEAFLETS",
    "DOWNLOAD_DIR_MMR",
    "DOWNLOAD_DIR_RCMS",
    "DRUG_NAME_INPUT_SELECTOR",
    "EXIST_MMR_DROPDOWN_SELECTOR",
    "FF_DROPDOWN_SELECTOR",
    "FI_ICON_SELECTOR",
    "GENERICO_DROPDOWN_SELECTOR",
    "GRUPO_PRODUTO_DROPDOWN_SELECTOR",
    "HOMEPAGE_URL",
    "MARGEM_TERAP_DROPDOWN_SELECTOR",
    "MEDICAMENTOS_CSV",
    "MEDICAMENTOS_JSON",
    "MMR_ICON_SELECTOR",
    "MONIT_ADICIONAL_DROPDOWN_SELECTOR",
    "NEXT_PAGE_SELECTOR",
    "PROGRESS_FILE",
    "RCM_ICON_SELECTOR",
    "REG_NUMBER_INPUT_SELECTOR",
    "RESULTS_TABLE_SELECTOR",
    "SEARCH_BUTTON_SELECTOR",
    "TABLE_BODY_SELECTOR",
    "TARGET_URL",
    "VIAS_ADMIN_DROPDOWN_SELECTOR",
    "audit_documents_and_integrity",
    "backfill_ema_and_missing_documents",
    "close_secondary_pages",
    "create_browser_session",
    "download_single_document",
    "export_db_to_datasets",
    "extract_dropdown_options",
    "extract_medicine_row",
    "fetch_ema_epar_document",
    "fetch_portal_benchmark_stats",
    "format_duration",
    "generate_ema_candidate_slugs",
    "init_db",
    "load_all_medicamentos_from_db",
    "load_all_sweep_metrics",
    "load_atc_progress_from_db",
    "load_progress_table",
    "main",
    "mark_atc_processed_in_db",
    "mark_progress_item",
    "parse_cli_args",
    "print_summary_table",
    "process_dimension_category",
    "retrieve_infomed_rcms",
    "retry_missing_documents",
    "run_dimension_sweep",
    "sanitize_filename",
    "save_sweep_metrics",
    "select_dropdown_option",
    "strip_infomed_badges",
    "translate_portuguese_as_to_inn",
    "upsert_medicamentos_batch",
    "validate_pdf",
    "wait_for_primefaces_ajax",
]


def audit_documents_and_integrity(
    medicines: Dict[str, Dict[str, Any]],
    download_dir_rcms: str = DOWNLOAD_DIR_RCMS,
    download_dir_leaflets: str = DOWNLOAD_DIR_LEAFLETS,
    download_dir_mmr: str = DOWNLOAD_DIR_MMR,
    audit_report_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Audit scraped medicines using the module-level ``AUDIT_REPORT_FILE`` default.

    Args:
        medicines: Dictionary of medicine records keyed by ``id_key``.
        download_dir_rcms: Directory containing RCM PDFs.
        download_dir_leaflets: Directory containing Leaflet PDFs.
        download_dir_mmr: Directory containing MMR PDFs.
        audit_report_file: Output JSON report path (defaults to ``AUDIT_REPORT_FILE``).

    Returns:
        Audit summary dictionary.

    """
    report_path = AUDIT_REPORT_FILE if audit_report_file is None else audit_report_file
    return _audit_documents_and_integrity(
        medicines=medicines,
        download_dir_rcms=download_dir_rcms,
        download_dir_leaflets=download_dir_leaflets,
        download_dir_mmr=download_dir_mmr,
        audit_report_file=report_path,
    )


def retrieve_infomed_rcms(
    headless: bool = True,
    db_path: str = DB_PATH,
    stage_2_only: bool = False,
    sweep_all: bool = False,
    sweep_dispensa: bool = False,
    sweep_cft: bool = True,
    sweep_atc: bool = True,
    sweep_ff: bool = False,
    sweep_via: bool = False,
    sweep_grupo: bool = False,
    sweep_aim: bool = False,
    sweep_comerc: bool = False,
    sweep_generico: bool = False,
    sweep_margem: bool = False,
    sweep_monit: bool = False,
    sweep_mmr: bool = False,
    skip_backfill: bool = False,
    backfill_only: bool = False,
    substance: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Retrieve all RCMs, Leaflets, and metadata with multi-sweep support.

    Args:
        headless: Whether to run Playwright in headless mode.
        db_path: Path to SQLite database file.
        stage_2_only: Whether to skip sweeps and run only Stage 2 targeted retry.
        sweep_all: Run full sweep across all 12 classification dimensions.
        sweep_dispensa: Sweep Dispensa classifications (8 categories).
        sweep_cft: Sweep Farmacoterapêutica classifications (380 categories).
        sweep_atc: Sweep WHO ATC classifications (3,193 categories).
        sweep_ff: Sweep Forma Farmacêutica classifications (339 categories).
        sweep_via: Sweep Via de Administração classifications (66 categories).
        sweep_grupo: Sweep Grupo de Produto classifications (18 categories).
        sweep_aim: Sweep Estado da AIM filters (4 categories).
        sweep_comerc: Sweep Estado de Comercialização filters (3 categories).
        sweep_generico: Sweep Genérico filters (2 categories).
        sweep_margem: Sweep Margem Terapêutica filters (2 categories).
        sweep_monit: Sweep Monitorização Adicional filters (2 categories).
        sweep_mmr: Sweep Existência de MMR filters (2 categories).
        skip_backfill: Skip automatic Stage 3 EMA and missing doc backfill pass.
        backfill_only: Run only the EMA and missing doc backfill without sweeps.
        substance: Optional active substance name filter for backfill.
        limit: Optional maximum number of medicines to query in backfill.

    Returns:
        Audit report dict summarizing scraped data and file integrity.

    """
    start_pipeline_walltime = time.time()
    init_db(db_path=db_path)

    existing_db_meds = load_all_medicamentos_from_db(db_path=db_path)
    logger.info(
        f"Starting pipeline with unified SQLite: {len(existing_db_meds)} drugs in DB."
    )

    sweep_flags = {
        "sweep_cft": sweep_cft,
        "sweep_atc": sweep_atc,
        "sweep_dispensa": sweep_dispensa,
        "sweep_ff": sweep_ff,
        "sweep_via": sweep_via,
        "sweep_grupo": sweep_grupo,
        "sweep_aim": sweep_aim,
        "sweep_comerc": sweep_comerc,
        "sweep_generico": sweep_generico,
        "sweep_margem": sweep_margem,
        "sweep_monit": sweep_monit,
        "sweep_mmr": sweep_mmr,
    }

    with sync_playwright() as p:
        browser, context, page = create_browser_session(p, headless=headless)

        # 1. Fetch live portal homepage benchmark totals
        benchmark = fetch_portal_benchmark_stats(page)
        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=20000)

        if not stage_2_only and not backfill_only:
            for attr_name, sweep_name, selector, progress_table in DIMENSIONS:
                if sweep_all or sweep_flags.get(attr_name, False):
                    browser, context, page = run_dimension_sweep(
                        sweep_name=sweep_name,
                        selector=selector,
                        progress_table=progress_table,
                        browser=browser,
                        context=context,
                        page=page,
                        p=p,
                        db_path=db_path,
                        headless=headless,
                    )

        # Stage 2: Retry Downloads of Missing Files
        if not backfill_only:
            if page.is_closed():
                browser, context, page = create_browser_session(p, headless=headless)

            retry_missing_documents(
                page,
                TARGET_URL,
                db_path=db_path,
                download_dir_rcms=DOWNLOAD_DIR_RCMS,
                download_dir_leaflets=DOWNLOAD_DIR_LEAFLETS,
                download_dir_mmr=DOWNLOAD_DIR_MMR,
            )

        # Stage 3: Automatic EMA & Missing Document Backfill
        # Runs by default on full sweeps or explicitly via --backfill-only,
        # but is skipped when --stage2 (retry-only) or --skip-backfill is set.
        if backfill_only or (not stage_2_only and not skip_backfill):
            if page.is_closed():
                browser, context, page = create_browser_session(p, headless=headless)

            backfill_ema_and_missing_documents(
                page,
                db_path=db_path,
                target_url=TARGET_URL,
                download_dir_rcms=DOWNLOAD_DIR_RCMS,
                download_dir_leaflets=DOWNLOAD_DIR_LEAFLETS,
                download_dir_mmr=DOWNLOAD_DIR_MMR,
                limit=limit,
                substance=substance,
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
    total_pipeline_walltime = time.time() - start_pipeline_walltime

    audit = audit_documents_and_integrity(
        all_final_meds,
        download_dir_rcms=DOWNLOAD_DIR_RCMS,
        download_dir_leaflets=DOWNLOAD_DIR_LEAFLETS,
        download_dir_mmr=DOWNLOAD_DIR_MMR,
    )
    audit["pipeline_total_walltime_seconds"] = total_pipeline_walltime
    print_summary_table(
        audit,
        db_path=db_path,
        benchmark=benchmark,
        pipeline_walltime_seconds=total_pipeline_walltime,
    )
    return audit


def parse_cli_args() -> argparse.Namespace:
    """Parse command-line arguments for the scraper.

    When no individual dimension flag is passed, defaults to running both
    CFT and WHO ATC sweeps (unless suppressed via ``--skip-cft``/``--skip-atc``).
    Passing an explicit dimension flag (e.g. ``--ff``) runs only the requested
    dimension(s) unless ``--cft`` or ``--atc`` is also specified.

    Returns:
        Parsed ``argparse.Namespace`` with resolved boolean sweep flags.

    """
    parser = argparse.ArgumentParser(
        description="INFOMED Scraper: Download RCMs, Leaflets, and drug metadata."
    )
    parser.add_argument(
        "--stage2",
        "--retry-only",
        action="store_true",
        dest="stage_2_only",
        help="Skip category sweeps and Stage 3 backfill; run only Stage 2 retry.",
    )
    parser.add_argument(
        "--sweep-all",
        action="store_true",
        dest="sweep_all",
        help="Execute sweeps across all 12 dimensions.",
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
        default=None,
        help="Execute sweep across CFT classification (380 categories).",
    )
    parser.add_argument(
        "--skip-cft",
        action="store_false",
        dest="sweep_cft",
        help="Skip default Classificação Farmacoterapêutica (CFT) sweep.",
    )
    parser.add_argument(
        "--atc",
        action="store_true",
        dest="sweep_atc",
        default=None,
        help="Execute sweep across WHO ATC (3,193 categories, default: enabled).",
    )
    parser.add_argument(
        "--skip-atc",
        action="store_false",
        dest="sweep_atc",
        help="Skip default WHO ATC classification sweep.",
    )
    parser.add_argument(
        "--ff",
        "--forma-farmaceutica",
        action="store_true",
        dest="sweep_ff",
        help="Execute sweep across Forma Farmacêutica (339 categories).",
    )
    parser.add_argument(
        "--via",
        "--via-admin",
        action="store_true",
        dest="sweep_via",
        help="Execute sweep across Via de Administração (66 categories).",
    )
    parser.add_argument(
        "--grupo",
        "--grupo-produto",
        action="store_true",
        dest="sweep_grupo",
        help="Execute sweep across Grupo de Produto (18 categories).",
    )
    parser.add_argument(
        "--aim",
        action="store_true",
        dest="sweep_aim",
        help="Execute sweep across Estado da AIM filters (4 categories).",
    )
    parser.add_argument(
        "--comerc",
        action="store_true",
        dest="sweep_comerc",
        help="Execute sweep across Estado de Comercialização filters (3 categories).",
    )
    parser.add_argument(
        "--generico",
        action="store_true",
        dest="sweep_generico",
        help="Execute sweep across Genérico (Sim/Não) filters (2 categories).",
    )
    parser.add_argument(
        "--margem",
        action="store_true",
        dest="sweep_margem",
        help="Execute sweep across Margem Terapêutica filters (2 categories).",
    )
    parser.add_argument(
        "--monit",
        action="store_true",
        dest="sweep_monit",
        help="Execute sweep across Monitorização Adicional filters (2 categories).",
    )
    parser.add_argument(
        "--mmr-docs",
        action="store_true",
        dest="sweep_mmr",
        help="Execute sweep across Documentos MMR filters (2 categories).",
    )
    parser.add_argument(
        "--skip-backfill",
        action="store_true",
        dest="skip_backfill",
        help="Skip automatic Stage 3 EMA and missing document backfill pass.",
    )
    parser.add_argument(
        "--backfill-only",
        "--backfill-ema-docs",
        action="store_true",
        dest="backfill_only",
        help="Run only the EMA and missing document backfill without sweeps.",
    )
    parser.add_argument(
        "--substance",
        type=str,
        default=None,
        help="Optional active substance filter for backfill (e.g. 'Siponimod').",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional limit on the number of medicines to query in backfill.",
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
    args = parser.parse_args()

    explicit_other_dim = any(
        [
            args.sweep_all,
            args.sweep_dispensa,
            args.sweep_ff,
            args.sweep_via,
            args.sweep_grupo,
            args.sweep_aim,
            args.sweep_comerc,
            args.sweep_generico,
            args.sweep_margem,
            args.sweep_monit,
            args.sweep_mmr,
        ]
    )
    any_explicit_dim = (
        explicit_other_dim or args.sweep_cft is True or args.sweep_atc is True
    )

    if args.sweep_cft is None:
        args.sweep_cft = not any_explicit_dim
    if args.sweep_atc is None:
        args.sweep_atc = not any_explicit_dim

    return args


def main() -> None:
    """CLI entrypoint for the INFOMED scraper pipeline."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    args = parse_cli_args()
    retrieve_infomed_rcms(
        headless=args.headless,
        db_path=args.db,
        stage_2_only=args.stage_2_only,
        sweep_all=args.sweep_all,
        sweep_dispensa=args.sweep_dispensa,
        sweep_cft=args.sweep_cft,
        sweep_atc=args.sweep_atc,
        sweep_ff=args.sweep_ff,
        sweep_via=args.sweep_via,
        sweep_grupo=args.sweep_grupo,
        sweep_aim=args.sweep_aim,
        sweep_comerc=args.sweep_comerc,
        sweep_generico=args.sweep_generico,
        sweep_margem=args.sweep_margem,
        sweep_monit=args.sweep_monit,
        sweep_mmr=args.sweep_mmr,
        skip_backfill=args.skip_backfill,
        backfill_only=args.backfill_only,
        substance=args.substance,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()
