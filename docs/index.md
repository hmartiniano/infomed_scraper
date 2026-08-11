# Infomed RCM Scraper

Documentation for the INFOMED extranet RCM (Resumo das Características do Medicamento) scraper.

## Overview
This tool automates extraction of RCM link references from the INFOMED JSF portal using Playwright.

## Workflow
1. Load `https://extranet.infarmed.pt/INFOMED-fo/pesquisa-avancada.xhtml`.
2. Extract all available ATC categories from the PrimeFaces `mainForm:classif-atc_input` dropdown.
3. For each ATC category:
   - Select the ATC option.
   - Execute search (`mainForm:btnDoSearch`).
   - Iterate through PrimeFaces paginator pages (`mainForm:dt-medicamentos_paginator_bottom`).
   - Extract links matching RCM document patterns.
4. Save deduplicated links to `rcm_links.txt`.
