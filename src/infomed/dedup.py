"""RCM Deduplication and Pharmacogenomics (PGx) Analysis Engine for INFOMED.

This module provides text extraction, clinical section isolation (Sections 4 & 5),
pairwise TF-IDF similarity calculation, pharmacogenomic marker detection,
and conservative deduplication for Summary of Product Characteristics (RCM / SmPC)
documents belonging to the same active substance.
"""

import argparse
import json
import logging
import os
import random
import re
import sqlite3
import subprocess
from typing import Any, Dict, List, Optional

import numpy as np
from pypdf import PdfReader
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from infomed.main import DB_PATH
from infomed.main import DOWNLOAD_DIR_RCMS as RCMS_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

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
        r"\b(defici[êe]ncia\s+(completa|parcial)?\s+de\s+(dpd|dpyd|tpmt|g6pd))\b"
    ),
}


def extract_clinical_sections_text(raw_text: str) -> str:
    """Isolate Sections 4 (Clinical Particulars) and 5 (Pharmacology) from an RCM.

    Strips Section 1 (Brand Names), Section 2 (Composition), Section 3 (Form),
    Section 6 (Excipient/Storage), and Sections 7-10 (MAH address and admin).

    Args:
        raw_text: Raw text string extracted directly from PDF.

    Returns:
        Isolated text of Sections 4 and 5, or full normalized text if boundaries
        cannot be determined.

    """
    if not raw_text:
        return ""

    # Regex for start of Section 4 (Clinical Particulars)
    sec4_match = re.search(
        r"(?:(?:^|\n)\s*4\.\s*INFORMAÇÕES\s+CLÍNICAS|(?:^|\n)\s*4\.1\s*Indicações)",
        raw_text,
        re.IGNORECASE,
    )
    # Regex for start of Section 6 or 7 (End of Section 5)
    sec6_match = re.search(
        r"(?:(?:^|\n)\s*6\.\s*INFORMAÇÕES\s+FARMACÊUTICAS|"
        r"(?:^|\n)\s*6\.\s*DADOS\s+FARMACÊUTICOS|"
        r"(?:^|\n)\s*6\.1\s*Lista\s+dos\s+excipientes|"
        r"(?:^|\n)\s*7\.\s*TITULAR)",
        raw_text,
        re.IGNORECASE,
    )

    if sec4_match and sec6_match and sec6_match.start() > sec4_match.start():
        clinical_substring = raw_text[sec4_match.start() : sec6_match.start()]
        return normalize_rcm_text(clinical_substring)

    # Fallback to full normalized text if regex boundary fails
    return normalize_rcm_text(raw_text)


