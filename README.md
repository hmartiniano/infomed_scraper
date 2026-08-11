# Infomed RCM Scraper

A robust Python browser automation tool built with Playwright and `uv` to scrape and download Resumo das Características do Medicamento (RCM) documents from the INFOMED JSF extranet portal.

## Features
- **JSF State Handling**: Operates inside a browser environment to automatically maintain `javax.faces.ViewState` and execute dynamic PrimeFaces AJAX DOM updates.
- **RCM Document Downloading**: Saves extracted RCM files directly into `downloads/rcms/`.
- **Duplicate Prevention**: Automatically skips re-downloading files that have already been saved to disk or tracked in progress.
- **Progress Persistence**: Automatically saves processed ATC codes, extracted URLs, and downloaded filenames to `atc_progress.json` to allow seamless resuming.
- **Pagination Support**: Iterates through PrimeFaces paginator controls for multi-page search results.
- **Error Recovery**: Handles network/ViewState timeouts by automatically resetting session state.

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

The scraper will save RCM documents to `downloads/rcms/`, write extracted links to `rcm_links.txt`, and track execution progress in `atc_progress.json`.

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
