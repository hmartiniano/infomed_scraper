"""Unit tests for the Infomed RCM, Leaflet, and drug metadata scraper."""

import os

from infomed.main import (
    audit_documents_and_integrity,
    export_db_to_datasets,
    format_duration,
    init_db,
    load_all_medicamentos_from_db,
    load_all_sweep_metrics,
    load_atc_progress_from_db,
    mark_atc_processed_in_db,
    parse_cli_args,
    print_summary_table,
    sanitize_filename,
    save_sweep_metrics,
    upsert_medicamentos_batch,
    validate_pdf,
)

MINIMAL_VALID_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
    b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R/Resources<<>>>>endobj\n"
    b"xref\n"
    b"0 4\n"
    b"0000000000 65535 f \n"
    b"0000000009 00000 n \n"
    b"0000000052 00000 n \n"
    b"0000000101 00000 n \n"
    b"trailer<</Size 4/Root 1 0 R>>\n"
    b"startxref\n"
    b"178\n"
    b"%%EOF\n"
)


def test_sanitize_filename():
    """Verify filename sanitization cleans invalid path characters."""
    assert sanitize_filename("4083_Dulcolax gotas") == "4083_Dulcolax_gotas"
    assert (
        sanitize_filename("603863_Evacol (solution/drops)")
        == "603863_Evacol_solution_drops"
    )
    assert sanitize_filename("   invalid/file\\name?:*  ") == "invalid_file_name"


def test_format_duration():
    """Test format_duration converts seconds into clean strings."""
    assert format_duration(0) == "00m 00s"
    assert format_duration(65.4) == "01m 05s"
    assert format_duration(3665) == "01h 01m"


def test_validate_pdf_valid(tmp_path):
    """Test validate_pdf returns True for a valid dummy PDF."""
    pdf_file = tmp_path / "valid.pdf"
    pdf_file.write_bytes(MINIMAL_VALID_PDF)

    assert validate_pdf(str(pdf_file)) is True


def test_validate_pdf_invalid(tmp_path):
    """Test validate_pdf detects missing header, trailer, or undersized files."""
    assert validate_pdf(str(tmp_path / "nonexistent.pdf")) is False

    too_small = tmp_path / "small.pdf"
    too_small.write_bytes(b"%PDF-1.4 %%EOF")
    assert validate_pdf(str(too_small)) is False

    no_header = tmp_path / "no_header.pdf"
    no_header.write_bytes(b"HELLO WORLD\n" + b"x" * 600 + b"\n%%EOF\n")
    assert validate_pdf(str(no_header)) is False

    no_trailer = tmp_path / "no_trailer.pdf"
    no_trailer.write_bytes(b"%PDF-1.4\n" + b"x" * 600 + b"\nEND OF FILE\n")
    assert validate_pdf(str(no_trailer)) is False


def test_atc_progress_in_sqlite(tmp_path):
    """Test recording and loading ATC progress directly in SQLite."""
    db_path = str(tmp_path / "test_progress.db")
    init_db(db_path=db_path, auto_migrate=False)

    mark_atc_processed_in_db("REF_ATC_1", "Category 1", db_path=db_path)
    mark_atc_processed_in_db("REF_ATC_2", "Category 2", db_path=db_path)

    loaded = load_atc_progress_from_db(db_path=db_path)
    assert loaded == {"REF_ATC_1", "REF_ATC_2"}


def test_sweep_metrics_storage_with_provenance_and_runtime(tmp_path):
    """Test saving and loading per-sweep document statistics and runtimes."""
    db_path = str(tmp_path / "test_sweeps.db")
    init_db(db_path=db_path, auto_migrate=False)

    save_sweep_metrics(
        sweep_name="1. WHO ATC Traversal",
        total_categories=3193,
        categories_processed=3167,
        medicines_encountered=8900,
        new_medicines=8900,
        rcms_available=7265,
        rcms_downloaded=7202,
        new_rcms_downloaded=7202,
        leaflets_available=7268,
        leaflets_downloaded=7151,
        new_leaflets_downloaded=7151,
        runtime_seconds=2530.5,
        db_path=db_path,
    )

    sweeps = load_all_sweep_metrics(db_path=db_path)
    assert len(sweeps) == 1
    assert sweeps[0]["sweep_name"] == "1. WHO ATC Traversal"
    assert sweeps[0]["new_medicines"] == 8900
    assert sweeps[0]["new_rcms_downloaded"] == 7202
    assert sweeps[0]["new_leaflets_downloaded"] == 7151
    assert sweeps[0]["runtime_seconds"] == 2530.5


