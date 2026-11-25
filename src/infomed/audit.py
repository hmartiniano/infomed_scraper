"""Document integrity audit and executive benchmark reporting for INFOMED."""

import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

from infomed.config import (
    AUDIT_REPORT_FILE,
    DB_PATH,
    DOWNLOAD_DIR_LEAFLETS,
    DOWNLOAD_DIR_MMR,
    DOWNLOAD_DIR_RCMS,
)
from infomed.db import load_all_sweep_metrics
from infomed.ema import validate_pdf

logger = logging.getLogger("infomed")


def audit_documents_and_integrity(
    medicines: Dict[str, Dict[str, Any]],
    download_dir_rcms: str = DOWNLOAD_DIR_RCMS,
    download_dir_leaflets: str = DOWNLOAD_DIR_LEAFLETS,
    download_dir_mmr: str = DOWNLOAD_DIR_MMR,
    audit_report_file: Optional[str] = AUDIT_REPORT_FILE,
) -> Dict[str, Any]:
    """Audit all scraped medicines, document coverage, and file integrity.

    Args:
        medicines: Dictionary of medicine records keyed by ``id_key``.
        download_dir_rcms: Directory containing downloaded RCM PDFs.
        download_dir_leaflets: Directory containing downloaded FI PDFs.
        download_dir_mmr: Directory containing downloaded MMR PDFs.
        audit_report_file: Optional path to write JSON audit summary.

    Returns:
        Audit report dictionary summarizing coverage and disk integrity.

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

    auth_count = sum(
        1
        for m in medicines.values()
        if (m.get("aim_status") or "").strip() == "Autorizado"
    )
    caducado_count = sum(
        1 for m in medicines.values() if "Caducado" in (m.get("aim_status") or "")
    )
    revogado_count = sum(
        1 for m in medicines.values() if "Revogado" in (m.get("aim_status") or "")
    )
    comercializado_count = sum(
        1
        for m in medicines.values()
        if (m.get("commercialization") or "").strip() == "Comercializado"
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
        "intact_mmr_pdfs": intact_mmrs,
        "corrupted_mmr_pdfs": corrupted_mmrs,
        "total_pdfs_on_disk_all_folders": total_all_disk,
        "total_intact_pdfs_all_folders": intact_all_disk,
        "total_corrupted_pdfs_all_folders": corrupted_all_disk,
        "all_published_rcms_downloaded": (len(rcms_missing) == 0),
        "all_published_leaflets_downloaded": (len(fis_missing) == 0),
        "overall_integrity_rate_percent": (
            (intact_all_disk / total_all_disk * 100) if total_all_disk else 100.0
        ),
    }

    if audit_report_file:
        try:
            with open(audit_report_file, "w", encoding="utf-8") as f:
                json.dump(audit_summary, f, indent=2)
            logger.info(f"Saved audit report to '{audit_report_file}'.")
        except Exception as err:
            logger.error(f"Failed to save audit report: {err}")

    return audit_summary


def format_duration(seconds: float) -> str:
    """Format duration in seconds into human-readable ``mm:ss`` or ``hh:mm`` string.

    Args:
        seconds: Elapsed duration in seconds.

    Returns:
        Formatted duration string.

    """
    if seconds <= 0:
        return "00m 00s"
    mins, secs = divmod(int(seconds), 60)
    hrs, mins = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs:02d}h {mins:02d}m"
    return f"{mins:02d}m {secs:02d}s"


def _fmt_opt_int(val: Optional[int]) -> str:
    """Format an optional integer with thousands separators or ``'N/A'``."""
    return f"{val:,}" if isinstance(val, int) else "N/A"


def print_summary_table(
    audit: Dict[str, Any],
    db_path: str = DB_PATH,
    benchmark: Optional[Dict[str, Any]] = None,
    pipeline_walltime_seconds: Optional[float] = None,
) -> None:
    """Print the complete executive audit table with per-sweep document breakdown.

    Args:
        audit: Audit summary dictionary from ``audit_documents_and_integrity``.
        db_path: Path to SQLite database file.
        benchmark: Optional benchmark dict from ``fetch_portal_benchmark_stats``.
        pipeline_walltime_seconds: Total pipeline wall-clock time in seconds.

    """
    bench = benchmark or {}
    sweeps = load_all_sweep_metrics(db_path=db_path)

    total_drugs = audit.get("total_unique_drugs", 0)
    distinct_dci = audit.get("distinct_active_substances_dci", 0)
    off_dci = bench.get("official_active_substances_dci")
    off_meds = bench.get("official_marketed_medicines")
    off_pres = bench.get("official_marketed_presentations")
    last_upd = bench.get("portal_last_updated") or "Unknown"

    dci_cov_str = (
        f"({distinct_dci / off_dci * 100:.1f}% coverage)"
        if isinstance(off_dci, int) and off_dci > 0
        else "(N/A)"
    )

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
    corrupted_pct = (corrupted / tot_disk * 100) if tot_disk else 0.0
    integrity_pct = audit.get("overall_integrity_rate_percent", 100.0)

    rcm_disk = audit.get("total_rcm_pdfs_on_disk", 0)
    fi_disk = audit.get("total_leaflet_pdfs_on_disk", 0)
    mmr_disk = audit.get("total_mmr_pdfs_on_disk", 0)
    auth_cnt = audit.get("drugs_autorizado_status", 0)
    caduc_cnt = audit.get("drugs_caducado_status", 0) + audit.get(
        "drugs_revogado_status", 0
    )
    comerc_cnt = audit.get("drugs_comercializado_status", 0)

    wall_str = (
        format_duration(pipeline_walltime_seconds)
        if pipeline_walltime_seconds is not None
        else "N/A"
    )

    sep_thick = "=" * 108
    sep_thin = "-" * 108

    lines = [
        "",
        sep_thick,
        f"{'INFOMED MASTER AUDIT & COMPARISON REPORT':^108}",
        sep_thick,
        "  PORTAL OFFICIAL BENCHMARK (https://extranet.infarmed.pt/INFOMED-fo/index.xhtml)",
        f"  Portal Last Updated Date : {last_upd}",
        f"  Active Substances (DCI)  : {_fmt_opt_int(off_dci)}",
        f"  Marketed Medicines       : {_fmt_opt_int(off_meds)}",
        f"  Marketed Presentations   : {_fmt_opt_int(off_pres)}",
        f"  Pipeline Total Wall Time : {wall_str} (including all downloads & retries)",
        sep_thin,
        "  PER-SWEEP DOCUMENT YIELD & WALL-TIME BENCHMARK",
        f"  {'Sweep Dimension':<24} {'Categories':<12} {'Wall Time':<10} "
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
        lines.append("  No per-sweep metrics recorded yet.")

    lines.extend(
        [
            sep_thin,
            "  COMBINED DATABASE CATALOG & BENCHMARK COMPARISON",
            f"  Unique Medicines in DB   : {total_drugs:,} "
            f"(vs {_fmt_opt_int(off_meds)} official)",
            f"  Distinct DCIs in DB      : {distinct_dci:,} / "
            f"{_fmt_opt_int(off_dci)} {dci_cov_str}",
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
            f"({rcm_disk:,} RCMs + {fi_disk:,} Leaflets + {mmr_disk:,} MMRs)",
            f"  Corrupted Files on Disk  : {corrupted:,} ({corrupted_pct:.1f}%)",
            f"  Overall File Integrity   : {integrity_pct:.1f}% "
            "(Validated: %PDF-, %%EOF, OLE2)",
            sep_thick,
            "",
        ]
    )
    print("\n".join(lines))
