"""RCM Deduplication and Pharmacogenomics (PGx) Analysis Engine for INFOMED.

This module provides text extraction, clinical section isolation (Sections 4 & 5),
pairwise TF-IDF similarity calculation, pharmacogenomic marker detection,
and conservative medoid-based deduplication for Summary of Product
Characteristics (RCM / SmPC) documents belonging to the same active substance.
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
from typing import Any, Dict, List, Optional

import numpy as np
from pypdf import PdfReader
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from infomed.config import DB_PATH
from infomed.config import DOWNLOAD_DIR_RCMS as RCMS_DIR

logger = logging.getLogger(__name__)

_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Pharmacogenomic (PGx) genes, enzymes, and biomarker patterns
PGX_GENE_PATTERNS = {
    "CYP2C19": r"\bcyp\s*2c19\b",
    "CYP2C9": r"\bcyp\s*2c9\b",
    "CYP2D6": r"\bcyp\s*2d6\b",
    "CYP3A4": r"\bcyp\s*3a4\b",
    "CYP3A5": r"\bcyp\s*3a5\b",
    "CYP1A2": r"\bcyp\s*1a2\b",
    "CYP2B6": r"\bcyp\s*2b6\b",
    "CYP2E1": r"\bcyp\s*2e1\b",
    "DPYD": (
        r"\b(dpyd|dpd|di-hidropirimidina\s+desidrogenase|"
        r"dihidropirimidina\s+desidrogenase)\b"
    ),
    "TPMT": (r"\b(tpmt|tiopurina\s+s-metiltransferase|tiopurina\s+metiltransferase)\b"),
    "NUDT15": r"\bnudt\s*15\b",
    "VKORC1": r"\bvkorc\s*1\b",
    "HLA-B*5701": r"\bhla\s*[-–—]?\s*b\s*\*?\s*5701\b",
    "HLA-B*1502": r"\bhla\s*[-–—]?\s*b\s*\*?\s*1502\b",
    "HLA-A*3101": r"\bhla\s*[-–—]?\s*a\s*\*?\s*3101\b",
    "SLCO1B1": r"\b(slco1b1|oatp1b1|oatp-c)\b",
    "UGT1A1": r"\bugt\s*1a1\b",
    "G6PD": r"\b(g6pd|g-6-pd|glicose-6-fosfato\s+desidrogenase)\b",
}

# PGx clinical concepts, metabolizer phenotypes, and terminology
PGX_TERM_PATTERNS = {
    "Metabolizador Lento/Fraco": (
        r"\b(metabolizador(es)?\s+(lento(s)?|fraco(s)?|pobre(s)?))\b"
    ),
    "Metabolizador Ultrarrápido": (
        r"\b(metabolizador(es)?\s+(ultrarr[áa]pido(s)?|ultra-r[áa]pido(s)?))\b"
    ),
    "Metabolizador Intermédio": (
        r"\b(metabolizador(es)?\s+(interm[ée]dio(s)?|intermedi[áa]rio(s)?))\b"
    ),
    "Metabolizador Extensivo/Normal": (
        r"\b(metabolizador(es)?\s+(extensivo(s)?|normal(ais)?|r[áa]pido(s)?))\b"
    ),
    "Polimorfismo Genético": (
        r"\b(polimorfismo(s)?(\s+gen[ée]tico(s)?)?|variante(s)?\s+al[ée]lica(s)?)\b"
    ),
    "Genótipo / Fenótipo": (
        r"\b(gen[oó]tipo(s)?|fen[oó]tipo(s)?|genotipagem|fenotipagem)\b"
    ),
    "Farmacogenómica": (
        r"\b(farmacogen[oó]mica|farmacogen[ée]tica|teste(s)?\s+gen[ée]tico(s)?)\b"
    ),
    "Deficiência Enzimática": (
        r"\bdefici[êe]ncia\s+(?:(?:completa|parcial)\s+)?de\s+"
        r"(?:dpd|dpyd|tpmt|g6pd)\b"
    ),
}

_SEC4_START_RE = re.compile(
    r"(?:^|\n)\s*(?:4\.?\s*INFORMA[ÇC][ÕO]ES\s+CL[ÍI]NICAS|"
    r"4\.1\.?\s*INDICA[ÇC][ÕO]ES)",
    re.IGNORECASE,
)

_SEC6_START_RE = re.compile(
    r"(?:^|\n)\s*(?:6\.?\s*INFORMA[ÇC][ÕO]ES\s+FARMAC[ÊE]UTICAS|"
    r"6\.?\s*DADOS\s+FARMAC[ÊE]UTICOS|"
    r"6\.1\.?\s*LISTA\s+DOS\s+EXCIPIENTES|"
    r"7\.?\s*TITULAR)",
    re.IGNORECASE,
)


def _is_toc_block(block: str) -> bool:
    """Detect whether a matched Section 4..6 slice is merely a Table of Contents."""
    if re.search(r"\.{3,}", block):
        return True
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    if lines and all(re.search(r"\s\d{1,4}\s*$", ln) for ln in lines):
        return True
    return False


def extract_clinical_sections_text(raw_text: str) -> str:
    """Isolate Sections 4 (Clinical Particulars) and 5 (Pharmacology) from an RCM.

    Handles unaccented or accented headers, optional trailing dots on numbered
    headings, skips Table of Contents matches in EMA EPAR documents, and merges
    multiple Annex I clinical blocks when present. Falls back to full
    normalized text if no valid section boundaries are found.

    Args:
        raw_text: Raw text string extracted directly from PDF.

    Returns:
        Isolated and normalized text of Sections 4 and 5.

    """
    if not raw_text:
        return ""

    start_matches = list(_SEC4_START_RE.finditer(raw_text))
    end_matches = list(_SEC6_START_RE.finditer(raw_text))
    if not start_matches or not end_matches:
        return normalize_rcm_text(raw_text)

    blocks: List[str] = []
    last_end = 0
    for s_match in start_matches:
        if s_match.start() < last_end:
            continue
        end_match = next(
            (e for e in end_matches if e.start() > s_match.end()),
            None,
        )
        if end_match is None:
            continue
        block = raw_text[s_match.start() : end_match.start()]
        last_end = end_match.end()
        if _is_toc_block(block):
            continue
        blocks.append(block)

    if blocks:
        return normalize_rcm_text("\n".join(blocks))

    return normalize_rcm_text(raw_text)


def extract_raw_rcm_text(pdf_path: str) -> str:
    """Extract raw unnormalized text from an RCM PDF file.

    Detects legacy OLE2 ``.doc`` binaries early and returns an empty string
    without invoking PDF parsers.

    Args:
        pdf_path: Absolute or relative path to the PDF file.

    Returns:
        Raw extracted text string, or ``""`` on failure or OLE2 binary.

    """
    if not os.path.exists(pdf_path):
        logger.warning(f"PDF file not found: {pdf_path}")
        return ""

    try:
        with open(pdf_path, "rb") as f:
            magic = f.read(len(_OLE2_MAGIC))
        if magic == _OLE2_MAGIC:
            logger.warning(
                f"Legacy OLE2 .doc binary unsupported for text extraction: {pdf_path}"
            )
            return ""
    except OSError as err:
        logger.warning(f"Failed to read file header for '{pdf_path}': {err}")
        return ""

    # Try pdftotext first (fast C-based extraction)
    try:
        res = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
            check=True,
        )
        return res.stdout
    except Exception:
        # Fallback to pure-Python pypdf
        try:
            reader = PdfReader(pdf_path)
            pages_text = []
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    pages_text.append(text)
            return "\n".join(pages_text)
        except Exception as err:
            logger.error(f"Failed to extract text from {pdf_path}: {err}")
            return ""


def extract_rcm_text(pdf_path: str, clinical_only: bool = True) -> str:
    """Extract and normalize text from an RCM PDF file.

    Args:
        pdf_path: Absolute or relative path to the PDF file.
        clinical_only: If True, extracts only Sections 4 & 5 (Clinical Particulars
            and Pharmacology). If False, extracts the full document.

    Returns:
        Normalized text content extracted from the PDF.

    """
    raw_text = extract_raw_rcm_text(pdf_path)
    if not raw_text:
        return ""
    if clinical_only:
        return extract_clinical_sections_text(raw_text)
    return normalize_rcm_text(raw_text)


def normalize_rcm_text(text: str) -> str:
    """Clean and normalize regulatory RCM text.

    Strips repeated regulatory headers, page numbers, date stamps, and
    excessive whitespace to focus similarity and PGx scanning on clinical content.

    Args:
        text: Raw text string extracted from PDF.

    Returns:
        Cleaned and normalized text string.

    """
    if not text:
        return ""

    text = re.sub(
        r"INFARMED\s*[-–—]?\s*Autoridade\s+Nacional\s+do\s+Medicamento[^\n]*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"RESUMO\s+DAS\s+CARACTER[ÍI]STICAS\s+DO\s+MEDICAMENTO",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"P[áa]gina\s+\d+\s+de\s+\d+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d{1,2}/\d{1,2}/\d{4}\b", "", text)

    text = re.sub(r"\s+", " ", text).strip()
    return text.lower()


def scan_pgx_markers(text: str) -> Dict[str, Any]:
    """Scan text for pharmacogenomic biomarkers, enzymes, alleles, and phenotypes.

    Args:
        text: Normalized text from an RCM document.

    Returns:
        Dictionary containing detected genes, phenotypes, terms, and context snippets.

    """
    if not text:
        return {
            "has_pgx": False,
            "genes": [],
            "phenotypes": [],
            "terms": [],
            "snippets": [],
        }

    detected_genes = [
        gene
        for gene, pattern in PGX_GENE_PATTERNS.items()
        if re.search(pattern, text, flags=re.IGNORECASE)
    ]

    detected_phenotypes = []
    detected_terms = []
    for term, pattern in PGX_TERM_PATTERNS.items():
        if re.search(pattern, text, flags=re.IGNORECASE):
            if "Metabolizador" in term:
                detected_phenotypes.append(term)
            else:
                detected_terms.append(term)

    # Split into sentences without breaking on decimal numbers (e.g. 0.5 mg, 4.1)
    all_patterns = list(PGX_GENE_PATTERNS.values()) + list(PGX_TERM_PATTERNS.values())
    combined_regex = re.compile(
        r"(?:" + "|".join(all_patterns) + r")", flags=re.IGNORECASE
    )
    sentences = re.split(r"(?<!\d)[.!?]+(?!\d)\s*", text)

    snippets: List[str] = []
    for sentence in sentences:
        snippet_text = sentence.strip()
        if (
            len(snippet_text) > 25
            and combined_regex.search(snippet_text)
            and snippet_text not in snippets
        ):
            snippets.append(snippet_text)
        if len(snippets) >= 5:
            break

    has_pgx = bool(detected_genes or detected_phenotypes or detected_terms)

    return {
        "has_pgx": has_pgx,
        "genes": detected_genes,
        "phenotypes": detected_phenotypes,
        "terms": detected_terms,
        "snippets": snippets,
    }


def compute_similarity_matrix(texts: List[str]) -> np.ndarray:
    """Compute pairwise TF-IDF cosine similarity matrix for a list of texts.

    Empty or whitespace-only texts receive self-similarity ``1.0`` and ``0.0``
    similarity to all other documents (including other empty documents), so
    unreadable/scanned files are never falsely grouped as identical.

    Args:
        texts: List of normalized text documents.

    Returns:
        Square symmetric numpy ndarray of pairwise cosine similarities (0.0 to 1.0).

    """
    n = len(texts)
    if n == 0:
        return np.empty((0, 0))
    if n == 1:
        return np.array([[1.0]])

    sim_matrix = np.eye(n, dtype=float)
    non_empty_indices = [i for i, t in enumerate(texts) if t and t.strip()]

    if len(non_empty_indices) >= 2:
        non_empty_texts = [texts[i] for i in non_empty_indices]
        vectorizer = TfidfVectorizer(
            ngram_range=(1, 2),
            sublinear_tf=True,
            min_df=1,
            token_pattern=r"(?u)\b\w+\b",
        )
        tfidf_matrix = vectorizer.fit_transform(non_empty_texts)
        sub_sim = cosine_similarity(tfidf_matrix, tfidf_matrix)
        sim_matrix[np.ix_(non_empty_indices, non_empty_indices)] = sub_sim

    return sim_matrix


def get_substance_rcms(
    substance_name: str,
    db_path: str = DB_PATH,
    rcms_dir: str = RCMS_DIR,
) -> List[Dict[str, Any]]:
    """Retrieve all medicines with downloaded RCMs for an active substance.

    Args:
        substance_name: The target active substance (DCI).
        db_path: Path to the SQLite database.
        rcms_dir: Directory where RCM PDFs are stored.

    Returns:
        List of medicine record dictionaries with verified local PDF paths.

    """
    if not os.path.exists(db_path):
        return []

    records = []
    with sqlite3.connect(db_path, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(
            """
            SELECT id_key, med_id, drug_name, active_substance, pharma_form,
                   dosage, mah, rcm_filename
            FROM medicamentos
            WHERE LOWER(active_substance) = LOWER(?)
              AND rcm_downloaded = 1
              AND rcm_filename IS NOT NULL
            ORDER BY id_key;
            """,
            (substance_name,),
        )
        for row in c.fetchall():
            d = dict(row)
            pdf_path = os.path.join(rcms_dir, d["rcm_filename"])
            if os.path.exists(pdf_path):
                d["pdf_path"] = pdf_path
                records.append(d)

    return records


def _describe_missing_rcms_reason(substance_name: str, db_path: str) -> str:
    """Determine accurate diagnostic reason when no local RCM PDFs exist."""
    if not os.path.exists(db_path):
        return "Database not found"

    with sqlite3.connect(db_path, timeout=30.0) as conn:
        rows = conn.execute(
            "SELECT COALESCE(has_rcm, 0), COALESCE(rcm_downloaded, 0) "
            "FROM medicamentos WHERE LOWER(active_substance) = LOWER(?)",
            (substance_name,),
        ).fetchall()

    if not rows:
        return "Substance not found in database"
    if any(r[0] == 1 and r[1] == 0 for r in rows):
        return "SmPC published on portal but not downloaded locally"
    if all(r[0] == 0 for r in rows):
        return "No SmPC published on INFOMED portal"
    return "No local RCM files found on disk"


def deduplicate_substance(
    substance_name: str,
    db_path: str = DB_PATH,
    rcms_dir: str = RCMS_DIR,
    threshold: float = 0.85,
    seed: int = 42,
    clinical_only: bool = True,
    raw_texts: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Evaluate text similarity and apply deterministic medoid deduplication.

    Args:
        substance_name: Name of the active substance.
        db_path: Path to SQLite database.
        rcms_dir: Path to directory containing RCM PDFs.
        threshold: Minimum pairwise similarity required to collapse (default: 0.85).
        seed: Retained for API compatibility (representative selection is
            deterministic via similarity medoid).
        clinical_only: If True, restricts similarity to Sections 4 & 5 (default: True).
        raw_texts: Optional pre-extracted raw document texts aligned with
            ``get_substance_rcms`` ordering to avoid duplicate PDF parsing.

    Returns:
        Dictionary containing evaluation metrics, pairwise similarity matrix,
        PGx biomarker profiles, and deduplication decision with unified schema.

    """
    _ = seed  # Retained for backwards-compatible signature
    mode_label = "clinical_sections_4_and_5" if clinical_only else "full_document"
    rcm_records = get_substance_rcms(substance_name, db_path, rcms_dir)
    n_docs = len(rcm_records)

    if n_docs == 0:
        reason = _describe_missing_rcms_reason(substance_name, db_path)
        return {
            "substance": substance_name,
            "doc_count": 0,
            "mode": mode_label,
            "decision": "NO_LOCAL_DOCUMENTS",
            "reason": reason,
            "is_homogeneous": True,
            "min_similarity": 1.0,
            "mean_similarity": 1.0,
            "max_similarity": 1.0,
            "threshold": threshold,
            "representative": None,
            "representative_drug": None,
            "retained_rcms": [],
            "discarded_rcms": [],
            "similarity_matrix": [],
            "pgx_profile": {
                "has_pgx": False,
                "genes": [],
                "phenotypes": [],
                "terms": [],
                "snippets": [],
            },
            "medicines": [],
        }

    # Extract text (or normalize supplied raw text) and scan PGx for each document
    extracted_texts: List[str] = []
    doc_pgx_profiles: List[Dict[str, Any]] = []
    for idx, r in enumerate(rcm_records):
        if raw_texts is not None and idx < len(raw_texts):
            raw = raw_texts[idx]
            text = (
                extract_clinical_sections_text(raw)
                if clinical_only
                else normalize_rcm_text(raw)
            )
        else:
            text = extract_rcm_text(r["pdf_path"], clinical_only=clinical_only)
        extracted_texts.append(text)
        pgx = scan_pgx_markers(text)
        doc_pgx_profiles.append(pgx)
        r["pgx"] = pgx

    med_entries = [
        {
            "id_key": r["id_key"],
            "drug_name": r["drug_name"],
            "pharma_form": r["pharma_form"],
            "dosage": r["dosage"],
            "mah": r["mah"],
            "rcm_filename": r["rcm_filename"],
            "pgx": r["pgx"],
        }
        for r in rcm_records
    ]

    if n_docs == 1:
        rep = rcm_records[0]
        return {
            "substance": substance_name,
            "doc_count": 1,
            "mode": mode_label,
            "decision": "SINGLETON_SAFE",
            "reason": "Single local RCM document",
            "is_homogeneous": True,
            "min_similarity": 1.0,
            "mean_similarity": 1.0,
            "max_similarity": 1.0,
            "threshold": threshold,
            "representative": rep["rcm_filename"],
            "representative_drug": rep["drug_name"],
            "retained_rcms": [rep["rcm_filename"]],
            "discarded_rcms": [],
            "similarity_matrix": [[1.0]],
            "pgx_profile": doc_pgx_profiles[0],
            "medicines": med_entries,
        }

    sim_matrix = compute_similarity_matrix(extracted_texts)

    pairwise_scores = [
        float(sim_matrix[i, j]) for i in range(n_docs) for j in range(i + 1, n_docs)
    ]

    min_sim = float(min(pairwise_scores)) if pairwise_scores else 1.0
    max_sim = float(max(pairwise_scores)) if pairwise_scores else 1.0
    mean_sim = float(np.mean(pairwise_scores)) if pairwise_scores else 1.0

    has_empty_doc = any(not t.strip() for t in extracted_texts)
    is_homogeneous = (not has_empty_doc) and (min_sim >= threshold)

    substance_genes = sorted({g for p in doc_pgx_profiles for g in p["genes"]})
    substance_phenotypes = sorted(
        {ph for p in doc_pgx_profiles for ph in p["phenotypes"]}
    )
    substance_terms = sorted({t for p in doc_pgx_profiles for t in p["terms"]})

    all_snippets: List[str] = []
    for p in doc_pgx_profiles:
        for s in p["snippets"]:
            if s not in all_snippets:
                all_snippets.append(s)
            if len(all_snippets) >= 5:
                break
        if len(all_snippets) >= 5:
            break

    if is_homogeneous:
        # Select deterministic medoid (highest mean similarity; tie-break by id_key)
        chosen_idx = int(np.argmax(sim_matrix.mean(axis=1)))
        rep = rcm_records[chosen_idx]
        retained = [rep["rcm_filename"]]
        discarded = [
            r["rcm_filename"] for i, r in enumerate(rcm_records) if i != chosen_idx
        ]
        decision = "SAFE_TO_COLLAPSE"
        reason = "All pairwise similarities meet homogeneity threshold"
    else:
        rep = None
        retained = [r["rcm_filename"] for r in rcm_records]
        discarded = []
        decision = "KEEP_ALL_HETEROGENEOUS"
        reason = (
            "Unreadable or empty PDF text prevents safe collapse"
            if has_empty_doc
            else "Pairwise clinical similarity below homogeneity threshold"
        )

    return {
        "substance": substance_name,
        "doc_count": n_docs,
        "mode": mode_label,
        "decision": decision,
        "reason": reason,
        "is_homogeneous": is_homogeneous,
        "min_similarity": round(min_sim, 4),
        "mean_similarity": round(mean_sim, 4),
        "max_similarity": round(max_sim, 4),
        "threshold": threshold,
        "representative": rep["rcm_filename"] if rep else None,
        "representative_drug": rep["drug_name"] if rep else None,
        "retained_rcms": retained,
        "discarded_rcms": discarded,
        "similarity_matrix": sim_matrix.round(4).tolist(),
        "pgx_profile": {
            "has_pgx": bool(substance_genes or substance_phenotypes or substance_terms),
            "genes": substance_genes,
            "phenotypes": substance_phenotypes,
            "terms": substance_terms,
            "snippets": all_snippets,
        },
        "medicines": med_entries,
    }


