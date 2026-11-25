"""Unit tests for the Infomed RCM, Leaflet, and drug metadata scraper."""

import csv
import os
import sqlite3
from contextlib import nullcontext
from unittest.mock import MagicMock

import pytest

from infomed.main import (
    FI_ICON_SELECTOR,
    RCM_ICON_SELECTOR,
    audit_documents_and_integrity,
    backfill_ema_and_missing_documents,
    close_secondary_pages,
    download_single_document,
    export_db_to_datasets,
    extract_medicine_row,
    fetch_ema_epar_document,
    format_duration,
    generate_ema_candidate_slugs,
    init_db,
    load_all_medicamentos_from_db,
    load_all_sweep_metrics,
    load_atc_progress_from_db,
    load_progress_table,
    mark_atc_processed_in_db,
    mark_progress_item,
    parse_cli_args,
    print_summary_table,
    process_dimension_category,
    retrieve_infomed_rcms,
    sanitize_filename,
    save_sweep_metrics,
    strip_infomed_badges,
    translate_portuguese_as_to_inn,
    upsert_medicamentos_batch,
    validate_pdf,
    wait_for_primefaces_ajax,
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


def test_strip_infomed_badges():
    """Verify UI badge stripping removes uppercase badges without mangling doses."""
    assert strip_infomed_badges("Pregabalina SandozMG") == "Pregabalina Sandoz"
    assert strip_infomed_badges("EydenzeltMB") == "Eydenzelt"
    assert strip_infomed_badges("SlenytoMP") == "Slenyto"
    assert strip_infomed_badges("Paracetamol 500 mg") == "Paracetamol 500 mg"


def test_format_duration():
    """Test format_duration converts seconds into clean strings."""
    assert format_duration(0) == "00m 00s"
    assert format_duration(65.4) == "01m 05s"
    assert format_duration(3665) == "01h 01m"


def test_validate_pdf_valid(tmp_path):
    """Test validate_pdf returns True for valid PDF and legacy OLE2 .doc files."""
    pdf_file = tmp_path / "valid.pdf"
    pdf_file.write_bytes(MINIMAL_VALID_PDF)
    assert validate_pdf(str(pdf_file)) is True

    ole_file = tmp_path / "legacy.doc"
    ole_file.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 120)
    assert validate_pdf(str(ole_file)) is True


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


def test_progress_table_allowlist_validation(tmp_path):
    """Verify progress table helpers reject unapproved table names."""
    db_path = str(tmp_path / "test_allowlist.db")
    init_db(db_path=db_path, auto_migrate=False)

    with pytest.raises(ValueError, match="Invalid progress table name"):
        load_progress_table("medicamentos; DROP TABLE medicamentos", db_path=db_path)

    with pytest.raises(ValueError, match="Invalid progress table name"):
        mark_progress_item("unknown_table", "X", "Y", db_path=db_path)


def test_sweep_metrics_storage_with_provenance_and_runtime(tmp_path):
    """Test saving, accumulating, and loading per-sweep statistics."""
    db_path = str(tmp_path / "test_sweeps.db")
    init_db(db_path=db_path, auto_migrate=False)

    save_sweep_metrics(
        sweep_name="1. WHO ATC Traversal",
        total_categories=3193,
        categories_processed=3000,
        medicines_encountered=8000,
        new_medicines=8000,
        rcms_available=7000,
        rcms_downloaded=6900,
        new_rcms_downloaded=6900,
        leaflets_available=7000,
        leaflets_downloaded=6850,
        new_leaflets_downloaded=6850,
        runtime_seconds=2000.5,
        db_path=db_path,
    )

    # Resumed partial run accumulates onto prior stored totals
    save_sweep_metrics(
        sweep_name="1. WHO ATC Traversal",
        total_categories=3193,
        categories_processed=3193,
        medicines_encountered=900,
        new_medicines=900,
        rcms_available=265,
        rcms_downloaded=302,
        new_rcms_downloaded=302,
        leaflets_available=268,
        leaflets_downloaded=301,
        new_leaflets_downloaded=301,
        runtime_seconds=530.0,
        db_path=db_path,
        accumulate=True,
    )

    sweeps = load_all_sweep_metrics(db_path=db_path)
    assert len(sweeps) == 1
    assert sweeps[0]["categories_processed"] == 3193
    assert sweeps[0]["medicines_encountered"] == 8900
    assert sweeps[0]["new_medicines"] == 8900
    assert sweeps[0]["new_rcms_downloaded"] == 7202
    assert sweeps[0]["new_leaflets_downloaded"] == 7151
    assert sweeps[0]["runtime_seconds"] == pytest.approx(2530.5)


