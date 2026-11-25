"""Configuration constants, selectors, and dimension metadata for INFOMED."""

from typing import FrozenSet, Tuple

HOMEPAGE_URL = "https://extranet.infarmed.pt/INFOMED-fo/index.xhtml"
TARGET_URL = "https://extranet.infarmed.pt/INFOMED-fo/pesquisa-avancada.xhtml"
PROGRESS_FILE = "atc_progress.json"
DB_PATH = "medicamentos.db"
MEDICAMENTOS_JSON = "medicamentos.json"
MEDICAMENTOS_CSV = "medicamentos.csv"
AUDIT_REPORT_FILE = "audit_report.json"

DOWNLOAD_DIR_RCMS = "downloads/rcms"
DOWNLOAD_DIR_LEAFLETS = "downloads/leaflets"
DOWNLOAD_DIR_MMR = "downloads/mmr"

# Memory optimization browser recycling interval (categories per browser session)
BROWSER_RECYCLE_INTERVAL = 20

# Low-memory Chromium flags
CHROMIUM_LOW_MEM_ARGS = [
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--no-zygote",
    "--js-flags=--max-old-space-size=256",
    "--disable-software-rasterizer",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-extensions",
    "--renderer-process-limit=4",
    "--disable-site-isolation-trials",
]

# DOM Selectors on pesquisa-avancada.xhtml
ATC_DROPDOWN_SELECTOR = "select[id='mainForm:classif-atc_input']"
CFT_DROPDOWN_SELECTOR = "select[id='mainForm:classif-farmacoterapeutica_input']"
DISPENSA_DROPDOWN_SELECTOR = "select[id='mainForm:classif-dispensa_input']"
AIM_DROPDOWN_SELECTOR = "select[id='mainForm:estado-aim_input']"
COMERC_DROPDOWN_SELECTOR = "select[id='mainForm:estado-comercializacao_input']"
FF_DROPDOWN_SELECTOR = "select[id='mainForm:ff_input']"
VIAS_ADMIN_DROPDOWN_SELECTOR = "select[id='mainForm:vias-admin_input']"
GRUPO_PRODUTO_DROPDOWN_SELECTOR = "select[id='mainForm:grupo-produto_input']"
GENERICO_DROPDOWN_SELECTOR = "select[id='mainForm:generico_input']"
MARGEM_TERAP_DROPDOWN_SELECTOR = "select[id='mainForm:margem-terap_input']"
MONIT_ADICIONAL_DROPDOWN_SELECTOR = "select[id='mainForm:monit-adicional_input']"
EXIST_MMR_DROPDOWN_SELECTOR = "select[id='mainForm:exist-docs-mmr_input']"
SEARCH_BUTTON_SELECTOR = "button[id='mainForm:btnDoSearch']"
RESULTS_TABLE_SELECTOR = "div[id='mainForm:dt-medicamentos']"
TABLE_BODY_SELECTOR = "tbody[id='mainForm:dt-medicamentos_data']"
NEXT_PAGE_SELECTOR = "a.ui-paginator-next"
DISABLED_NEXT_PAGE_SELECTOR = "a.ui-paginator-next.ui-state-disabled"
RCM_ICON_SELECTOR = (
    "a[id*='pesqAvancadaDatableRcmIcon'], a[id*='pesqAvancadaDatableEmaIcon']"
)
FI_ICON_SELECTOR = (
    "a[id*='pesqAvancadaDatableFiIcon'], a[id*='pesqAvancadaDatableEmaFiIcon']"
)
MMR_ICON_SELECTOR = "a[id*='pesqAvancadaDatableMmrIcon']"
DRUG_NAME_INPUT_SELECTOR = "input[id='mainForm:medicamento_input']"
REG_NUMBER_INPUT_SELECTOR = "input[id='mainForm:numero-registro']"

CSV_FIELDNAMES = [
    "id_key",
    "med_id",
    "drug_name",
    "active_substance",
    "pharma_form",
    "dosage",
    "mah",
    "commercialization",
    "aim_status",
    "atc_codes",
    "atc_labels",
    "first_seen_sweep",
    "has_rcm",
    "rcm_filename",
    "rcm_downloaded",
    "rcm_verified",
    "rcm_source_sweep",
    "has_fi",
    "fi_filename",
    "fi_downloaded",
    "fi_verified",
    "fi_source_sweep",
    "has_mmr",
    "mmr_filename",
    "mmr_downloaded",
    "mmr_verified",
    "mmr_source_sweep",
]

ALLOWED_PROGRESS_TABLES: FrozenSet[str] = frozenset(
    {
        "atc_progress",
        "dispensa_progress",
        "cft_progress",
        "aim_progress",
        "comerc_progress",
        "ff_progress",
        "via_progress",
        "grupo_progress",
        "generico_progress",
        "margem_progress",
        "monit_progress",
        "mmr_doc_progress",
    }
)

# Declarative list of all 12 INFOMED search dimensions in execution order:
# (arg_attr, sweep_name, dropdown_selector, progress_table)
DIMENSIONS: Tuple[Tuple[str, str, str, str], ...] = (
    (
        "sweep_cft",
        "1. Farmacoterapêutica (CFT)",
        CFT_DROPDOWN_SELECTOR,
        "cft_progress",
    ),
    (
        "sweep_atc",
        "2. WHO ATC Traversal",
        ATC_DROPDOWN_SELECTOR,
        "atc_progress",
    ),
    (
        "sweep_dispensa",
        "3. Dispensa Classes",
        DISPENSA_DROPDOWN_SELECTOR,
        "dispensa_progress",
    ),
    (
        "sweep_ff",
        "4. Forma Farmacêutica",
        FF_DROPDOWN_SELECTOR,
        "ff_progress",
    ),
    (
        "sweep_via",
        "5. Via de Administração",
        VIAS_ADMIN_DROPDOWN_SELECTOR,
        "via_progress",
    ),
    (
        "sweep_grupo",
        "6. Grupo de Produto",
        GRUPO_PRODUTO_DROPDOWN_SELECTOR,
        "grupo_progress",
    ),
    (
        "sweep_aim",
        "7. Estado da AIM",
        AIM_DROPDOWN_SELECTOR,
        "aim_progress",
    ),
    (
        "sweep_comerc",
        "8. Comercialização",
        COMERC_DROPDOWN_SELECTOR,
        "comerc_progress",
    ),
    (
        "sweep_generico",
        "9. Genérico",
        GENERICO_DROPDOWN_SELECTOR,
        "generico_progress",
    ),
    (
        "sweep_margem",
        "10. Margem Terapêutica",
        MARGEM_TERAP_DROPDOWN_SELECTOR,
        "margem_progress",
    ),
    (
        "sweep_monit",
        "11. Monit. Adicional",
        MONIT_ADICIONAL_DROPDOWN_SELECTOR,
        "monit_progress",
    ),
    (
        "sweep_mmr",
        "12. Documentos MMR",
        EXIST_MMR_DROPDOWN_SELECTOR,
        "mmr_doc_progress",
    ),
)
