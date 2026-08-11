"""Unit tests for the Infomed RCM, Leaflet, and drug metadata scraper."""

import os

from infomed.main import (
    audit_documents_and_integrity,
    export_db_to_datasets,
    init_db,
    load_all_medicamentos_from_db,
    load_atc_progress_from_db,
    mark_atc_processed_in_db,
    sanitize_filename,
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


def test_sqlite_db_upsert_and_export(tmp_path):
    """Test SQLite database initialization, upsert with merging, and export."""
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
        "has_fi": True,
        "fi_filename": "599044_Glimepirida_FI.pdf",
        "fi_downloaded": True,
        "fi_verified": True,
        "has_mmr": False,
        "mmr_filename": None,
        "mmr_downloaded": False,
        "mmr_verified": False,
    }

    upsert_medicamentos_batch([record_1], db_path=db_path)

    loaded = load_all_medicamentos_from_db(db_path=db_path)
    assert "599044_Glimepirida" in loaded
    assert loaded["599044_Glimepirida"]["drug_name"] == "Glimepirida Aurovitas"
    assert loaded["599044_Glimepirida"]["atc_codes"] == ["A10BB12"]

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
        "has_mmr": False,
        "mmr_filename": None,
        "mmr_downloaded": False,
        "mmr_verified": False,
    }

    upsert_medicamentos_batch([record_1_update], db_path=db_path)

    loaded_merged = load_all_medicamentos_from_db(db_path=db_path)
    assert len(loaded_merged) == 1
    assert "A10BB12" in loaded_merged["599044_Glimepirida"]["atc_codes"]
    assert "A10BD99" in loaded_merged["599044_Glimepirida"]["atc_codes"]

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
    """Test CLI argument parsing for stage2 flag."""
    from infomed.main import parse_cli_args

    monkeypatch.setattr("sys.argv", ["main.py", "--stage2", "--no-headless"])
    args = parse_cli_args()
    assert args.stage_2_only is True
    assert args.headless is False