def test_sqlite_db_upsert_provenance_and_export(tmp_path):
    """Test SQLite upsert preserves existing flags/filenames and explicit provenance."""
    db_path = str(tmp_path / "test_medicamentos.db")
    json_path = str(tmp_path / "test_medicamentos.json")
    csv_path = str(tmp_path / "test_medicamentos.csv")

    init_db(db_path=db_path, auto_migrate=False)

    # Empty export writes a header-only CSV instead of skipping
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write("stale,data\n1,2\n")
    export_db_to_datasets(db_path=db_path, json_path=json_path, csv_path=csv_path)
    with open(csv_path, "r", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert len(rows) == 1
    assert rows[0][0] == "id_key"

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
        "fi_downloaded": False,
        "fi_verified": False,
        "has_mmr": False,
        "mmr_filename": None,
        "mmr_downloaded": False,
        "mmr_verified": False,
    }

    new_m, new_r, new_f = upsert_medicamentos_batch(
        [record_1], current_sweep="1. WHO ATC Traversal", db_path=db_path
    )
    assert (new_m, new_r, new_f) == (1, 1, 0)

    # Transient update omits RCM icon (has_rcm=False, rcm_filename=None) while
    # explicitly tagging FI provenance
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
        "has_rcm": False,
        "rcm_filename": None,
        "rcm_downloaded": False,
        "has_fi": True,
        "fi_filename": "599044_Glimepirida_FI.pdf",
        "fi_downloaded": True,
        "fi_source_sweep": "Explicit Backfill",
        "has_mmr": True,
        "mmr_filename": "599044_Glimepirida_MMR.pdf",
        "mmr_downloaded": True,
    }

    new_m2, new_r2, new_f2 = upsert_medicamentos_batch(
        [record_1_update], current_sweep="2. Dispensa Classes", db_path=db_path
    )
    assert (new_m2, new_r2, new_f2) == (0, 0, 1)

    loaded = load_all_medicamentos_from_db(db_path=db_path)["599044_Glimepirida"]
    assert loaded["has_rcm"] is True
    assert loaded["rcm_filename"] == "599044_Glimepirida.pdf"
    assert loaded["rcm_downloaded"] is True
    assert loaded["fi_source_sweep"] == "Explicit Backfill"
    assert loaded["mmr_source_sweep"] == "2. Dispensa Classes"

    export_db_to_datasets(db_path=db_path, json_path=json_path, csv_path=csv_path)
    assert os.path.exists(json_path)
    assert os.path.exists(csv_path)


