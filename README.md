# Infomed RCM, Patient Leaflet & Drug Data Scraper

A robust Python browser automation tool built with Playwright and `uv` to scrape comprehensive medicine metadata, Resumo das Características do Medicamento (RCM / SmPC) documents, and Folhetos Informativos (FI / Patient Leaflets) from the INFOMED JSF extranet portal.

## Features
- **Comprehensive Metadata Extraction**: Extracts all drug attributes including Registration / Med ID, Trade Name, Active Substance (INN/DCI), Pharmaceutical Form, Dosage / Strength, Marketing Authorization Holder (MAH), Marketed Status, Authorization Status, WHO ATC Classifications, and document flags.
- **Structured Dataset Exports**: Automatically streams and updates both `medicamentos.json` and `medicamentos.csv`.
- **RCM & Leaflet Document Downloading**:
  - SmPC documents (RCM) saved directly to `downloads/rcms/`.
  - Patient Information Leaflets (FI) saved to `downloads/leaflets/`.
  - Risk Minimization Materials (MMR) saved to `downloads/mmr/`.
- **Multi-Point PDF Integrity Verification**: Every downloaded file is validated against `%PDF-` header magic bytes, `%%EOF` trailer markers, size requirements, and `pdfinfo` structure checks.
- **Completeness & Integrity Auditing**: Generates a detailed `audit_report.json` reporting total medicines, available documents on the portal, download success rates, and PDF integrity rates across all folders.
- **Duplicate Prevention & Caching**: Automatically skips re-downloading files that already exist on disk and pass integrity verification.
- **Progress Persistence**: Saves processed ATC codes, document URLs, and downloaded filenames to `atc_progress.json` to allow seamless resuming.
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
