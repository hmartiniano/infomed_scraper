# Infomed RCM, Patient Leaflet & Drug Data Scraper

A high-performance Python browser automation pipeline built with Playwright and `uv` to extract comprehensive medicine metadata, Resumo das Características do Medicamento (RCM / SmPC) documents, and Folhetos Informativos (FI / Patient Leaflets) from the Portuguese National Authority of Medicines and Health Products (INFARMED / INFOMED) into a unified ACID SQLite database with automated CSV/JSON dataset exports.

---

## Key Features

- **Dual-Stage Execution Workflow**:
  - **Stage 1 (ATC Category Traversal)**: Traverses all 3,193 WHO ATC classifications across the entire INFOMED catalog, extracting full presentation attributes, active substances, dosages, MAHs, and available document links.
  - **Stage 2 (Retry Downloads of Missing Files)**: Automatically targets and retries any published documents that encountered transient server timeouts during Stage 1 by performing direct searches with extended 20-second download timeouts.
- **Unified ACID SQLite Persistence (`medicamentos.db`)**:
  - Stores all medicine records and ATC progression directly inside SQLite with WAL (`Write-Ahead Logging`) mode, eliminating file corruption and enabling seamless crash recovery.
  - Automatically deduplicates and merges multi-classification ATC taxonomies per medicine presentation.
- **Automated Multi-Format Dataset Exports**:
  - Automatically exports the database to `medicamentos.json` (7.3 MB) and `medicamentos.csv` (2.7 MB) upon completion and during periodic checkpoints.
- **Low-Memory Browser Lifecycle Management**:
  - Automatically recycles Playwright browser contexts every 25 ATCs and Chromium browser processes every 100 ATCs with tight memory caps (`--max-old-space-size=256`, `--disable-gpu`) to maintain low memory usage across long-running executions.
- **Multi-Point Binary & PDF Integrity Verification**:
  - Verifies all downloaded files against `%PDF-` header magic bytes, `%%EOF` trailer markers, file size constraints, and `pdfinfo` structure checking (including handling legacy INFARMED OLE2 Word `.doc` binaries).
- **Executive Audit Summary Table**:
  - Outputs a structured executive audit report table directly to the console at the end of runs and writes machine-readable statistics to `audit_report.json`.

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
├── audit_report.json          # Integrity audit summary
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

### 1. Full Pipeline Execution (Stage 1 + Stage 2)

To run the complete scraper from start to finish:

```bash
uv run python -m infomed.main
```

- If running for the first time, it executes **Stage 1 (ATC Category Traversal)** followed immediately by **Stage 2 (Retry Downloads of Missing Files)**.
- If re-run after Stage 1 has completed, it automatically detects that all ATCs are done and proceeds straight to **Stage 2**.

### 2. Retry Downloads of Missing Files Only (`--stage2`)

To skip the 3,193 ATC category check and only retry downloading any missing published documents:

```bash
uv run python -m infomed.main --stage2
```

### 3. Additional CLI Flags

| Flag | Description | Default |
| :--- | :--- | :--- |
| `--stage2` / `--retry-only` | Run only Stage 2 (Retry Downloads of Missing Files) | `False` |
| `--no-headless` | Run Chromium in visible/headed mode | `Headless` |
| `--db <path>` | Specify custom SQLite database path | `medicamentos.db` |

---

## Executive Audit Table

Upon pipeline completion, the scraper generates a structured console report:

```text
========================================================================================
                              INFOMED SCRAPER AUDIT REPORT                              
========================================================================================
Category               Metric Name                  Count / Status       Notes
----------------------------------------------------------------------------------------
Catalog Scope          ATC Categories Traversed     3,193 / 3,193 (100%) All valid categories
                       Unique Medicines in DB       8,900                Distinct formulations
----------------------------------------------------------------------------------------
SmPC Documents (RCM)   Published on Portal          7,265                Published by INFARMED
                       Downloaded & Verified        7,202 (99.1%)        Saved in downloads/rcms
                       Missing on Portal            63                   Server null/ghost links
----------------------------------------------------------------------------------------
Patient Leaflets (FI)  Published on Portal          7,268                Published by INFARMED
                       Downloaded & Verified        7,151 (98.4%)        Saved in downloads/leaflets
                       Missing on Portal            117                  Server null/ghost links
----------------------------------------------------------------------------------------
Files on Disk          Total Documents on Disk      14,755               RCMs + Leaflets
                       Corrupted Files              0 (0.0%)             100% intact
                       File Integrity Rate          100.0%               Header, trailer & pdfinfo
========================================================================================
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
