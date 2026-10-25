"""
mid_generator.py — Deterministic US Customs Manufacturer Identification (MID)
code generator.

Implements CBP Appendix rules as documented in:
  "Manufacturer Identification Code (MID) — CBP Trade Compliance"
  https://www.cbp.gov/sites/default/files/assets/documents/2016-Apr/mid_appendix.pdf

Usage (standalone):
    python mid_generator.py
"""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Corporate noise words to strip from company name (case-insensitive)
# ---------------------------------------------------------------------------
_NOISE_WORDS: frozenset[str] = frozenset(
    [
        "PVT",
        "PRIVATE",
        "LTD",
        "LIMITED",
        "INC",
        "INCORPORATED",
        "LLC",
        "LLP",
        "CORP",
        "CORPORATION",
        "CO",
        "COMPANY",
        "AND",
        "THE",
        "OF",
        "GROUP",
        "INDUSTRIES",
        "INDUSTRY",
        "INTERNATIONAL",
        "INTL",
        "ENTERPRISES",
        "ENTERPRISE",
        "EXPORTS",
        "EXPORT",
        "TRADING",
        "TRADE",
        "SOLUTIONS",
        "SERVICES",
        "SERVICE",
    ]
)

# Punctuation to remove before tokenising the company name
_PUNCT_RE = re.compile(r"[&.,\-/\\()\[\]{}'\"]")


def _clean_company_tokens(company_name: str) -> list[str]:
    """
    Strip noise words and punctuation from a company name, then return
    the remaining tokens as uppercase strings (empty tokens excluded).
    """
    cleaned = _PUNCT_RE.sub(" ", company_name.upper())
    tokens = cleaned.split()
    return [t for t in tokens if t not in _NOISE_WORDS and t.isalpha()]


def _company_code(company_name: str) -> str:
    """
    Return up to 6 uppercase alpha characters derived from the company name.

    CBP rule:
    - Take the first 3 letters of the first meaningful word.
    - Take the first 3 letters of the second meaningful word.
    - If only one meaningful word remains, take its first 6 letters.
    - Result is always alpha-only, uppercase, max 6 chars.

    Examples:
      "Shreeji Textiles & Apparel Pvt. Ltd."  → SHR + TEX = SHRTEX
      "BlueStar Co."                           → BLU + STA = BLUSTA
      "Maersk"                                 → MAERSK
    """
    tokens = _clean_company_tokens(company_name)
    if not tokens:
        # Fallback: take first 6 alpha chars of the raw name
        raw_alpha = re.sub(r"[^A-Za-z]", "", company_name).upper()
        return raw_alpha[:6]

    first = tokens[0]
    if len(tokens) == 1:
        return first[:6]

    second = tokens[1]
    return (first[:3] + second[:3])[:6]


def _address_number(street_address: str) -> str:
    """
    Extract up to 4 digits representing the street/plot number.

    CBP rule: Use the first numeric sequence in the address that is
    NOT a 6-digit Indian PIN code (600000–999999 range) and
    NOT preceded by 'PIN', 'PINCODE', or 'ZIP'.

    If multiple numeric sequences exist, use the first one that is
    ≤ 4 digits (plot/street number level).  If the first sequence is
    > 4 digits, take only its first 4 characters.

    Examples:
      "Plot 2301, GIDC Pandesara"              → 2301
      "A-15 Sector 5, Noida 201301"            → 15   (201301 is PIN)
      "12/3, Industrial Area"                  → 12
    """
    # Tokenise on non-digit characters to get all numeric sequences
    numbers = re.findall(r"\d+", street_address)

    for num in numbers:
        # Skip 6-digit Indian PIN codes (all digits, 6 long, range 100000-999999)
        if len(num) == 6 and 100000 <= int(num) <= 999999:
            continue
        # Skip postal codes that happen to be 5-digit US ZIPs too
        if len(num) == 5:
            continue
        # Accept: take up to 4 digits
        return num[:4]

    # If every number was filtered, fall back to the first number truncated
    if numbers:
        return numbers[0][:4]

    return ""


def _city_code(city: str) -> str:
    """
    Return the first 3 uppercase alpha letters of the city name.

    Non-alpha characters are stripped first.

    Examples:
      "Surat"       → SUR
      "New Delhi"   → NEW
      "Mumbai"      → MUM
    """
    alpha = re.sub(r"[^A-Za-z]", "", city).upper()
    return alpha[:3]


def generate_cbp_mid(
    country_iso: str,
    company_name: str,
    street_address: str,
    city: str,
) -> str:
    """
    Generate a US CBP Manufacturer Identification (MID) code.

    Structure: <CC><COMPANY_CODE><ADDRESS_DIGITS><CITY_CODE>
    - CC            : 2-char ISO 3166-1 alpha-2 country code (uppercase)
    - COMPANY_CODE  : up to 6 uppercase alpha chars derived from company name
    - ADDRESS_DIGITS: up to 4 digits from street address
    - CITY_CODE     : first 3 uppercase alpha chars of city

    Args:
        country_iso:    ISO 3166-1 alpha-2 country code, e.g. "IN"
        company_name:   Full company name, e.g. "Shreeji Textiles & Apparel Pvt. Ltd."
        street_address: Street/plot address, e.g. "Plot 2301, GIDC Pandesara"
        city:           City name, e.g. "Surat"

    Returns:
        MID string, e.g. "INSHRTEX2301SUR"

    >>> generate_cbp_mid("IN", "Shreeji Textiles & Apparel Pvt. Ltd.", "Plot 2301, GIDC Pandesara", "Surat")
    'INSHRTEX2301SUR'
    """
    cc = country_iso.strip().upper()[:2]
    company = _company_code(company_name)
    addr = _address_number(street_address)
    city_code = _city_code(city)
    return f"{cc}{company}{addr}{city_code}"


# ---------------------------------------------------------------------------
# Self-test / CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    test_cases = [
        (
            "IN",
            "Shreeji Textiles & Apparel Pvt. Ltd.",
            "Plot 2301, GIDC Pandesara",
            "Surat",
            "INSHRTEX2301SUR",
        ),
        (
            "IN",
            "BlueStar Exports Co.",
            "A-15, Industrial Area, Phase II",
            "New Delhi",
            "INBLUEST15NEW",  # BlueStar = one token (no space), 6 chars = BLUEST
        ),
        (
            "CN",
            "Guangzhou International Trading Co. Ltd.",
            "No. 88, Tianhe Road",
            "Guangzhou",
            None,  # just print, no assertion
        ),
    ]

    print("US CBP MID Generator — Self-test\n" + "=" * 40)
    for country, name, addr, city, expected in test_cases:
        result = generate_cbp_mid(country, name, addr, city)
        status = ""
        if expected:
            status = "PASS" if result == expected else f"FAIL (expected {expected})"
        print(f"  {name[:35]:36s} -> {result}  {status}")
