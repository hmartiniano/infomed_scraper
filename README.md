# Infomed RCM, Patient Leaflet & Drug Data Scraper

A Python browser automation and regulatory text-analysis pipeline built with Playwright and `uv` to extract comprehensive medicine metadata, Resumo das Características do Medicamento (RCM / SmPC) documents, and Folhetos Informativos (FI / Patient Leaflets) from the Portuguese National Authority of Medicines and Health Products (INFARMED / INFOMED) into a unified ACID SQLite database with automated CSV/JSON exports, 12-dimension search support, direct EMA EPAR fallback resolution, and pharmacogenomic (PGx) clinical-section deduplication.

---

## 12 Search Dimensions Supported

The scraper supports all 12 search taxonomies and status dimensions available on INFOMED:

1. **Classificação Farmacoterapêutica (CFT)**: All 380 Portuguese national pharmacotherapeutic classes (default Stage 1 sweep).
2. **WHO ATC Traversal**: All 3,193 WHO ATC classifications across the INFOMED catalog (default Stage 2 sweep).
3. **Classificação Quanto à Dispensa**: All 8 legal dispensing classifications (`MNSRM`, `MSRM`, `MSRM restrita`, etc.).
4. **Forma Farmacêutica (Dosage Forms)**: All 339 physical pharmaceutical forms (`Comprimido`, `Solução`, `Cápsula`, etc.).
5. **Via de Administração (Routes)**: All 66 administration routes (`Via oral`, `Via intravenosa`, etc.).
6. **Grupo de Produto (Product Groups)**: All 18 regulatory groups (`Biológico`, `Biossimilar`, `Genérico`, `Órfão`, etc.).
7. **Estado da AIM**: Marketing authorization states (`Autorizado`, `Caducado`, `Revogado`, `Suspenso`).
8. **Estado de Comercialização**: Commercialization states (`Comercializado`, `Não Comercializado`, `Temporariamente indisponível`).
9. **Genérico**: Generic status filter (`Sim` / `Não`).
10. **Margem Terapêutica Estreita**: Narrow therapeutic index filter (`Sim` / `Não`).
11. **Monitorização Adicional**: Black triangle pharmacovigilance tracking (`Sim` / `Não`).
12. **Existência de Documentos MMR**: Risk Minimization Materials filter (`Sim` / `Não`).

---

## Key Features

- **Document Sweep Provenance & Incremental Harvesting**:
  - Attributes the originating sweep dimension to each document (`rcm_source_sweep`, `fi_source_sweep`, `mmr_source_sweep`) inside SQLite WAL-mode storage (`medicamentos.db`).
- **Stage 2 Targeted Retry (`--stage2`)**:
  - Re-queries missing RCM, Leaflet, and MMR documents using deterministic PrimeFaces AJAX synchronization.
- **Stage 3 Backfill & INN-Normalized Direct EMA EPAR Resolver**:
  - Searches INFOMED by badge-stripped drug name (`EMA CAP Backfill`) and resolves Centrally Authorized Product (CAP) EPAR PDFs directly from `ema.europa.eu` using specificity-ordered European INN slugs (`EMA Direct EPAR Resolver`).
- **Clinical Sections (4 & 5) Deduplication & PGx Biomarker Engine (`infomed-dedup`)**:
  - Extracts Sections 4 & 5 via `pdftotext` (or `pypdf` fallback), computes pairwise TF-IDF cosine similarity, applies deterministic medoid deduplication, and scans for pharmacogenomic genes/phenotypes (`CYP2C19`, `CYP2D6`, `DPYD`, `TPMT`, `HLA-B*5701`, etc.).
- **Multi-Point Binary Integrity Verification**:
  - Atomically downloads into `.tmp` staging files and validates `%PDF-` / `%%EOF` markers or legacy OLE2 `.doc` headers before committing files to `downloads/`.

---

## Package & Directory Structure