def test_audit_documents_and_integrity(tmp_path, monkeypatch):
    """Test audit report calculation and exact Autorizado matching."""
    audit_file = str(tmp_path / "audit_report.json")
    monkeypatch.setattr("infomed.main.AUDIT_REPORT_FILE", audit_file)

    rcm_dir = tmp_path / "rcms"
    leaflet_dir = tmp_path / "leaflets"
    mmr_dir = tmp_path / "mmr"
    rcm_dir.mkdir()
    leaflet_dir.mkdir()
    mmr_dir.mkdir()

    (rcm_dir / "valid_rcm.pdf").write_bytes(MINIMAL_VALID_PDF)
    (leaflet_dir / "corrupted_fi.pdf").write_bytes(b"INVALID PDF CONTENT")

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
            "aim_status": "Não Autorizado",
            "commercialization": "Não Comercializado",
            "has_rcm": False,
            "has_fi": False,
            "has_mmr": False,
        },
        "med_3": {
            "id_key": "med_3",
            "med_id": "3",
            "drug_name": "Drug 3",
            "active_substance": "Substance C",
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

    assert audit["total_unique_drugs"] == 3
    assert audit["drugs_autorizado_status"] == 1
    assert audit["drugs_caducado_status"] == 1
    assert audit["total_pdfs_on_disk_all_folders"] == 2
    assert audit["total_intact_pdfs_all_folders"] == 1
    assert audit["total_corrupted_pdfs_all_folders"] == 1


def test_parse_cli_args(monkeypatch):
    """Test CLI argument parsing for multi-sweep and single-dimension flags."""
    monkeypatch.setattr("sys.argv", ["main.py"])
    default_args = parse_cli_args()
    assert default_args.sweep_cft is True
    assert default_args.sweep_atc is True
    assert default_args.sweep_ff is False

    monkeypatch.setattr("sys.argv", ["main.py", "--ff"])
    ff_only = parse_cli_args()
    assert ff_only.sweep_ff is True
    assert ff_only.sweep_cft is False
    assert ff_only.sweep_atc is False

    monkeypatch.setattr("sys.argv", ["main.py", "--skip-cft"])
    skip_cft = parse_cli_args()
    assert skip_cft.sweep_cft is False
    assert skip_cft.sweep_atc is True

    monkeypatch.setattr(
        "sys.argv",
        ["main.py", "--sweep-all", "--dispensa", "--cft", "--ff", "--no-headless"],
    )
    multi = parse_cli_args()
    assert multi.sweep_all is True
    assert multi.sweep_dispensa is True
    assert multi.sweep_cft is True
    assert multi.sweep_ff is True
    assert multi.headless is False


def test_print_summary_table(tmp_path, capsys):
    """Test executive summary table computes real corrupted % and handles N/A."""
    db_path = str(tmp_path / "summary_test.db")
    init_db(db_path=db_path, auto_migrate=False)

    audit = {
        "total_unique_drugs": 2,
        "distinct_active_substances_dci": 2,
        "drugs_autorizado_status": 2,
        "drugs_caducado_status": 0,
        "drugs_revogado_status": 0,
        "drugs_comercializado_status": 2,
        "drugs_with_rcm_published_on_portal": 2,
        "rcm_download_success_count": 2,
        "rcm_missing_download_count": 0,
        "drugs_with_fi_published_on_portal": 2,
        "fi_download_success_count": 1,
        "fi_missing_download_count": 1,
        "total_rcm_pdfs_on_disk": 2,
        "total_leaflet_pdfs_on_disk": 1,
        "total_mmr_pdfs_on_disk": 1,
        "total_pdfs_on_disk_all_folders": 4,
        "total_corrupted_pdfs_all_folders": 1,
        "overall_integrity_rate_percent": 75.0,
    }

    print_summary_table(audit, db_path=db_path, benchmark=None)
    out = capsys.readouterr().out

    assert "1 (25.0%)" in out
    assert "1 MMRs" in out
    assert "N/A" in out
    assert "12/08/2026" not in out
    assert "pdfinfo" not in out


def test_ema_selectors_defined():
    """Verify that RCM and FI selectors match both National and EMA element IDs."""
    assert "pesqAvancadaDatableRcmIcon" in RCM_ICON_SELECTOR
    assert "pesqAvancadaDatableEmaIcon" in RCM_ICON_SELECTOR
    assert "pesqAvancadaDatableFiIcon" in FI_ICON_SELECTOR
    assert "pesqAvancadaDatableEmaFiIcon" in FI_ICON_SELECTOR


def test_cli_parser_backfill_args(monkeypatch):
    """Test CLI argument parsing for EMA backfill options."""
    monkeypatch.setattr(
        "sys.argv",
        ["main.py", "--backfill-only", "--substance", "Siponimod", "--limit", "5"],
    )
    args = parse_cli_args()
    assert args.backfill_only is True
    assert args.skip_backfill is False
    assert args.substance == "Siponimod"
    assert args.limit == 5


def test_close_secondary_pages():
    """Test close_secondary_pages closes popup tabs without closing main page."""
    main_page = MagicMock()
    popup1 = MagicMock()
    popup2 = MagicMock()

    context = MagicMock()
    context.pages = [main_page, popup1, popup2]
    main_page.context = context

    close_secondary_pages(main_page)

    popup1.close.assert_called_once()
    popup2.close.assert_called_once()
    main_page.close.assert_not_called()


def test_wait_for_primefaces_ajax():
    """Verify wait_for_primefaces_ajax invokes JS check and swallows timeouts."""
    mock_page = MagicMock()
    wait_for_primefaces_ajax(mock_page, timeout_ms=5000)
    mock_page.wait_for_function.assert_called_once_with(
        "typeof PrimeFaces === 'undefined' || PrimeFaces.ajax.Queue.isEmpty()",
        timeout=5000,
    )

    mock_page.wait_for_function.side_effect = TimeoutError("timed out")
    wait_for_primefaces_ajax(mock_page)


def test_translate_portuguese_as_to_inn():
    """Verify Portuguese AS translation handles diacritics, stems, and combos."""
    assert translate_portuguese_as_to_inn("Pregabalina") == "pregabalin"
    assert translate_portuguese_as_to_inn("Lenalidomida") == "lenalidomide"
    assert translate_portuguese_as_to_inn("Olanzapina") == "olanzapine"
    assert translate_portuguese_as_to_inn("Aripiprazol") == "aripiprazole"
    assert (
        translate_portuguese_as_to_inn("Darunavir + Cobicistate")
        == "darunavir-cobicistat"
    )
    assert translate_portuguese_as_to_inn("Sevelâmero") == "sevelamer"
    assert (
        translate_portuguese_as_to_inn("Emtricitabina + Tenofovir")
        == "emtricitabine-tenofovir"
    )
    assert translate_portuguese_as_to_inn("Ambrisentano") == "ambrisentan"


def test_generate_ema_candidate_slugs():
    """Verify specificity ordering, MAH filtering, and parent-brand derivation."""
    slugs = generate_ema_candidate_slugs(
        drug_name="Pregabalina SandozMG",
        active_substance="Pregabalina",
        mah="Sandoz GmbH",
    )
    assert "pregabalin-sandoz" in slugs
    assert "pregabalina-sandoz" in slugs
    assert slugs.index("pregabalin-sandoz") < slugs.index("pregabalin")
    assert "pregabalin-pregabalina-sandoz" not in slugs

    organon_slugs = generate_ema_candidate_slugs(
        drug_name="Fosavance",
        active_substance="Ácido alendrónico",
        mah="N.V. Organon",
    )
    assert "fosavance-n" not in organon_slugs
    assert "fosavance-organon" in organon_slugs

    novorapid_slugs = generate_ema_candidate_slugs(
        drug_name="NovoRapid Penfill",
        active_substance="Insulina aspártico",
        mah="Novo Nordisk, A/S",
    )
    assert "novorapid-penfill" in novorapid_slugs
    assert "novorapid" in novorapid_slugs
    assert novorapid_slugs.index("novorapid-penfill") < novorapid_slugs.index(
        "novorapid"
    )


def test_fetch_ema_epar_document_success_and_cleanup(tmp_path, monkeypatch):
    """Verify atomic promotion on valid PDF and temp cleanup on invalid payload."""
    target = str(tmp_path / "test_doc.pdf")

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = MINIMAL_VALID_PDF
    mock_resp.__enter__.return_value = mock_resp

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: mock_resp)

    assert (
        fetch_ema_epar_document(
            drug_name="Prezista",
            active_substance="Darunavir",
            mah="Janssen",
            target_filepath=target,
        )
        is True
    )
    assert os.path.exists(target)
    assert not os.path.exists(f"{target}.tmp")

    # Bare filename without directory component + invalid payload cleans up temp file
    monkeypatch.chdir(tmp_path)
    bad_resp = MagicMock()
    bad_resp.status = 200
    bad_resp.read.return_value = b"<html>404 Not Found</html>"
    bad_resp.__enter__.return_value = bad_resp
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: bad_resp)

    assert (
        fetch_ema_epar_document(
            drug_name="MissingDrug",
            target_filepath="bare_invalid.pdf",
        )
        is False
    )
    assert not os.path.exists("bare_invalid.pdf")
    assert not os.path.exists("bare_invalid.pdf.tmp")


