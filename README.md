# Infomed RCM, Patient Leaflet & Drug Data Scraper

A robust Python browser automation tool built with Playwright and `uv` to scrape comprehensive medicine metadata, Resumo das Características do Medicamento (RCM / SmPC) documents, and Folhetos Informativos (FI / Patient Leaflets) from the INFOMED JSF extranet portal into a unified ACID SQLite database and structured dataset exports.

## Features
- **Unified ACID SQLite Persistence**: All medicine records, document flags, and ATC taxonomy progression are unified directly inside an SQLite database (`medicamentos.db`), guaranteeing complete durability against crashes and eliminating redundant text/JSON file I/O overhead.
- **Stage 2 Targeted Document Reconciliation**: Automatically queries SQLite for any published RCM or Leaflet documents that encountered transient network timeouts during the initial ATC traversal, and performs targeted direct searches with extended timeouts to maximize capture rates.
- **Automated Dataset Exports**: Automatically serializes `medicamentos.db` to `medicamentos.json` and `medicamentos.csv` upon completion and at periodic checkpoints.
- **Comprehensive Metadata Extraction**: Extracts all drug attributes including Registration / Med ID, Trade Name, Active Substance (INN/DCI), Pharmaceutical Form, Dosage / Strength, Marketing Authorization Holder (MAH), Marketed Status, Authorization Status, WHO ATC Classifications, and document flags.
- **RCM & Leaflet Document Downloading**:
  - SmPC documents (RCM) saved directly to `downloads/rcms/`.
  - Patient Information Leaflets (FI) saved to `downloads/leaflets/`.
  - Risk Minimization Materials (MMR) saved to `downloads/mmr/`.
- **Low-Memory Browser Recycling**: Periodically recycles Playwright browser contexts (every 25 ATCs) and Chromium processes (every 100 ATCs) with tight memory caps to keep system RAM under ~200 MB.
- **Multi-Point PDF Integrity Verification**: Every downloaded file is validated against `%PDF-` header magic bytes, `%%EOF` trailer markers, size requirements, and `pdfinfo` structure checks.
- **Completeness & Integrity Auditing**: Generates a detailed `audit_report.json` reporting total medicines, available documents on the portal, download success rates, and PDF integrity rates across all folders.
- **Duplicate Prevention & Caching**: Automatically skips re-downloading files that already exist on disk and pass integrity verification.
- **JSF State & Session Error Recovery**: Handles network timeouts and JSF ViewState desynchronization by automatically resetting and re-establishing clean browser sessions.

## Setup & Prerequisites

Make sure you have `uv` installed:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Install dependencies:
```bash
uv sync
uv run playwright install chromium
```

## Running the Scraper

To run the main scraper:
```bash
uv run python -m infomed.main
```

## Running Tests & Quality Checks

Run unit tests:
```bash
uv run pytest
```

Run linter and formatter checks:
```bash
uv run ruff check .
uv run ruff format .
```
