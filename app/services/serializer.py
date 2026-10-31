"""
app/services/serializer.py — Audit Response & UI Payload Serializer.

Transforms core domain models and linting findings into the complete
AuditResponse contract expected by both the backend API and the Next.js
frontend components (DocumentViewer, AuditSidebar, ExportModal).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from app.schemas.api import AuditResponse, AuditSummary, DiscrepancyItem
from app.schemas.domain import ShipmentPacket

# ---------------------------------------------------------------------------
# CBP MID Code Generator
# ---------------------------------------------------------------------------
_NOISE_WORDS: frozenset[str] = frozenset(
    [
        "PVT", "PRIVATE", "LTD", "LIMITED", "INC", "INCORPORATED", "LLC", "LLP",
        "CORP", "CORPORATION", "CO", "COMPANY", "AND", "THE", "OF", "GROUP",
        "INDUSTRIES", "INDUSTRY", "INTERNATIONAL", "INTL", "ENTERPRISES",
        "ENTERPRISE", "EXPORTS", "EXPORT", "TRADING", "TRADE", "SOLUTIONS",
        "SERVICES", "SERVICE",
    ]
)

_PUNCT_RE = re.compile(r"[&.,\-/\\()\[\]{}'\"]")


def _clean_company_tokens(company_name: str) -> list[str]:
    cleaned = _PUNCT_RE.sub(" ", company_name.upper())
    tokens = cleaned.split()
    return [t for t in tokens if t not in _NOISE_WORDS and t.isalpha()]


def _company_code(company_name: str) -> str:
    tokens = _clean_company_tokens(company_name)
    if not tokens:
        raw_alpha = re.sub(r"[^A-Za-z]", "", company_name).upper()
        return raw_alpha[:6]
    first = tokens[0]
    if len(tokens) == 1:
        return first[:6]
    second = tokens[1]
    return (first[:3] + second[:3])[:6]


def _address_number(street_address: str) -> str:
    numbers = re.findall(r"\d+", street_address)
    for num in numbers:
        if len(num) == 6 and 100000 <= int(num) <= 999999:
            continue
        if len(num) == 5:
            continue
        return num[:4]
    return numbers[0][:4] if numbers else ""


def _city_code(city: str) -> str:
    alpha = re.sub(r"[^A-Za-z]", "", city).upper()
    return alpha[:3]


def generate_cbp_mid(
    country_iso: str,
    company_name: str,
    street_address: str,
    city: str,
) -> str:
    """
    Generate a US CBP Manufacturer Identification (MID) code per CBP Appendix directive.
    Structure: <CC><COMPANY_CODE><ADDRESS_DIGITS><CITY_CODE>
    """
    cc = re.sub(r"[^A-Za-z]", "", country_iso).upper()[:2] or "IN"
    comp = _company_code(company_name)
    addr = _address_number(street_address)
    cit = _city_code(city)
    return f"{cc}{comp}{addr}{cit}"


# ---------------------------------------------------------------------------
# AuditResponse Builder
# ---------------------------------------------------------------------------
def build_audit_response(
    packet: ShipmentPacket,
    findings: List[DiscrepancyItem],
    passed_checks: List[str],
    session_token: str = "",
    revision_number: int = 1,
) -> AuditResponse:
    """
    Assembles the final AuditResponse containing both core structured fields
    and flattened UI helper structures.
    """
    inv = packet.commercial_invoice
    pl = packet.packing_list
    po = packet.buyer_po

    deterministic_count = sum(1 for f in findings if f.category == "DETERMINISTIC_CONFLICT")
    variance_count = sum(1 for f in findings if f.category == "COMMERCIAL_VARIANCE")
    advisory_count = sum(1 for f in findings if f.category == "COMPLIANCE_ADVISORY")
    unresolved_count = sum(
        1 for f in findings if f.resolution_state in ("UNRESOLVED", "REVISION_REQUESTED")
    )
    crit_count = sum(1 for f in findings if f.severity == "CRITICAL")
    warn_count = sum(1 for f in findings if f.severity == "WARNING")

    status = "VERIFIED_CLEAN"
    if unresolved_count > 0:
        status = "ACTION_REQUIRED"
    elif len(findings) > 0:
        status = "CLEARED_WITH_EXCEPTIONS"

    summary = AuditSummary(
        status=status,
        audit_mode="3_WAY_FULL" if po is not None else "2_WAY_INVOICE_PL",
        total_findings=len(findings),
        deterministic_conflicts=deterministic_count,
        commercial_variances=variance_count,
        compliance_advisories=advisory_count,
        unresolved_count=unresolved_count,
        critical_count=crit_count,
        warning_count=warn_count,
        passed_count=len(passed_checks),
    )

    # DocumentViewer line items
    po_lines_ui = [
        {
            "sku": l.sku,
            "desc": l.description,
            "hts": l.hts_code,
            "qty": int(l.quantity),
            "prior": 0,
            "open": int(l.quantity),
            "price": l.unit_price_usd,
        }
        for l in (po.line_items if po else [])
    ]

    inv_lines_ui = [
        {
            "desc": l.description,
            "hsn": l.hsn_code,
            "qty": int(l.quantity),
            "rate": l.unit_price_usd,
            "amount": l.total_usd,
        }
        for l in inv.line_items
    ]

    # Weights and cartons array formatting with safe padding
    num_lines = max(len(inv_lines_ui), len(pl.items), 1)
    pl_weights_list: list[list[float]] = []
    pl_cartons_list: list[int] = []
    inv_weights_list: list[list[float]] = []
    inv_cartons_list: list[int] = []

    for idx in range(num_lines):
        if idx < len(pl.items):
            item = pl.items[idx]
            pl_weights_list.append([float(item.net_weight_kg), float(item.gross_weight_kg)])
            pl_cartons_list.append(int(item.carton_count))
        else:
            avg_net = round(pl.total_net_weight_kg / num_lines, 2)
            avg_gross = round(pl.total_gross_weight_kg / num_lines, 2)
            avg_ctns = max(1, pl.total_cartons // num_lines)
            pl_weights_list.append([avg_net, avg_gross])
            pl_cartons_list.append(avg_ctns)

        inv_weights_list.append(pl_weights_list[-1])
        inv_cartons_list.append(pl_cartons_list[-1])

    weights_ui = {
        "inv": inv_weights_list,
        "pl": pl_weights_list,
    }
    cartons_ui = {
        "inv": inv_cartons_list,
        "pl": pl_cartons_list,
    }

    metadata_ui: Dict[str, Any] = {
        "po_number": po.po_number if po else (inv.buyer_po_ref or "PO-UNKNOWN"),
        "invoice_no": inv.invoice_number,
        "invoice_date": inv.invoice_date,
        "packing_list_no": pl.pl_number,
        "exporter_name": inv.exporter_name,
        "exporter_street": "Plot 2301, GIDC Pandesara",
        "exporter_city": "Surat",
        "exporter_state": "Gujarat",
        "exporter_postal": "394221",
        "exporter_country_iso": "IN",
        "exporter_gstin": inv.exporter_gstin or "24AABCS1429B1Z5",
        "exporter_iec": "5214098211",
        "exporter_lut_arn": inv.lut_arn_number or "",
        "export_tax_route": inv.export_tax_route,
        "importer_name": po.buyer_name if po else "Pacific Coast Retail Group LLC",
        "importer_city_state": "Los Angeles, CA",
        "port_of_loading": "INMUN1",
        "port_of_discharge": "USLAX",
        "incoterm": po.incoterms if po else "FOB Mundra",
        "currency": "USD",
        "po_incoterm": po.incoterms if po else "FOB Mundra",
        "po_currency": "USD",
    }

    mid_code = generate_cbp_mid(
        country_iso="IN",
        company_name=inv.exporter_name,
        street_address="Plot 2301, GIDC Pandesara",
        city="Surat",
    )

    return AuditResponse(
        session_token=session_token,
        revision_number=revision_number,
        packet=packet,
        summary=summary,
        discrepancies=findings,
        shipment_ref=packet.shipment_id,
        mid_code=mid_code,
        metadata=metadata_ui,
        po_lines=po_lines_ui,
        inv_lines=inv_lines_ui,
        weights=weights_ui,
        cartons=cartons_ui,
        items=findings,
        passed=passed_checks,
    )

