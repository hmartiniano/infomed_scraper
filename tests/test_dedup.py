"""Unit tests for the RCM Deduplication and PGx Analysis module."""

import numpy as np

from infomed.db import init_db, upsert_medicamentos_batch
from infomed.dedup import (
    compute_similarity_matrix,
    deduplicate_substance,
    extract_clinical_sections_text,
    extract_raw_rcm_text,
    normalize_rcm_text,
    scan_pgx_markers,
)


def test_extract_clinical_sections_text():
    """Test isolation of Sections 4 and 5, skipping TOC and handling formats."""
    raw_doc = (
        "1. NOME DO MEDICAMENTO\n"
        "Clopidogrel Generis 75 mg Comprimidos\n"
        "2. COMPOSIÇÃO QUALITATIVA E QUANTITATIVA\n"
        "Contém lactose monohidratada.\n"
        "3. FORMA FARMACÊUTICA\n"
        "Comprimido revestido por película.\n"
        "4. INFORMAÇÕES CLÍNICAS\n"
        "4.1 Indicações terapêuticas: Prevenção de eventos aterotrombóticos.\n"
        "4.2 Posologia: 75 mg uma vez por dia.\n"
        "5. PROPRIEDADES FARMACOLÓGICAS\n"
        "5.1 Propriedades farmacodinâmicas: Inibidor da agregação plaquetária.\n"
        "5.2 Propriedades farmacocinéticas: Metabolizado pelo CYP2C19.\n"
        "6. INFORMAÇÕES FARMACÊUTICAS\n"
        "6.1 Lista dos excipientes: Celulose microcristalina.\n"
        "7. TITULAR DA AIM\n"
        "Generis Farmacêutica, S.A.\n"
    )
    clinical = extract_clinical_sections_text(raw_doc)
    assert "prevenção de eventos aterotrombóticos" in clinical
    assert "metabolizado pelo cyp2c19" in clinical
    assert "clopidogrel generis 75 mg comprimidos" not in clinical
    assert "lactose monohidratada" not in clinical
    assert "generis farmacêutica" not in clinical


def test_extract_clinical_sections_skips_epar_toc_and_merges_annexes():
    """Verify EMA EPAR Table of Contents is skipped and real Annex I extracted."""
    epar_doc = (
        "ÍNDICE\n"
        "4. INFORMAÇÕES CLÍNICAS ........... 4\n"
        "5. PROPRIEDADES FARMACOLÓGICAS ........... 10\n"
        "6. INFORMAÇÕES FARMACÊUTICAS ........... 12\n\n"
        "ANEXO I - RESUMO DAS CARACTERÍSTICAS DO MEDICAMENTO\n"
        "4. INFORMACOES CLINICAS\n"
        "4.1. Indicacoes terapeuticas: Tratamento de epilepsia.\n"
        "5.1. Propriedades farmacodinamicas: Liga-se a subunidade alfa2-delta.\n"
        "6.1. Lista dos excipientes: Amido de milho.\n\n"
        "SEGUNDA APRESENTAÇÃO\n"
        "4. INFORMAÇÕES CLÍNICAS\n"
        "4.1. Indicações terapêuticas: Dor neuropática periférica e central.\n"
        "6. DADOS FARMACÊUTICOS\n"
        "6.1. Lista dos excipientes: Talco.\n"
    )
    clinical = extract_clinical_sections_text(epar_doc)
    assert "tratamento de epilepsia" in clinical
    assert "dor neuropática periférica e central" in clinical
    assert "amido de milho" not in clinical
    assert "..........." not in clinical


def test_normalize_rcm_text():
    """Test header stripping and whitespace normalization."""
    raw = (
        "INFARMED - Autoridade Nacional do Medicamento e Produtos de Saúde, I.P.\n"
        "RESUMO DAS CARACTERÍSTICAS DO MEDICAMENTO\n"
        "1. NOME DO MEDICAMENTO\n"
        "Paracetamol Generis 500 mg Comprimidos\n"
        "Página 1 de 5\n"
        "12/08/2026\n"
    )
    cleaned = normalize_rcm_text(raw)
    assert "infarmed" not in cleaned
    assert "página 1 de 5" not in cleaned
    assert "12/08/2026" not in cleaned
    assert "paracetamol generis 500 mg comprimidos" in cleaned