def test_download_single_document_atomic_cleanup(tmp_path):
    """Verify corrupted Playwright downloads are rejected and not kept on disk."""
    target = str(tmp_path / "rcms" / "bad.pdf")
    mock_icon = MagicMock()
    mock_page = MagicMock()
    mock_dl = MagicMock()

    def _save_bad(path):
        with open(path, "wb") as f:
            f.write(b"NOT_A_PDF")

    mock_dl.save_as.side_effect = _save_bad
    dl_ctx = MagicMock()
    dl_ctx.__enter__.return_value.value = mock_dl
    mock_page.expect_download.return_value = dl_ctx

    ok = download_single_document(mock_icon, target, mock_page, doc_type="RCM")
    assert ok is False
    assert not os.path.exists(target)
    assert not os.path.exists(f"{target}.tmp")


def test_extract_medicine_row_parses_truck_aim_and_docs(tmp_path, monkeypatch):
    """Verify extract_medicine_row extracts metadata and downloads present docs."""
    texts = [
        "12345",
        "Prezista",
        "Darunavir",
        "Comprimido",
        "800 mg",
        "Janssen-Cilag",
        "",
        "1",
    ]
    td_mocks = []
    for t in texts:
        m = MagicMock()
        m.inner_text.return_value = t
        td_mocks.append(m)

    row_mock = MagicMock()

    def _locator_side_effect(sel):
        loc = MagicMock()
        if sel == "td":
            loc.all.return_value = td_mocks
        elif "blueTruck" in sel:
            loc.count.return_value = 1
        elif sel == RCM_ICON_SELECTOR:
            loc.count.return_value = 1
        elif sel == FI_ICON_SELECTOR:
            loc.count.return_value = 0
        else:
            loc.count.return_value = 1
        return loc

    row_mock.locator.side_effect = _locator_side_effect
    monkeypatch.setattr(
        "infomed.scraper.download_single_document", lambda **kwargs: True
    )

    rec = extract_medicine_row(
        row=row_mock,
        atc={"value": "REF_CLASS_ATC:J05AE10", "label": "darunavir"},
        page=MagicMock(),
        sweep_name="1. CFT",
        download_dir_rcms=str(tmp_path),
        download_dir_leaflets=str(tmp_path),
        download_dir_mmr=str(tmp_path),
    )

    assert rec is not None
    assert rec["commercialization"] == "Comercializado"
    assert rec["aim_status"] == "Autorizado"
    assert rec["atc_codes"] == ["J05AE10"]
    assert rec["has_rcm"] is True and rec["rcm_downloaded"] is True
    assert rec["has_fi"] is False
    assert rec["has_mmr"] is True and rec["mmr_downloaded"] is True


