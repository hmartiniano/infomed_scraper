"""PDF validation, filename sanitization, and direct EMA EPAR resolution."""

import logging
import os
import re
import unicodedata
import urllib.request
from typing import Callable, List, Optional, Set, Tuple

logger = logging.getLogger("infomed")

_LEGAL_MAH_TOKENS: Set[str] = {
    "nv",
    "bv",
    "sa",
    "ab",
    "ag",
    "sl",
    "slu",
    "as",
    "kg",
    "gmbh",
    "ltd",
    "inc",
    "plc",
    "sas",
    "sarl",
    "lda",
    "sp",
    "spa",
    "dd",
    "ks",
    "eeig",
    "dr",
    "st",
}

_DEVICE_SUFFIXES_RE = re.compile(
    r"-(?:penfill|pumpcart|flexpen|kwikpen|onepress|velotab|junior|adulto|"
    r"lipossomal)$"
)

_INN_SUFFIX_RULES: Tuple[Tuple[str, str | Callable[[re.Match[str]], str]], ...] = (
    (r"(?:stato|state)$", "stat"),
    (r"(?:balina|tedina|bulina)$", lambda m: m.group(0)[:-1]),
    (r"ina$", "ine"),
    (r"ona$", "one"),
    (r"ida$", "ide"),
    (r"azol$", "azole"),
    (r"ano$", "an"),
    (r"ero$", "er"),
    (r"tido$", "tide"),
    (r"xedo$", "xed"),
)