def extract_rcm_text(pdf_path: str, clinical_only: bool = True) -> str:
    """Extract and normalize text from an RCM PDF file.

    Args:
        pdf_path: Absolute or relative path to the PDF file.
        clinical_only: If True, extracts only Sections 4 & 5 (Clinical Particulars
            and Pharmacology). If False, extracts the full document.

    Returns:
        Normalized text content extracted from the PDF.

    """
    if not os.path.exists(pdf_path):
        logger.warning(f"PDF file not found: {pdf_path}")
        return ""

    raw_text = ""
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
        raw_text = res.stdout
    except Exception:
        # Fallback to pure-Python pypdf
        try:
            reader = PdfReader(pdf_path)
            pages_text = []
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    pages_text.append(text)
            raw_text = "\n".join(pages_text)
        except Exception as err:
            logger.error(f"Failed to extract text from {pdf_path}: {err}")
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

    # Remove standard INFARMED headers and recurring boilerplates
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

    # Normalize whitespace
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

    detected_genes = []
    for gene, pattern in PGX_GENE_PATTERNS.items():
        if re.search(pattern, text, flags=re.IGNORECASE):
            detected_genes.append(gene)

    detected_phenotypes = []
    detected_terms = []
    for term, pattern in PGX_TERM_PATTERNS.items():
        if re.search(pattern, text, flags=re.IGNORECASE):
            if "Metabolizador" in term:
                detected_phenotypes.append(term)
            else:
                detected_terms.append(term)

    # Extract context snippets around detected PGx terms (max 5 key snippets)
    all_patterns = list(PGX_GENE_PATTERNS.values()) + list(PGX_TERM_PATTERNS.values())
    combined_regex = re.compile(
        r"[^.!?\n]*?(?:" + "|".join(all_patterns) + r")[^.!?\n]*?[.!?]",
        flags=re.IGNORECASE,
    )

    snippets = []
    for match in combined_regex.finditer(text):
        snippet_text = match.group(0).strip()
        if len(snippet_text) > 25 and snippet_text not in snippets:
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

    Args:
        texts: List of normalized text documents.

    Returns:
        Square symmetric numpy ndarray of pairwise cosine similarities (0.0 to 1.0).

    """
    if not texts:
        return np.empty((0, 0))
    if len(texts) == 1:
        return np.array([[1.0]])

    valid_texts = [t if t.strip() else "empty_document" for t in texts]

    vectorizer = TfidfVectorizer(
        ngram_range=(1, 2),
        sublinear_tf=True,
        min_df=1,
        token_pattern=r"(?u)\b\w+\b",
    )
    tfidf_matrix = vectorizer.fit_transform(valid_texts)
    sim_matrix = cosine_similarity(tfidf_matrix, tfidf_matrix)
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


def deduplicate_substance(
    substance_name: str,
    db_path: str = DB_PATH,
    rcms_dir: str = RCMS_DIR,
    threshold: float = 0.85,
    seed: int = 42,
    clinical_only: bool = True,
) -> Dict[str, Any]:
    """Evaluate text similarity and apply Option-B deduplication for one substance.

    Args:
        substance_name: Name of the active substance.
        db_path: Path to SQLite database.
        rcms_dir: Path to directory containing RCM PDFs.
        threshold: Minimum pairwise similarity required to collapse (default: 0.85).
        seed: Random seed for reproducible selection (default: 42).
        clinical_only: If True, restricts similarity to Sections 4 & 5 (default: True).

    Returns:
        Dictionary containing evaluation metrics, pairwise similarity matrix,
        PGx biomarker profiles, and deduplication decision.

    """
    rcm_records = get_substance_rcms(substance_name, db_path, rcms_dir)
    n_docs = len(rcm_records)

    if n_docs == 0:
        is_registered = False
        sample_mah = None
        if os.path.exists(db_path):
            with sqlite3.connect(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT drug_name, mah FROM medicamentos "
                    "WHERE LOWER(active_substance) = LOWER(?) LIMIT 1",
                    (substance_name,),
                )
                row = cur.fetchone()
                if row:
                    is_registered = True
                    sample_mah = row[1]

        reason = (
            "EMA Centrally Authorised (SmPC on EMA EPAR register)"
            if (is_registered and sample_mah and "Novartis" in sample_mah)
            else "No local RCM files found on disk"
        )

        return {
            "substance": substance_name,
            "doc_count": 0,
            "decision": "NO_LOCAL_DOCUMENTS",
            "reason": reason,
            "is_homogeneous": True,
            "min_similarity": 1.0,
            "mean_similarity": 1.0,
            "max_similarity": 1.0,
            "representative": None,
            "retained_rcms": [],
            "discarded_rcms": [],
            "similarity_matrix": [],
            "pgx_profile": {
                "has_pgx": False,
                "genes": [],
                "phenotypes": [],
                "terms": [],
            },
        }

    # Extract text and scan PGx for each PDF
    extracted_texts = []
    doc_pgx_profiles = []
    for r in rcm_records:
        text = extract_rcm_text(r["pdf_path"], clinical_only=clinical_only)
        extracted_texts.append(text)
        pgx = scan_pgx_markers(text)
        doc_pgx_profiles.append(pgx)
        r["pgx"] = pgx

    if n_docs == 1:
        rep = rcm_records[0]
        return {
            "substance": substance_name,
            "doc_count": 1,
            "decision": "SINGLETON_SAFE",
            "is_homogeneous": True,
            "min_similarity": 1.0,
            "mean_similarity": 1.0,
            "max_similarity": 1.0,
            "representative": rep["rcm_filename"],
            "representative_drug": rep["drug_name"],
            "retained_rcms": [rep["rcm_filename"]],
            "discarded_rcms": [],
            "similarity_matrix": [[1.0]],
            "pgx_profile": doc_pgx_profiles[0],
            "medicines": [
                {
                    "id_key": rep["id_key"],
                    "drug_name": rep["drug_name"],
                    "pharma_form": rep["pharma_form"],
                    "dosage": rep["dosage"],
                    "mah": rep["mah"],
                    "rcm_filename": rep["rcm_filename"],
                    "pgx": rep["pgx"],
                }
            ],
        }

    sim_matrix = compute_similarity_matrix(extracted_texts)

    pairwise_scores = []
    for i in range(n_docs):
        for j in range(i + 1, n_docs):
            pairwise_scores.append(float(sim_matrix[i, j]))

    min_sim = float(min(pairwise_scores)) if pairwise_scores else 1.0
    max_sim = float(max(pairwise_scores)) if pairwise_scores else 1.0
    mean_sim = float(np.mean(pairwise_scores)) if pairwise_scores else 1.0

    is_homogeneous = min_sim >= threshold

    substance_genes = sorted(list(set(g for p in doc_pgx_profiles for g in p["genes"])))
    substance_phenotypes = sorted(
        list(set(ph for p in doc_pgx_profiles for ph in p["phenotypes"]))
    )
    substance_terms = sorted(list(set(t for p in doc_pgx_profiles for t in p["terms"])))
    all_snippets = []
    for p in doc_pgx_profiles:
        for s in p["snippets"]:
            if s not in all_snippets:
                all_snippets.append(s)
            if len(all_snippets) >= 5:
                break

    rng = random.Random(seed)
    if is_homogeneous:
        chosen_idx = rng.randint(0, n_docs - 1)
        rep = rcm_records[chosen_idx]
        retained = [rep["rcm_filename"]]
        discarded = [
            r["rcm_filename"] for i, r in enumerate(rcm_records) if i != chosen_idx
        ]
        decision = "SAFE_TO_COLLAPSE"
    else:
        rep = None
        retained = [r["rcm_filename"] for r in rcm_records]
        discarded = []
        decision = "KEEP_ALL_HETEROGENEOUS"

    return {
        "substance": substance_name,
        "doc_count": n_docs,
        "mode": "clinical_sections_4_and_5" if clinical_only else "full_document",
        "decision": decision,
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
        "medicines": [
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
        ],
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

    Args:
        sample_substances: List of substances to evaluate.
        db_path: Path to database file.
        rcms_dir: Path to RCM files directory.
        threshold: Homogeneity threshold (default: 0.85).
        seed: Random seed (default: 42).
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
        f"Config: Homogeneity Threshold = {threshold:.2f} | Random Seed = {seed} | "
        f"Substances = {len(sample_substances)}"
    )
    print("-" * 120)
    print(
        f"{'Active Substance':<16} {'RCMs':<5} {'Full Doc (Min/Mean)':<22} "
        f"{'Clinical 4&5 (Min/Mean)':<25} {'Clinical Decision':<24} {'PGx Markers'}"
    )
    print("-" * 120)

    for sub in sample_substances:
        # Full text mode
        res_full = deduplicate_substance(
            substance_name=sub,
            db_path=db_path,
            rcms_dir=rcms_dir,
            threshold=threshold,
            seed=seed,
            clinical_only=False,
        )
        # Clinical sections mode
        res_clin = deduplicate_substance(
            substance_name=sub,
            db_path=db_path,
            rcms_dir=rcms_dir,
            threshold=threshold,
            seed=seed,
            clinical_only=True,
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
            pgx_str = "EMA CAP (EPAR)"
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
        help="Random seed for reproducible selection (default: 42)",
    )
    parser.add_argument(
        "--pilot",
        action="store_true",
        help="Run comparative pilot evaluation (Full Doc vs Clinical Sections)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="clinical_sections_pilot_results.json",
        help="Output JSON file for results",
    )
    args = parser.parse_args()

    if args.substance:
        res = deduplicate_substance(
            substance_name=args.substance,
            threshold=args.threshold,
            seed=args.seed,
            clinical_only=(args.mode == "clinical"),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        run_pilot_evaluation(
            threshold=args.threshold,
            seed=args.seed,
            output_json=args.output,
        )


if __name__ == "__main__":
    main()