def test_sqlite_db_upsert_provenance_and_export(tmp_path):
    """Test SQLite upsert with provenance tagging and merging."""
    db_path = str(tmp_path / "test_medicamentos.db")
    json_path = str(tmp_path / "test_medicamentos.json")
    csv_path = str(tmp_path / "test_medicamentos.csv")

    init_db(db_path=db_path, auto_migrate=False)

    record_1 = {
        "id_key": "599044_Glimepirida",
        "med_id": "599044",
        "drug_name": "Glimepirida Aurovitas",
        "active_substance": "Glimepirida",
        "pharma_form": "Comprimido",
        "dosage": "2 mg",
        "mah": "Aurovitas Unipessoal, Lda.",
        "commercialization": "Comercializado",
        "aim_status": "Autorizado",
        "atc_codes": ["A10BB12"],
        "atc_labels": ["A10BB12 - glimepiride"],
        "has_rcm": True,
        "rcm_filename": "599044_Glimepirida.pdf",
        "rcm_downloaded": True,
        "rcm_verified": True,
        "rcm_source_sweep": "1. WHO ATC Traversal",
        "has_fi": True,
        "fi_filename": "599044_Glimepirida_FI.pdf",
        "fi_downloaded": True,
        "fi_verified": True,
        "fi_source_sweep": "1. WHO ATC Traversal",
        "has_mmr": False,
        "mmr_filename": None,
        "mmr_downloaded": False,
        "mmr_verified": False,
    }

    new_m, new_r, new_f = upsert_medicamentos_batch(
        [record_1], current_sweep="1. WHO ATC Traversal", db_path=db_path
    )
    assert new_m == 1
    assert new_r == 1
    assert new_f == 1

    loaded = load_all_medicamentos_from_db(db_path=db_path)
    assert "599044_Glimepirida" in loaded
    assert loaded["599044_Glimepirida"]["rcm_source_sweep"] == "1. WHO ATC Traversal"

    # Subsequent sweep discovers an MMR for the same drug
    record_1_update = {
        "id_key": "599044_Glimepirida",
        "med_id": "599044",
        "drug_name": "Glimepirida Aurovitas",
        "active_substance": "Glimepirida",
        "pharma_form": "Comprimido",
        "dosage": "2 mg",
        "mah": "Aurovitas Unipessoal, Lda.",
        "commercialization": "Comercializado",
        "aim_status": "Autorizado",
        "atc_codes": ["A10BD99"],
        "atc_labels": ["A10BD99 - glimepiride combination"],
        "has_rcm": True,
        "rcm_filename": "599044_Glimepirida.pdf",
        "rcm_downloaded": True,
        "rcm_verified": True,
        "has_fi": True,
        "fi_filename": "599044_Glimepirida_FI.pdf",
        "fi_downloaded": True,
        "fi_verified": True,
        "has_mmr": True,
        "mmr_filename": "599044_Glimepirida_MMR.pdf",
        "mmr_downloaded": True,
        "mmr_verified": True,
    }

    new_m2, new_r2, new_f2 = upsert_medicamentos_batch(
        [record_1_update], current_sweep="2. Dispensa Classes", db_path=db_path
    )
    assert new_m2 == 0
    assert new_r2 == 0
    assert new_f2 == 0

    loaded_merged = load_all_medicamentos_from_db(db_path=db_path)
    assert loaded_merged["599044_Glimepirida"]["rcm_source_sweep"] == (
        "1. WHO ATC Traversal"
    )
    assert loaded_merged["599044_Glimepirida"]["mmr_source_sweep"] == (
        "2. Dispensa Classes"
    )

    export_db_to_datasets(db_path=db_path, json_path=json_path, csv_path=csv_path)
    assert os.path.exists(json_path)
    assert os.path.exists(csv_path)


