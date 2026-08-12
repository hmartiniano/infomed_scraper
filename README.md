# Infomed RCM, Patient Leaflet & Drug Data Scraper

A high-performance Python browser automation pipeline built with Playwright and `uv` to extract comprehensive medicine metadata, Resumo das Características do Medicamento (RCM / SmPC) documents, and Folhetos Informativos (FI / Patient Leaflets) from the Portuguese National Authority of Medicines and Health Products (INFARMED / INFOMED) into a unified ACID SQLite database with automated CSV/JSON dataset exports, 12-dimension search support, per-sweep document provenance tracking, runtime performance metrics, and live portal benchmark reconciliation.

---

## 12 Search Dimensions Supported

The scraper supports all 12 search taxonomies and status dimensions available on INFOMED:

1. **1. WHO ATC Traversal**: All 3,193 WHO ATC classifications across the entire INFOMED catalog.
2. **2. Classificação Quanto à Dispensa**: All 8 legal dispensing classifications (`MNSRM`, `MSRM`, `MSRM restrita`, etc.).
3. **3. Classificação Farmacoterapêutica (CFT)**: All 380 Portuguese national pharmacotherapeutic classes.
4. **4. Forma Farmacêutica (Dosage Forms)**: All 339 physical pharmaceutical forms (`Comprimido`, `Solução`, `Cápsula`, etc.).
5. **5. Via de Administração (Routes)**: All 66 administration routes (`Via oral`, `Via intravenosa`, etc.).
6. **6. Grupo de Produto (Product Groups)**: All 18 regulatory groups (`Biológico`, `Biossimilar`, `Genérico`, `Órfão`, etc.).
7. **7. Estado da AIM**: Marketing authorization states (`Autorizado`, `Caducado`, `Revogado`, `Suspenso`).
8. **8. Estado de Comercialização**: Commercialization states (`Comercializado`, `Não Comercializado`, `Temporariamente indisponível`).
9. **9. Genérico**: Generic status filter (`Sim` / `Não`).
10. **10. Margem Terapêutica Estreita**: Narrow therapeutic index filter (`Sim` / `Não`).
11. **11. Monitorização Adicional**: Black triangle pharmacovigilance tracking (`Sim` / `Não`).
12. **12. Existência de Documentos MMR**: Risk Minimization Materials filter (`Sim` / `Não`).

---

## Key Features

- **Document Sweep Provenance & Incremental Harvesting**:
  - Each sweep checks if the PDF files it encounters are already downloaded and valid on disk, downloading any missing documents.
  - Attributes the originating sweep dimension to each document (`rcm_source_sweep`, `fi_source_sweep`, `mmr_source_sweep`), revealing the marginal contribution of each classification system.
- **Runtime Performance & Yield Benchmarking**:
  - Measures execution runtime and logs processing throughput in `sweep_metrics`.
- **Stage 2: Retry Downloads of Missing Files**:
  - Automatically targets and retries any published documents that encountered transient server timeouts during sweeps by performing direct searches with extended 20-second download timeouts.