def _fold_ascii(text: str) -> str:
    """Fold Unicode diacritics into plain ASCII characters."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def strip_infomed_badges(name: str) -> str:
    """Strip concatenated INFOMED UI badges (MG, MB, MP, DP) from a drug name.

    Matches only uppercase badge tokens at the end of the string so legitimate
    lowercase dosage units (e.g. ``50 mg``) are left intact.

    Args:
        name: Raw drug name string extracted from the INFOMED table.

    Returns:
        Drug name with trailing UI badges removed.

    """
    return re.sub(r"\s*(?:MG|MB|MP|DP)$", "", name.strip()).strip()


def validate_pdf(filepath: str) -> bool:
    """Validate PDF/OLE2 file integrity via header, trailer, and size checks.

    Args:
        filepath: Path to the PDF file on disk.

    Returns:
        True if the PDF is non-empty and structurally intact, False otherwise.

    """
    if not os.path.exists(filepath):
        return False

    file_size = os.path.getsize(filepath)
    if file_size < 100:
        logger.warning(f"File '{filepath}' is too small ({file_size} bytes).")
        return False

    try:
        with open(filepath, "rb") as f:
            header = f.read(1024)
            is_pdf = b"%PDF-" in header
            is_doc = header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")

            if not is_pdf and not is_doc:
                logger.warning(
                    f"File '{filepath}' is missing '%PDF-' or OLE doc header."
                )
                return False

            if is_pdf:
                f.seek(max(0, file_size - 1024))
                trailer = f.read()
                if b"%%EOF" not in trailer:
                    logger.warning(f"PDF file '{filepath}' is missing '%%EOF' trailer.")
                    return False
                return True
            return True
    except Exception as err:
        logger.warning(f"Failed to read PDF file '{filepath}': {err}")
        return False


def sanitize_filename(name: str) -> str:
    """Sanitize string to create a safe cross-platform filename.

    Args:
        name: Raw name string.

    Returns:
        Filesystem-safe filename stem.

    """
    clean = re.sub(r"[^\w\.-]", "_", name.strip())
    clean = re.sub(r"_+", "_", clean).strip("_")
    return clean or "document"


def translate_portuguese_as_to_inn(as_name: str) -> str:
    """Translate Portuguese active substance names to European INN standard.

    Folds Portuguese diacritics, translates each component of ``+`` combinations
    independently, and preserves multi-word boundaries with hyphens.

    Args:
        as_name: Portuguese active substance (DCI) name.

    Returns:
        Normalized European INN slug string.

    """
    folded = _fold_ascii(as_name).lower().strip()
    if not folded:
        return ""

    raw_components = re.split(r"\s*\+\s*", folded)
    translated_parts: List[str] = []

    for comp in raw_components:
        slug_comp = re.sub(r"[^a-z0-9]+", "-", comp).strip("-")
        if not slug_comp:
            continue
        for pattern, replacement in _INN_SUFFIX_RULES:
            new_comp, count = re.subn(pattern, replacement, slug_comp)
            if count > 0:
                slug_comp = new_comp
                break
        translated_parts.append(slug_comp)

    return "-".join(translated_parts)


def generate_ema_candidate_slugs(
    drug_name: str,
    active_substance: Optional[str] = None,
    mah: Optional[str] = None,
) -> List[str]:
    """Generate specificity-ordered candidate URL slugs for EMA EPAR resolution.

    Prioritizes exact brand and MAH-qualified INN slugs before generic/device-
    stripped fallbacks so brand-specific authorizations are not shadowed by bare
    substance EPARs.

    Args:
        drug_name: Drug name as listed on INFOMED.
        active_substance: Optional Portuguese active substance (DCI).
        mah: Optional Marketing Authorization Holder name.

    Returns:
        Ordered list of unique candidate URL slugs from most specific to fallback.

    """
    ordered: List[str] = []
    seen: Set[str] = set()

    def _add(slug: str) -> None:
        cleaned = re.sub(r"-+", "-", slug).strip("-")
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            ordered.append(cleaned)

    no_parens = re.sub(r"\(.*?\)", "", drug_name or "").strip()
    no_badges = strip_infomed_badges(no_parens)
    clean_brand = re.sub(r"[^a-z0-9]+", "-", _fold_ascii(no_badges).lower()).strip("-")

    mah_token = ""
    if mah:
        mah_tokens = [
            tok for tok in re.split(r"[^a-z0-9]+", _fold_ascii(mah).lower()) if tok
        ]
        for tok in mah_tokens:
            if len(tok) >= 2 and tok not in _LEGAL_MAH_TOKENS:
                mah_token = tok
                break

    inn = translate_portuguese_as_to_inn(active_substance) if active_substance else ""
    raw_as_slug = (
        re.sub(r"[^a-z0-9]+", "-", _fold_ascii(active_substance).lower()).strip("-")
        if active_substance
        else ""
    )

    alt_inn = ""
    if inn.endswith("ine"):
        alt_inn = f"{inn[:-1]}"
    elif inn.endswith("in"):
        alt_inn = f"{inn}e"

    # 1. Exact brand slug (and English 'adult' localization for 'adulto')
    if clean_brand:
        _add(clean_brand)
        if clean_brand.endswith("-adulto"):
            _add(f"{clean_brand[:-1]}")

    # 2. INN + meaningful MAH token (generic centralized EPARs)
    if inn and mah_token:
        _add(f"{inn}-{mah_token}")
        if alt_inn:
            _add(f"{alt_inn}-{mah_token}")

    # 3. Exact brand + meaningful MAH token
    if clean_brand and mah_token and mah_token not in clean_brand.split("-"):
        _add(f"{clean_brand}-{mah_token}")

    # 4. Device/presentation-stripped parent brand (e.g. NovoRapid Penfill -> novorapid)
    if clean_brand:
        parent_brand = clean_brand
        while True:
            stripped = _DEVICE_SUFFIXES_RE.sub("", parent_brand).strip("-")
            if stripped == parent_brand:
                break
            parent_brand = stripped
        if parent_brand and parent_brand not in (clean_brand, raw_as_slug, inn):
            _add(parent_brand)

    # 5. INN + distinct brand suffix (only when brand doesn't already contain AS)
    if (
        inn
        and clean_brand
        and clean_brand != inn
        and not clean_brand.startswith(inn)
        and (not raw_as_slug or not clean_brand.startswith(raw_as_slug))
    ):
        _add(f"{inn}-{clean_brand}")

    # 6. Last-resort bare INN and -in/-ine alternate
    if inn:
        _add(inn)
    if alt_inn:
        _add(alt_inn)

    return ordered


def fetch_ema_epar_document(
    drug_name: str,
    active_substance: Optional[str] = None,
    mah: Optional[str] = None,
    target_filepath: str = "",
    doc_type: str = "RCM",
    timeout_secs: int = 10,
) -> bool:
    """Fetch Centrally Authorized Product EPAR PDF directly from EMA.

    Downloads to a temporary file and atomically promotes to ``target_filepath``
    only after structural validation passes so invalid/HTML files never remain
    on disk.

    Args:
        drug_name: Commercial drug name.
        active_substance: Optional active substance (DCI).
        mah: Optional Marketing Authorization Holder.
        target_filepath: Destination path for the validated PDF.
        doc_type: Document type label for logging (e.g. ``'RCM'``, ``'Leaflet'``).
        timeout_secs: Network timeout per candidate URL in seconds.

    Returns:
        True if a valid PDF exists or was downloaded, False otherwise.

    """
    if not target_filepath:
        return False
    if os.path.exists(target_filepath) and validate_pdf(target_filepath):
        return True

    parent_dir = os.path.dirname(target_filepath)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)
    tmp_path = f"{target_filepath}.tmp"

    slugs = generate_ema_candidate_slugs(drug_name, active_substance, mah)
    for slug in slugs:
        for lang in ("pt", "en"):
            url = (
                f"https://www.ema.europa.eu/{lang}/documents/product-information/"
                f"{slug}-epar-product-information_{lang}.pdf"
            )
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=timeout_secs) as resp:
                    if resp.status == 200:
                        content = resp.read()
                        with open(tmp_path, "wb") as f:
                            f.write(content)
                        if validate_pdf(tmp_path):
                            os.replace(tmp_path, target_filepath)
                            logger.info(
                                f"Successfully resolved {doc_type} via EMA EPAR: "
                                f"'{slug}' -> '{target_filepath}'"
                            )
                            return True
            except Exception:
                continue
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

    return False