def test_compute_similarity_matrix_and_empty_handling():
    """Test TF-IDF cosine similarity and zero cross-similarity for empty docs."""
    texts = [
        "paracetamol 500 mg comprimidos para tratamento de febre e dor ligeira",
        "paracetamol 500 mg comprimidos indicado para dor ligeira e febre",
        "ciprofloxacina 250 mg gotas oftálmicas para infeções bacterianas oculares",
    ]
    sim = compute_similarity_matrix(texts)
    assert sim.shape == (3, 3)
    for i in range(3):
        assert np.isclose(sim[i, i], 1.0)
    assert sim[0, 1] > 0.40
    assert sim[0, 2] < 0.10
    assert sim[0, 1] > sim[0, 2]

    # Empty / unreadable docs must NOT share similarity 1.0 with each other
    sim_empty = compute_similarity_matrix(["", "   ", texts[0]])
    assert np.isclose(sim_empty[0, 0], 1.0)
    assert np.isclose(sim_empty[1, 1], 1.0)
    assert np.isclose(sim_empty[0, 1], 0.0)
    assert np.isclose(sim_empty[0, 2], 0.0)


def test_scan_pgx_markers_and_decimals():
    """Test detection of CYP, HLA, single-spaced deficiency, and decimals."""
    text_clopidogrel = (
        "o clopidogrel é metabolizado pela enzima cyp2c19 na dose de 0.5 mg/kg. "
        "em doentes que são metabolizadores lentos da cyp2c19, "
        "a formação do metabolito ativo é diminuída. "
        "recomenda-se precaução em indivíduos com este polimorfismo genético."
    )
    pgx = scan_pgx_markers(text_clopidogrel)
    assert pgx["has_pgx"] is True
    assert "CYP2C19" in pgx["genes"]
    assert "Metabolizador Lento/Fraco" in pgx["phenotypes"]
    assert "Polimorfismo Genético" in pgx["terms"]
    assert any("0.5 mg/kg" in s for s in pgx["snippets"])

    text_deficiency = (
        "contraindicado em doentes com deficiência de g6pd ou "
        "deficiência parcial de dpd devido ao risco de toxicidade grave"
    )
    pgx_def = scan_pgx_markers(text_deficiency)
    assert "G6PD" in pgx_def["genes"]
    assert "DPYD" in pgx_def["genes"]
    assert "Deficiência Enzimática" in pgx_def["terms"]
    assert len(pgx_def["snippets"]) == 1

    text_hla = (
        "o rastreio do alelo hla-b*5701 deve ser realizado antes de iniciar "
        "o tratamento com abacavir devido ao risco de reação de hipersensibilidade."
    )
    assert "HLA-B*5701" in scan_pgx_markers(text_hla)["genes"]


def test_extract_raw_rcm_text_skips_ole2_doc(tmp_path):
    """Verify legacy OLE2 .doc binary files return empty string cleanly."""
    doc_path = tmp_path / "legacy.doc"
    doc_path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 200)
    assert extract_raw_rcm_text(str(doc_path)) == ""


def test_deduplicate_substance_no_documents(tmp_path):
    """Test deduplication returns unified schema and accurate missing reasons."""
    res_missing_db = deduplicate_substance(
        substance_name="NonExistentDrug",
        db_path=str(tmp_path / "empty.db"),
    )
    assert res_missing_db["decision"] == "NO_LOCAL_DOCUMENTS"
    assert res_missing_db["is_homogeneous"] is True
    assert res_missing_db["mode"] == "clinical_sections_4_and_5"
    assert "snippets" in res_missing_db["pgx_profile"]
    assert res_missing_db["medicines"] == []

    db_path = str(tmp_path / "reasons.db")
    init_db(db_path=db_path, auto_migrate=False)
    upsert_medicamentos_batch(
        [
            {
                "id_key": "1_SubA",
                "med_id": "1",
                "drug_name": "DrugA",
                "active_substance": "SubA",
                "mah": "SomePharma",
                "has_rcm": True,
                "rcm_downloaded": False,
            },
            {
                "id_key": "2_SubB",
                "med_id": "2",
                "drug_name": "DrugB",
                "active_substance": "SubB",
                "mah": "LocalLab",
                "has_rcm": False,
                "rcm_downloaded": False,
            },
        ],
        db_path=db_path,
    )
    res_a = deduplicate_substance("SubA", db_path=db_path, rcms_dir=str(tmp_path))
    assert "not downloaded locally" in res_a["reason"]
    res_b = deduplicate_substance("SubB", db_path=db_path, rcms_dir=str(tmp_path))
    assert "No SmPC published" in res_b["reason"]


