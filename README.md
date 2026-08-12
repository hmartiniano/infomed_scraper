# Infomed RCM, Patient Leaflet & Drug Data Scraper

A high-performance Python browser automation pipeline built with Playwright and `uv` to extract comprehensive medicine metadata, Resumo das Características do Medicamento (RCM / SmPC) documents, and Folhetos Informativos (FI / Patient Leaflets) from the Portuguese National Authority of Medicines and Health Products (INFARMED / INFOMED) into a unified ACID SQLite database with automated CSV/JSON dataset exports, per-sweep document provenance tracking, runtime performance metrics, and live portal benchmark reconciliation.

---

## Key Features

- **Multi-Taxonomy & Status Sweeps**:
  - **1. WHO ATC Traversal**: Traverses all 3,193 WHO ATC classifications across the entire INFOMED catalog.
  - **2. Classificação Quanto à Dispensa**: Sweeps all 8 legal dispensing classifications (`MNSRM`, `MSRM`, `MSRM restrita`, etc.).
  - **3. Classificação Farmacoterapêutica (CFT)**: Sweeps all 380 Portuguese national pharmacotherapeutic classes.
  - **4. Estado da AIM**: Sweeps marketing authorization states (`Autorizado`, `Caducado`, `Revogado`, `Suspenso`).
  - **5. Estado de Comercialização**: Sweeps commercialization states (`Comercializado`, `Não Comercializado`, `Temporariamente indisponível`).
- **Document Sweep Provenance & Incremental Harvesting**:
  - Each sweep checks if the PDF files it encounters are already downloaded and valid on disk, downloading any missing documents.
  - Attributes the originating sweep dimension to each document (`rcm_source_sweep`, `fi_source_sweep`, `mmr_source_sweep`), revealing the marginal contribution of each classification system to identify the most optimal sweep order.
- **Runtime Performance & Yield Benchmarking**:
  - Measures execution runtime and logs processing throughput (`drugs/sec`, net-new medicines, net-new RCMs, net-new Leaflets) in `sweep_metrics`.
- **Stage 2: Retry Downloads of Missing Files**:
  - Automatically targets and retries any published documents that encountered transient server timeouts during sweeps by performing direct searches with extended 20-second download timeouts.
- **Live Official Portal Benchmark Comparison**:
  - Automatically fetches live official portal statistics from [`index.xhtml`](https://extranet.infarmed.pt/INFOMED-fo/index.xhtml) (e.g. `1,692` Active Substances / DCI, `10,426` Marketed Medicines, `12,645` Marketed Presentations, and portal update date) and compares local catalog coverage against national regulatory figures.
- **Unified ACID SQLite Persistence (`medicamentos.db`)**:
  - Stores all medicine records and dimension progress directly inside SQLite with WAL (`Write-Ahead Logging`) mode, eliminating file corruption and enabling seamless crash recovery.
  - Automatically deduplicates and merges multi-classification ATC taxonomies per medicine presentation.
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

### 1. Standard Run (ATC Sweep + Retry Downloads of Missing Files)

```bash
uv run python -m infomed.main
```

### 2. Multi-Dimension Sweep Across All Dimensions (`--sweep-all`)

To sweep across all 5 dimensions (ATC, Dispensa, CFT, AIM, and Comercialização):

```bash
uv run python -m infomed.main --sweep-all
```

### 3. Individual Dimension Sweeps

```bash
# Sweep Classificação Quanto à Dispensa (8 categories)
uv run python -m infomed.main --dispensa

# Sweep Classificação Farmacoterapêutica (380 categories)
uv run python -m infomed.main --cft

# Sweep Estado da AIM (Autorizado, Caducado, Revogado, Suspenso)
uv run python -m infomed.main --aim

# Sweep Estado de Comercialização
uv run python -m infomed.main --comerc
```

### 4. Retry Downloads of Missing Files Only (`--stage2`)

```bash
uv run python -m infomed.main --stage2
```

### 5. Summary of CLI Flags

| Flag | Description | Default |
| :--- | :--- | :--- |
| `--sweep-all` | Run sweeps across all 5 dimensions | `False` |
| `--dispensa` | Run sweep across Classificação Quanto à Dispensa | `False` |
| `--cft` | Run sweep across Classificação Farmacoterapêutica | `False` |
| `--aim` | Run sweep across Estado da AIM filters | `False` |
| `--comerc` | Run sweep across Estado de Comercialização filters | `False` |
| `--stage2` / `--retry-only` | Run only Stage 2 (Retry Missing Files) | `False` |
| `--no-headless` | Run Chromium in visible/headed mode | `Headless` |
| `--db <path>` | Specify custom SQLite database path | `medicamentos.db` |

---

## Executive Audit & Comparison Report

Upon pipeline completion, the scraper generates a structured console report:

```text
============================================================================================================
                                  INFOMED MASTER AUDIT & COMPARISON REPORT                                  
============================================================================================================
  PORTAL OFFICIAL BENCHMARK (https://extranet.infarmed.pt/INFOMED-fo/index.xhtml)
  Portal Last Updated Date : 12/08/2026
  Active Substances (DCI)  : 1,692
  Marketed Medicines       : 10,426
  Marketed Presentations   : 12,645
------------------------------------------------------------------------------------------------------------
  PER-SWEEP DOCUMENT YIELD & PERFORMANCE BENCHMARK
  Sweep Dimension          Categories   Runtime    Drugs Seen (New)   RCMs / Net New       Leaflets / Net New
  --------------------------------------------------------------------------------------------------------
  1. WHO ATC Traversal     3,167/3,193  42m 10s    8,900 (+8,900)     7,202 (+7,202)       7,151 (+7,151)
  2. Dispensa Classes      8/8          02m 15s    1,240 (+85)        1,100 (+42)          1,120 (+38)
  3. Farmacoterapêutica    380/380      18m 30s    2,450 (+12)        2,100 (+8)           2,080 (+11)
  4. Estado da AIM         4/4          01m 45s    8,950 (+3)         7,280 (+2)           7,290 (+1)
  5. Comercialização       3/3          01m 20s    8,920 (+0)         7,265 (+0)           7,268 (+0)
------------------------------------------------------------------------------------------------------------
  COMBINED DATABASE CATALOG & BENCHMARK COMPARISON
  Unique Medicines in DB   : 8,900 (vs 10,426 official marketed)
  Distinct DCIs in DB      : 1,565 / 1,692 (92.5% coverage)
  - Autorizado Status      : 8,900
  - Caducado / Revogado    : 0
  - Comercializado         : 8,900
------------------------------------------------------------------------------------------------------------
  DOCUMENT HARVESTING & RETRY RESULTS
  SmPC Documents (RCM)     : 7,202 / 7,265 (99.1%) downloaded & verified
  Patient Leaflets (FI)    : 7,151 / 7,268 (98.4%) downloaded & verified
  Missing on Portal        : 63 RCMs, 117 FIs (server null/ghost links)
------------------------------------------------------------------------------------------------------------
  PHYSICAL DISK & BINARY INTEGRITY
  Total Documents on Disk  : 14,756 PDFs (7,531 RCMs + 7,225 Leaflets)
  Corrupted Files on Disk  : 0 (0.0%)
  Overall File Integrity   : 100.0% (Validated: %PDF-, %%EOF, OLE2, pdfinfo)
============================================================================================================
```

---

## Development & Testing

Run unit tests:
```bash
uv run pytest
```

Run code formatting and lint checks:
```bash
uv run ruff check .
uv run ruff format .
```

---

## License

MIT License.