def test_process_dimension_category_no_double_click_on_timeout(tmp_path):
    """Verify slow AJAX response timeout does not click search/next twice."""
    page = MagicMock()
    search_btn = MagicMock()
    search_btn.click.return_value = None

    next_btn = MagicMock()
    next_btn.is_visible.return_value = False
    disabled_next = MagicMock()
    disabled_next.is_visible.return_value = True

    empty_row = MagicMock()
    td_empty = MagicMock()
    td_empty.inner_text.return_value = "Sem resultados."
    empty_row.locator.return_value.all.return_value = [td_empty]

    def _page_locator(sel):
        loc = MagicMock()
        if sel == "button[id='mainForm:btnDoSearch']":
            return search_btn
        if "ui-paginator-next" in sel and "disabled" not in sel:
            loc.first = next_btn
        elif "disabled" in sel:
            loc.first = disabled_next
        elif sel.endswith("tr"):
            loc.all.return_value = [empty_row]
        return loc

    page.locator.side_effect = _page_locator

    # Simulate timeout inside expect_response AFTER click was dispatched
    class _TimeoutOnExit:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            raise TimeoutError("Slow server")

    page.expect_response.return_value = _TimeoutOnExit()

    records = process_dimension_category(
        page=page,
        selector="select[id='mainForm:ff_input']",
        category={"value": "10", "label": "Comprimido"},
        target_url="https://example.test",
        download_dir_rcms=str(tmp_path),
        download_dir_leaflets=str(tmp_path),
        download_dir_mmr=str(tmp_path),
    )

    assert records == []
    search_btn.click.assert_called_once()