def test_deduplicate_substance_singleton_collapse_and_heterogeneous(
    tmp_path, monkeypatch
):
    """Test singleton, deterministic medoid collapse, heterogeneous, and empty PDFs."""
    db_path = str(tmp_path / "dedup_cases.db")
    rcms_dir = tmp_path / "rcms"
    rcms_dir.mkdir()
    init_db(db_path=db_path, auto_migrate=False)

    # Create dummy PDF files on disk (contents intercepted via monkeypatch)
    for fname in (
        "1_S.pdf",
        "10_A.pdf",
        "20_B.pdf",
        "30_C.pdf",
        "40_E1.pdf",
        "50_E2.pdf",
    ):
        (rcms_dir / fname).write_bytes(b"%PDF-1.4\n%%EOF\n")

    upsert_medicamentos_batch(
        [
            {
                "id_key": "1_S",
                "med_id": "1",
                "drug_name": "SingleDrug",
                "active_substance": "SingleSub",
                "has_rcm": True,
                "rcm_filename": "1_S.pdf",
                "rcm_downloaded": True,
            },
            {
                "id_key": "10_A",
                "med_id": "10",
                "drug_name": "MedA",
                "active_substance": "CollapseSub",
                "has_rcm": True,
                "rcm_filename": "10_A.pdf",
                "rcm_downloaded": True,
            },
            {
                "id_key": "20_B",
                "med_id": "20",
                "drug_name": "MedB",
                "active_substance": "CollapseSub",
                "has_rcm": True,
                "rcm_filename": "20_B.pdf",
                "rcm_downloaded": True,
            },
            {
                "id_key": "30_C",
                "med_id": "30",
                "drug_name": "MedC",
                "active_substance": "CollapseSub",
                "has_rcm": True,
                "rcm_filename": "30_C.pdf",
                "rcm_downloaded": True,
            },
            {
                "id_key": "40_E1",
                "med_id": "40",
                "drug_name": "Empty1",
                "active_substance": "EmptySub",
                "has_rcm": True,
                "rcm_filename": "40_E1.pdf",
                "rcm_downloaded": True,
            },
            {
                "id_key": "50_E2",
                "med_id": "50",
                "drug_name": "Empty2",
                "active_substance": "EmptySub",
                "has_rcm": True,
                "rcm_filename": "50_E2.pdf",
                "rcm_downloaded": True,
            },
        ],
        db_path=db_path,
    )

    text_map = {
        str(rcms_dir / "1_S.pdf"): (
            "4. INFORMAÇÕES CLÍNICAS\n4.1 Tratamento da hipertensão arterial.\n"
            "6. INFORMAÇÕES FARMACÊUTICAS"
        ),
        # 20_B overlaps strongly with both 10_A and 30_C -> must be chosen as medoid
        str(rcms_dir / "10_A.pdf"): (
            "indicado para diabetes mellitus tipo 2 adultos controlo glicémico "
            "cyp2c9 polimorfismo genético dose inicial 50 mg"
        ),
        str(rcms_dir / "20_B.pdf"): (
            "indicado para diabetes mellitus tipo 2 adultos controlo glicémico "
            "cyp2c9 polimorfismo genético dose inicial 50 mg via oral"
        ),
        str(rcms_dir / "30_C.pdf"): (
            "indicado para diabetes mellitus tipo 2 adultos controlo glicémico "
            "dose inicial via oral"
        ),
        str(rcms_dir / "40_E1.pdf"): "",
        str(rcms_dir / "50_E2.pdf"): "",
    }
    monkeypatch.setattr(
        "infomed.dedup.extract_rcm_text",
        lambda pdf_path, clinical_only=True: text_map.get(pdf_path, ""),
    )

    res_single = deduplicate_substance(
        "SingleSub", db_path=db_path, rcms_dir=str(rcms_dir)
    )
    assert res_single["decision"] == "SINGLETON_SAFE"
    assert res_single["representative"] == "1_S.pdf"

    res_collapse = deduplicate_substance(
        "CollapseSub", db_path=db_path, rcms_dir=str(rcms_dir), threshold=0.35
    )
    assert res_collapse["decision"] == "SAFE_TO_COLLAPSE"
    assert res_collapse["representative"] == "20_B.pdf"
    assert len(res_collapse["discarded_rcms"]) == 2

    res_hetero = deduplicate_substance(
        "CollapseSub", db_path=db_path, rcms_dir=str(rcms_dir), threshold=0.99
    )
    assert res_hetero["decision"] == "KEEP_ALL_HETEROGENEOUS"
    assert res_hetero["representative"] is None

    res_empty = deduplicate_substance(
        "EmptySub", db_path=db_path, rcms_dir=str(rcms_dir), threshold=0.85
    )
    assert res_empty["decision"] == "KEEP_ALL_HETEROGENEOUS"
    assert res_empty["is_homogeneous"] is False