def test_audit_documents_and_integrity(tmp_path, monkeypatch):
    """Test audit report calculation covering RCMs, Leaflets, and file integrity."""
    audit_file = str(tmp_path / "audit_report.json")
    monkeypatch.setattr("infomed.main.AUDIT_REPORT_FILE", audit_file)

    rcm_dir = tmp_path / "rcms"
    leaflet_dir = tmp_path / "leaflets"
    mmr_dir = tmp_path / "mmr"
    rcm_dir.mkdir()
    leaflet_dir.mkdir()
    mmr_dir.mkdir()

    valid_rcm = rcm_dir / "valid_rcm.pdf"
    valid_rcm.write_bytes(MINIMAL_VALID_PDF)

    corrupted_fi = leaflet_dir / "corrupted_fi.pdf"
    corrupted_fi.write_bytes(b"INVALID PDF CONTENT")

    medicines = {
        "med_1": {
            "id_key": "med_1",
            "med_id": "1",
            "drug_name": "Drug 1",
            "active_substance": "Substance A",
            "aim_status": "Autorizado",
            "commercialization": "Comercializado",
            "has_rcm": True,
            "rcm_filename": "valid_rcm.pdf",
            "rcm_downloaded": True,
            "rcm_verified": True,
            "has_fi": True,
            "fi_filename": "corrupted_fi.pdf",
            "fi_downloaded": False,
            "fi_verified": False,
            "has_mmr": False,
        },
        "med_2": {
            "id_key": "med_2",
            "med_id": "2",
            "drug_name": "Drug 2",
            "active_substance": "Substance B",
            "aim_status": "Caducado",
            "commercialization": "Não Comercializado",
            "has_rcm": False,
            "has_fi": False,
            "has_mmr": False,
        },
    }

    audit = audit_documents_and_integrity(
        medicines,
        download_dir_rcms=str(rcm_dir),
        download_dir_leaflets=str(leaflet_dir),
        download_dir_mmr=str(mmr_dir),
    )

    assert audit["total_unique_drugs"] == 2
    assert audit["distinct_active_substances_dci"] == 2
    assert audit["drugs_autorizado_status"] == 1
    assert audit["drugs_caducado_status"] == 1
    assert audit["drugs_comercializado_status"] == 1
    assert audit["drugs_with_rcm_published_on_portal"] == 1
    assert audit["drugs_with_fi_published_on_portal"] == 1
    assert audit["total_rcm_pdfs_on_disk"] == 1
    assert audit["intact_rcm_pdfs"] == 1
    assert audit["total_leaflet_pdfs_on_disk"] == 1
    assert audit["corrupted_leaflet_pdfs"] == 1
    assert audit["total_pdfs_on_disk_all_folders"] == 2
    assert audit["total_intact_pdfs_all_folders"] == 1
    assert audit["total_corrupted_pdfs_all_folders"] == 1


def test_parse_cli_args(monkeypatch):
    """Test CLI argument parsing for multi-sweep flags."""
    monkeypatch.setattr(
        "sys.argv",
        ["main.py", "--sweep-all", "--dispensa", "--cft", "--no-headless"],
    )
    args = parse_cli_args()
    assert args.sweep_all is True
    assert args.sweep_dispensa is True
    assert args.sweep_cft is True
    assert args.headless is False


def test_print_summary_table(tmp_path, capsys):
    """Test printing the executive master summary table with per-sweep breakdown."""
    db_path = str(tmp_path / "summary_test.db")
    init_db(db_path=db_path, auto_migrate=False)

    save_sweep_metrics(
        sweep_name="1. WHO ATC Traversal",
        total_categories=3193,
        categories_processed=3167,
        medicines_encountered=8900,
        new_medicines=8900,
        rcms_available=7265,
        rcms_downloaded=7202,
        new_rcms_downloaded=7202,
        leaflets_available=7268,
        leaflets_downloaded=7151,
        new_leaflets_downloaded=7151,
        runtime_seconds=2530.0,
        db_path=db_path,
    )

    audit = {
        "total_unique_drugs": 8900,
        "distinct_active_substances_dci": 1565,
        "drugs_autorizado_status": 8200,
        "drugs_caducado_status": 400,
        "drugs_revogado_status": 300,
        "drugs_comercializado_status": 7500,
        "drugs_with_rcm_published_on_portal": 7265,
        "rcm_download_success_count": 7202,
        "rcm_missing_download_count": 63,
        "drugs_with_fi_published_on_portal": 7268,
        "fi_download_success_count": 7151,
        "fi_missing_download_count": 117,
        "total_rcm_pdfs_on_disk": 7531,
        "total_leaflet_pdfs_on_disk": 7224,
        "total_pdfs_on_disk_all_folders": 14755,
        "total_corrupted_pdfs_all_folders": 0,
        "overall_integrity_rate_percent": 100.0,
    }

    benchmark = {
        "portal_last_updated": "12/08/2026",
        "official_active_substances_dci": 1692,
        "official_marketed_medicines": 10426,
        "official_marketed_presentations": 12645,
    }

    print_summary_table(audit, db_path=db_path, benchmark=benchmark)
    captured = capsys.readouterr()

    assert "INFOMED MASTER AUDIT & COMPARISON REPORT" in captured.out
    assert "1,692" in captured.out
    assert "10,426" in captured.out
    assert "12/08/2026" in captured.out
    assert "1. WHO ATC Traversal" in captured.out
    assert "7,202" in captured.out
    assert "7,151" in captured.out
    assert "100.0%" in captured.out
