# Infomed RCM Scraper

Documentation for the INFOMED extranet RCM (Resumo das Características do Medicamento) and Patient Leaflet (FI) scraper and pharmacogenomic deduplication toolkit.

## Architecture Overview

- `infomed.config`: Portal URLs, PrimeFaces/JSF DOM selectors, SQLite progress table allowlist, and declarative 12-dimension sweep definitions.
- `infomed.db`: ACID SQLite persistence (`medicamentos.db` in WAL mode), per-sweep provenance and metrics tracking, and JSON/CSV dataset exports.
- `infomed.ema`: Binary PDF/OLE2 integrity validation (`%PDF-`, `%%EOF`, OLE2), UI badge stripping, and INN-normalized direct EMA EPAR document resolution.
- `infomed.scraper`: Playwright automation with PrimeFaces AJAX queue synchronization, paginated row extraction, Stage 2 targeted retry, and Stage 3 EMA/INFOMED backfill.
- `infomed.audit`: Disk/database integrity auditing and live homepage benchmark reconciliation.
- `infomed.main`: Pipeline orchestrator and `infomed` CLI entrypoint.
- `infomed.dedup`: Sections 4 & 5 extraction, TF-IDF cosine similarity, deterministic medoid deduplication, and PGx biomarker scanning (`infomed-dedup` CLI).

## Standard Pipeline Workflow

1. Fetch live official benchmarks from `https://extranet.infarmed.pt/INFOMED-fo/index.xhtml`.
2. Execute requested classification/filter sweeps on `https://extranet.infarmed.pt/INFOMED-fo/pesquisa-avancada.xhtml` (default: CFT followed by WHO ATC), synchronizing with PrimeFaces AJAX responses and validating downloaded PDFs atomically.
3. Run Stage 2 (`retry_missing_documents`) for any published RCM, FI, or MMR files that failed during sweeps.
4. Run Stage 3 (`backfill_ema_and_missing_documents`) to resolve Centrally Authorized Products (CAPs) from INFOMED and directly from EMA using European INN slugs.
5. Export `medicamentos.json` and `medicamentos.csv`, write `audit_report.json`, and print the executive audit summary.