```
infomed/
├── downloads/
│   ├── rcms/                  # SmPC / RCM PDF documents
│   ├── leaflets/              # Patient Leaflet / FI PDF documents
│   └── mmr/                   # Risk Minimization Material PDFs
├── scripts/
│   ├── compare_runs.py        # Compare local vs remote SQLite stats via SSH
│   └── remote_status.py       # Monitor live pipeline process, DB & logs
├── src/
│   └── infomed/
│       ├── __init__.py
│       ├── config.py          # URLs, DOM selectors, constants, 12 dimensions
│       ├── db.py              # ACID SQLite persistence, metrics & CSV/JSON export
│       ├── ema.py             # PDF validation & INN-normalized EMA EPAR resolver
│       ├── scraper.py         # Playwright automation, Stage 2 retry & Stage 3 backfill
│       ├── audit.py           # Integrity auditing & executive summary reporting
│       ├── main.py            # Scraper orchestrator & CLI (`infomed`)
│       └── dedup.py           # Clinical section similarity & PGx engine (`infomed-dedup`)
├── tests/
│   ├── test_dedup.py
│   └── test_main.py
├── pyproject.toml             # uv / ruff / pytest configuration
└── README.md
```

---

## Installation & Setup

Ensure [`uv`](https://astral.sh/uv) is installed on your system. For fast C-based PDF text extraction in `infomed-dedup`, `poppler-utils` (`pdftotext`) is recommended (falls back to `pypdf` if absent):

```bash
git clone https://github.com/hmartiniano/infomed_scraper.git
cd infomed_scraper
uv sync
uv run playwright install chromium
```

---

## Usage Guide

### 1. Default Pipeline (`CFT -> ATC -> Stage 2 -> Stage 3`)

```bash
uv run infomed
# or:
uv run python -m infomed.main
```

### 2. Targeted Dimensions, Stage 2, and Backfill

```bash
# Sweep all 12 dimensions
uv run infomed --sweep-all

# Sweep only Forma Farmacêutica (339 categories)
uv run infomed --ff

# Default run skipping CFT (run ATC + Stage 2 + Stage 3)
uv run infomed --skip-cft

# Stage 2 targeted retry only (skips sweeps and Stage 3 backfill)
uv run infomed --stage2

# Stage 3 EMA & missing-document backfill only (optionally filtered)
uv run infomed --backfill-only --substance Siponimod --limit 25
```

### 3. Clinical-Section Deduplication & PGx Analysis (`infomed-dedup`)

```bash
# Evaluate a single active substance
uv run infomed-dedup --substance Clopidogrel --mode clinical

# Run the comparative pilot benchmark (Full Doc vs Sections 4 & 5)
uv run infomed-dedup --pilot --output clinical_sections_pilot_results.json
```

### 4. Summary of Scraper CLI Flags

| Flag | Description |
| :--- | :--- |
| `--sweep-all` | Run sweeps across all 12 dimensions |
| `--cft` / `--skip-cft` | Enable or skip Classificação Farmacoterapêutica (380) |
| `--atc` / `--skip-atc` | Enable or skip WHO ATC Traversal (3,193) |
| `--dispensa` | Sweep Classificação Quanto à Dispensa (8) |
| `--ff` / `--forma-farmaceutica` | Sweep Forma Farmacêutica (339) |
| `--via` / `--via-admin` | Sweep Via de Administração (66) |
| `--grupo` / `--grupo-produto` | Sweep Grupo de Produto (18) |
| `--aim` | Sweep Estado da AIM (4) |
| `--comerc` | Sweep Estado de Comercialização (3) |
| `--generico` | Sweep Genérico (2) |
| `--margem` | Sweep Margem Terapêutica Estreita (2) |
| `--monit` | Sweep Monitorização Adicional (2) |
| `--mmr-docs` | Sweep Existência de Documentos MMR (2) |
| `--stage2` / `--retry-only` | Run only Stage 2 targeted retry |
| `--backfill-only` | Run only Stage 3 backfill + Direct EMA EPAR Resolver |
| `--skip-backfill` | Skip automatic Stage 3 backfill after sweeps |
| `--substance <name>` | Filter Stage 3 backfill by active substance |
| `--limit <N>` | Limit Stage 3 search/resolver items |
| `--no-headless` | Run Chromium in visible mode |
| `--db <path>` | Specify custom SQLite database path |

---

## License

MIT License.
