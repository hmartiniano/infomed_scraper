"""Unit tests for the RCM Deduplication module."""

import numpy as np

from infomed.dedup import (
    compute_similarity_matrix,
    deduplicate_substance,
    normalize_rcm_text,
    scan_pgx_markers,
)


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


def test_compute_similarity_matrix():
    """Test TF-IDF cosine similarity matrix calculation."""
    texts = [
        "paracetamol 500 mg comprimidos para tratamento de febre e dor ligeira",
        "paracetamol 500 mg comprimidos indicado para dor ligeira e febre",
        "ciprofloxacina 250 mg gotas oftálmicas para infeções bacterianas oculares",
    ]
    sim = compute_similarity_matrix(texts)
    assert sim.shape == (3, 3)
    # Diagonal should be 1.0
    for i in range(3):
        assert np.isclose(sim[i, i], 1.0)
    # Similarity between the two paracetamol texts should be higher than with cipro
    assert sim[0, 1] > 0.40
    # Similarity between paracetamol and cipro eye drops should be near zero
    assert sim[0, 2] < 0.10
    assert sim[0, 1] > sim[0, 2]


def test_scan_pgx_markers():
    """Test detection of CYP enzymes, HLA alleles, and metabolizer phenotypes."""
    text_clopidogrel = (
        "o clopidogrel é metabolizado pela enzima cyp2c19. "
        "em doentes que são metabolizadores lentos da cyp2c19, "
        "a formação do metabolito ativo é diminuída. "
        "recomenda-se precaução em indivíduos com este polimorfismo genético."
    )
    pgx = scan_pgx_markers(text_clopidogrel)
    assert pgx["has_pgx"] is True
    assert "CYP2C19" in pgx["genes"]
    assert "Metabolizador Lento/Fraco" in pgx["phenotypes"]
    assert "Polimorfismo Genético" in pgx["terms"]
    assert len(pgx["snippets"]) >= 1

    text_hla = (
        "o rastreio do alelo hla-b*5701 deve ser realizado antes de iniciar "
        "o tratamento com abacavir devido ao risco de reação de hipersensibilidade."
    )
    pgx_hla = scan_pgx_markers(text_hla)
    assert "HLA-B*5701" in pgx_hla["genes"]


def test_deduplicate_substance_singleton(tmp_path):
    """Test deduplication behavior when only 1 RCM exists."""
    res = deduplicate_substance(
        substance_name="NonExistentDrug",
        db_path=str(tmp_path / "empty.db"),
    )
    assert res["decision"] == "NO_LOCAL_DOCUMENTS"
    assert res["is_homogeneous"] is True