- **Live Official Portal Benchmark Comparison**:
  - Automatically fetches live official portal statistics from [`index.xhtml`](https://extranet.infarmed.pt/INFOMED-fo/index.xhtml) (e.g. `1,692` Active Substances / DCI, `10,426` Marketed Medicines, `12,645` Marketed Presentations, and portal update date) and compares local catalog coverage against national regulatory figures.
- **Unified ACID SQLite Persistence (`medicamentos.db`)**:
  - Stores all medicine records and dimension progress directly inside SQLite with WAL (`Write-Ahead Logging`) mode, eliminating file corruption and enabling seamless crash recovery.
- **Multi-Point Binary & PDF Integrity Verification**:
  - Verifies all downloaded files against `%PDF-` header magic bytes, `%%EOF` trailer markers, file size constraints, and `pdfinfo` structure checking (including handling legacy INFARMED OLE2 Word `.doc` binaries).

---

## Scraped Data & Directory Structure

```
infomed/
├── downloads/
│   ├── rcms/                  # SmPC / RCM PDF documents (e.g. 4481_Indocid.pdf)
│   ├── leaflets/              # Patient Leaflet / FI PDF documents (e.g. 4481_Indocid_FI.pdf)
│   └── mmr/                   # Risk Minimization Material PDFs
├── src/
│   └── infomed/
│       ├── __init__.py
│       └── main.py            # Core scraper engine & CLI
├── tests/
│   ├── __init__.py
│   └── test_main.py           # Unit tests
├── audit_report.json          # Integrity audit & benchmark summary
├── medicamentos.csv           # Tabular dataset export
├── medicamentos.db            # Master ACID SQLite database
├── medicamentos.json          # Formatted JSON dataset export
├── pyproject.toml             # uv / ruff / pytest configuration
└── README.md
```

---

## Installation & Setup

Ensure [`uv`](https://astral.sh/uv) is installed on your system:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Clone the repository and install all dependencies:

```bash
git clone https://github.com/hmartiniano/infomed_scraper.git
cd infomed_scraper
uv sync
uv run playwright install chromium
```

---

## Usage Guide

### 1. Multi-Dimension Sweep Across All Dimensions (`--sweep-all`)

To sweep across all 12 dimensions:

```bash
uv run python -m infomed.main --sweep-all
```

### 2. Individual Dimension Sweeps

```bash
# Sweep Forma Farmacêutica (339 categories)
uv run python -m infomed.main --ff

# Sweep Via de Administração (66 categories)
uv run python -m infomed.main --via

# Sweep Grupo de Produto (18 categories)
uv run python -m infomed.main --grupo

# Sweep Classificação Farmacoterapêutica (380 categories)
uv run python -m infomed.main --cft

# Sweep Classificação Quanto à Dispensa (8 categories)
uv run python -m infomed.main --dispensa

# Sweep Estado da AIM (4 categories)
uv run python -m infomed.main --aim

# Sweep Estado de Comercialização (3 categories)
uv run python -m infomed.main --comerc

# Sweep Genérico (Sim / Não)
uv run python -m infomed.main --generico

# Sweep Margem Terapêutica Estreita
uv run python -m infomed.main --margem

# Sweep Monitorização Adicional
uv run python -m infomed.main --monit

# Sweep Existência de Documentos MMR
uv run python -m infomed.main --mmr-docs
```

### 3. Retry Downloads of Missing Files Only (`--stage2`)

```bash
uv run python -m infomed.main --stage2
```

### 4. Summary of CLI Flags

| Flag | Description | Default |
| :--- | :--- | :--- |
| `--sweep-all` | Run sweeps across all 12 dimensions | `False` |
| `--cft` | Run sweep across Classificação Farmacoterapêutica (380) | `False` |
| `--ff` / `--forma-farmaceutica` | Run sweep across Forma Farmacêutica (339) | `False` |
| `--via` / `--via-admin` | Run sweep across Via de Administração (66) | `False` |
| `--grupo` / `--grupo-produto` | Run sweep across Grupo de Produto (18) | `False` |
| `--dispensa` | Run sweep across Classificação Quanto à Dispensa (8) | `False` |
| `--aim` | Run sweep across Estado da AIM filters (4) | `False` |
| `--comerc` | Run sweep across Estado de Comercialização filters (3) | `False` |
| `--generico` | Run sweep across Genérico filters (2) | `False` |
| `--margem` | Run sweep across Margem Terapêutica filters (2) | `False` |
| `--monit` | Run sweep across Monitorização Adicional filters (2) | `False` |
| `--mmr-docs` | Run sweep across Existência de MMR filters (2) | `False` |
| `--stage2` / `--retry-only` | Run only Stage 2 (Retry Missing Files) | `False` |
| `--no-headless` | Run Chromium in visible/headed mode | `Headless` |
| `--db <path>` | Specify custom SQLite database path | `medicamentos.db` |

---

## Executive Audit & Comparison Report

```text
============================================================================================================
                                  INFOMED MASTER AUDIT & COMPARISON REPORT                                  
============================================================================================================
  PORTAL OFFICIAL BENCHMARK (https://extranet.infarmed.pt/INFOMED-fo/index.xhtml)
  Portal Last Updated Date : 12/08/2026
  Active Substances (DCI)  : 1,692
  Marketed Medicines       : 10,426
  Marketed Presentations   : 12,645
  Pipeline Total Wall Time : 48m 10s (including all downloads & retries)
------------------------------------------------------------------------------------------------------------
  PER-SWEEP DOCUMENT YIELD & WALL-TIME BENCHMARK
  Sweep Dimension          Categories   Wall Time  Drugs Seen (New)   RCMs / Net New       Leaflets / Net New
  --------------------------------------------------------------------------------------------------------
  1. WHO ATC Traversal     3,193/3,193  42m 10s    8,900 (+8,900)     7,202 (+7,202)       7,151 (+7,151)
  2. Dispensa Classes      8/8          02m 43s    153 (+151)         149 (+149)           149 (+149)
  3. Farmacoterapêutica    379/380      47m 03s    9,238 (+443)       7,628 (+415)         7,630 (+465)
  4. Forma Farmacêutica    339/339      --         ...                ...                  ...
  5. Via de Administração  66/66        --         ...                ...                  ...
  6. Grupo de Produto      18/18        --         ...                ...                  ...
  7. Estado da AIM         4/4          00m 12s    32 (+0)            10 (+0)              10 (+0)
  8. Comercialização       3/3          00m 05s    0 (+0)             0 (+0)               0 (+0)
  9. Genérico              2/2          --         ...                ...                  ...
 10. Margem Terapêutica    2/2          --         ...                ...                  ...
 11. Monit. Adicional      2/2          --         ...                ...                  ...
 12. Documentos MMR        2/2          --         ...                ...                  ...
------------------------------------------------------------------------------------------------------------
  COMBINED DATABASE CATALOG & BENCHMARK COMPARISON
  Unique Medicines in DB   : 9,494 (vs 10,426 official marketed)
  Distinct DCIs in DB      : 1,653 / 1,692 (97.7% national coverage)
  - Autorizado Status      : 9,494
  - Caducado / Revogado    : 0
  - Comercializado         : 9,494
------------------------------------------------------------------------------------------------------------
  DOCUMENT HARVESTING & RETRY RESULTS
  SmPC Documents (RCM)     : 7,766 / 7,778 (99.8%) downloaded & verified
  Patient Leaflets (FI)    : 7,765 / 7,782 (99.8%) downloaded & verified
  Missing on Portal        : 12 RCMs, 17 FIs (server null/ghost links)
------------------------------------------------------------------------------------------------------------
  PHYSICAL DISK & BINARY INTEGRITY
  Total Documents on Disk  : 15,568 PDFs (7,799 RCMs + 7,769 Leaflets)
  Corrupted Files on Disk  : 0 (0.0%)
  Overall File Integrity   : 100.0% (Validated: %PDF-, %%EOF, OLE2, pdfinfo)
============================================================================================================
```

---

## License

MIT License.