def run_pilot_evaluation(
    sample_substances: Optional[List[str]] = None,
    db_path: str = DB_PATH,
    rcms_dir: str = RCMS_DIR,
    threshold: float = 0.85,
    seed: int = 42,
    output_json: Optional[str] = "clinical_sections_pilot_results.json",
) -> List[Dict[str, Any]]:
    """Run comparative evaluation comparing Full-Text vs Clinical-Sections.

    Extracts each PDF once and evaluates both full-document and Sections 4 & 5
    similarity modes on the shared extracted text.

    Args:
        sample_substances: List of substances to evaluate.
        db_path: Path to database file.
        rcms_dir: Path to RCM files directory.
        threshold: Homogeneity threshold (default: 0.85).
        seed: Retained for backwards compatibility.
        output_json: Path to save result JSON.

    Returns:
        List of evaluation result dictionaries.

    """
    if sample_substances is None:
        sample_substances = [
            "Siponimod",
            "Clopidogrel",
            "Varfarina",
            "Fluorouracilo",
            "Azatioprina",
            "Carbamazepina",
            "Codeína",
            "Abacavir",
            "Paracetamol",
            "Ibuprofeno",
            "Omeprazol",
            "Diclofenac",
            "Ciprofloxacina",
        ]

    results = []
    print("\n" + "=" * 120)
    print("   RCM SIMILARITY: FULL DOC vs CLINICAL SECTIONS (4 & 5 ONLY) COMPARISON")
    print("=" * 120)
    print(
        f"Config: Homogeneity Threshold = {threshold:.2f} | "
        f"Selection = Medoid | Substances = {len(sample_substances)}"
    )
    print("-" * 120)
    print(
        f"{'Active Substance':<16} {'RCMs':<5} {'Full Doc (Min/Mean)':<22} "
        f"{'Clinical 4&5 (Min/Mean)':<25} {'Clinical Decision':<24} {'PGx Markers'}"
    )
    print("-" * 120)

    for sub in sample_substances:
        rcm_records = get_substance_rcms(sub, db_path, rcms_dir)
        raw_texts = [extract_raw_rcm_text(r["pdf_path"]) for r in rcm_records]

        res_full = deduplicate_substance(
            substance_name=sub,
            db_path=db_path,
            rcms_dir=rcms_dir,
            threshold=threshold,
            seed=seed,
            clinical_only=False,
            raw_texts=raw_texts,
        )
        res_clin = deduplicate_substance(
            substance_name=sub,
            db_path=db_path,
            rcms_dir=rcms_dir,
            threshold=threshold,
            seed=seed,
            clinical_only=True,
            raw_texts=raw_texts,
        )

        combined = {
            "substance": sub,
            "doc_count": res_clin["doc_count"],
            "full_document": res_full,
            "clinical_sections": res_clin,
        }
        results.append(combined)

        n_docs = res_clin["doc_count"]
        if n_docs == 0:
            full_str = "N/A"
            clin_str = "N/A"
            dec = res_clin["decision"]
            pgx_str = res_clin.get("reason", "N/A")
        else:
            full_str = (
                f"{res_full['min_similarity']:.2f} / {res_full['mean_similarity']:.2f}"
            )
            clin_str = (
                f"{res_clin['min_similarity']:.2f} / {res_clin['mean_similarity']:.2f}"
            )
            dec = res_clin["decision"]
            pgx = res_clin.get("pgx_profile", {})
            pgx_str = ", ".join(pgx.get("genes", [])) or "None"

        print(
            f"{sub:<16} {n_docs:<5} {full_str:<22} {clin_str:<25} {dec:<24} {pgx_str}"
        )

    print("=" * 120 + "\n")

    if output_json:
        try:
            with open(output_json, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
            logger.info(
                f"Saved clinical sections comparison results to '{output_json}'."
            )
        except Exception as err:
            logger.error(f"Failed to save JSON results: {err}")

    return results


def main() -> None:
    """CLI entrypoint for RCM deduplication and PGx analysis."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    parser = argparse.ArgumentParser(
        description="INFOMED RCM Text Similarity & Clinical Section Extraction"
    )
    parser.add_argument(
        "--substance",
        type=str,
        help="Evaluate a single active substance (e.g. --substance Clopidogrel)",
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["clinical", "full"],
        default="clinical",
        help=(
            "Similarity mode: 'clinical' (Sections 4 & 5) or 'full' (default: clinical)"
        ),
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.85,
        help="Similarity threshold for homogeneity (default: 0.85)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Retained for API compatibility (representative is deterministic medoid).",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=DB_PATH,
        help=f"Path to SQLite database (default: '{DB_PATH}').",
    )
    parser.add_argument(
        "--rcms-dir",
        type=str,
        default=RCMS_DIR,
        help=f"Path to RCM PDFs directory (default: '{RCMS_DIR}').",
    )
    parser.add_argument(
        "--pilot",
        action="store_true",
        help="Run comparative pilot evaluation (Full Doc vs Clinical Sections).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="clinical_sections_pilot_results.json",
        help="Output JSON file for pilot evaluation results.",
    )
    args = parser.parse_args()

    if args.substance and not args.pilot:
        res = deduplicate_substance(
            substance_name=args.substance,
            db_path=args.db,
            rcms_dir=args.rcms_dir,
            threshold=args.threshold,
            seed=args.seed,
            clinical_only=(args.mode == "clinical"),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        run_pilot_evaluation(
            db_path=args.db,
            rcms_dir=args.rcms_dir,
            threshold=args.threshold,
            seed=args.seed,
            output_json=args.output,
        )


if __name__ == "__main__":
    main()