def test_backfill_pass2_filters_and_no_national_smpc_copy(tmp_path, monkeypatch):
    """Verify Pass 2 honors substance/limit and never copies national SmPCs."""
    db_path = str(tmp_path / "test_backfill.db")
    rcm_dir = tmp_path / "rcms"
    fi_dir = tmp_path / "leaflets"
    mmr_dir = tmp_path / "mmr"
    rcm_dir.mkdir()
    fi_dir.mkdir()
    mmr_dir.mkdir()

    init_db(db_path=db_path, auto_migrate=False)

    # Row 1: Already has national SmPC downloaded (rcm_downloaded=1), missing FI.
    # Pass 2 MUST NOT copy the national SmPC to leaflets!
    (rcm_dir / "100_NationalDrug.pdf").write_bytes(MINIMAL_VALID_PDF)
    # Row 2: Different substance, missing RCM; limit=1 or substance filter should skip.
    upsert_medicamentos_batch(
        [
            {
                "id_key": "100_NationalDrug_50mg_Comp",
                "med_id": "100",
                "drug_name": "NationalDrug",
                "active_substance": "TargetSubst",
                "mah": "LocalLab",
                "has_rcm": True,
                "rcm_filename": "100_NationalDrug.pdf",
                "rcm_downloaded": True,
                "rcm_source_sweep": "1. CFT",
                "has_fi": True,
                "fi_filename": None,
                "fi_downloaded": False,
            },
            {
                "id_key": "200_EmaDrug_10mg_Comp",
                "med_id": "200",
                "drug_name": "EmaDrug",
                "active_substance": "TargetSubst",
                "mah": "EmaCorp",
                "has_rcm": True,
                "rcm_filename": None,
                "rcm_downloaded": False,
                "has_fi": True,
                "fi_filename": None,
                "fi_downloaded": False,
            },
            {
                "id_key": "300_OtherDrug_10mg_Comp",
                "med_id": "300",
                "drug_name": "OtherDrug",
                "active_substance": "OtherSubst",
                "mah": "OtherCorp",
                "has_rcm": True,
                "rcm_filename": None,
                "rcm_downloaded": False,
                "has_fi": False,
                "fi_downloaded": False,
            },
        ],
        current_sweep="1. CFT",
        db_path=db_path,
    )

    # Mock Pass 1 browser search returning no rows, and mock EMA EPAR fetcher
    mock_page = MagicMock()
    mock_page.locator.return_value.count.return_value = 0
    monkeypatch.setattr("infomed.scraper._submit_and_wait_ajax", lambda p, loc: None)

    fetched_calls = []

    def _fake_fetch(drug_name, active_substance, mah, target_filepath, doc_type):
        fetched_calls.append((drug_name, doc_type))
        if drug_name == "EmaDrug" and doc_type == "RCM":
            with open(target_filepath, "wb") as f:
                f.write(MINIMAL_VALID_PDF)
            return True
        return False

    monkeypatch.setattr("infomed.scraper.fetch_ema_epar_document", _fake_fetch)

    rec_rcm, rec_fi = backfill_ema_and_missing_documents(
        page=mock_page,
        db_path=db_path,
        target_url="https://example.test",
        download_dir_rcms=str(rcm_dir),
        download_dir_leaflets=str(fi_dir),
        download_dir_mmr=str(mmr_dir),
        substance="TargetSubst",
        limit=2,
    )

    assert (rec_rcm, rec_fi) == (1, 1)
    # NationalDrug FI was attempted via EMA fetch (and failed), NEVER copied from SmPC
    assert ("NationalDrug", "Leaflet") in fetched_calls
    assert not (fi_dir / "100_NationalDrug_FI.pdf").exists()
    # EmaDrug RCM was fetched from EMA and copied to Leaflet
    assert (rcm_dir / "200_EmaDrug.pdf").exists()
    assert (fi_dir / "200_EmaDrug_FI.pdf").exists()
    # OtherDrug excluded by substance filter
    assert all(name != "OtherDrug" for name, _ in fetched_calls)

    with sqlite3.connect(db_path) as conn:
        row2 = conn.execute(
            "SELECT rcm_filename, fi_filename, rcm_source_sweep, fi_source_sweep "
            "FROM medicamentos WHERE med_id = '200'"
        ).fetchone()
    assert row2 == (
        "200_EmaDrug.pdf",
        "200_EmaDrug_FI.pdf",
        "EMA Direct EPAR Resolver",
        "EMA Direct EPAR Resolver",
    )


def test_stage2_only_skips_stage3_backfill(tmp_path, monkeypatch):
    """Verify retrieve_infomed_rcms(stage_2_only=True) does not run Stage 3."""
    db_path = str(tmp_path / "stage2_only.db")
    monkeypatch.setattr("infomed.main.AUDIT_REPORT_FILE", str(tmp_path / "audit.json"))

    mock_pw_ctx = MagicMock()
    mock_pw_ctx.__enter__.return_value = MagicMock()
    monkeypatch.setattr("infomed.main.sync_playwright", lambda: mock_pw_ctx)

    mock_page = MagicMock()
    mock_page.is_closed.return_value = False
    monkeypatch.setattr(
        "infomed.main.create_browser_session",
        lambda p, headless=True: (MagicMock(), MagicMock(), mock_page),
    )
    monkeypatch.setattr("infomed.main.fetch_portal_benchmark_stats", lambda page: {})
    monkeypatch.setattr("infomed.main.print_summary_table", lambda *a, **kw: None)

    stage2_called = []
    stage3_called = []
    monkeypatch.setattr(
        "infomed.main.retry_missing_documents",
        lambda *a, **kw: stage2_called.append(True) or (0, 0),
    )
    monkeypatch.setattr(
        "infomed.main.backfill_ema_and_missing_documents",
        lambda *a, **kw: stage3_called.append(True) or (0, 0),
    )
    monkeypatch.setattr("infomed.main.export_db_to_datasets", lambda **kw: None)
    _ = nullcontext()

    retrieve_infomed_rcms(db_path=db_path, stage_2_only=True)
    assert stage2_called == [True]
    assert stage3_called == []
