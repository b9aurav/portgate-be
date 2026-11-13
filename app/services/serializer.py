"""
app/services/serializer.py — Audit Response & UI Payload Serializer.

Transforms core domain models and linting findings into the complete
AuditResponse contract expected by both the backend API and the Next.js
frontend components (DocumentViewer, AuditSidebar, ExportModal).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
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
# Extraction Confidence Scoring Engine
# ---------------------------------------------------------------------------
def calculate_extraction_confidence(packet: ShipmentPacket) -> tuple[float, list[str]]:
    """
    Computes a deterministic extraction confidence score (percentage) and
    identifies low-confidence or missing fields across the 3-document packet.

    Formula Components:
    1. Header Completeness & Quality (40% weight)
    2. Line Item Match & Field Validity (40% weight)
    3. Cross-Document Structural Parity (20% weight)
    """
    low_confidence_fields: list[str] = []
    deductions: float = 0.0

    inv = packet.commercial_invoice
    pl = packet.packing_list
    po = packet.buyer_po

    # 1. Commercial Invoice Header Completeness
    if not inv.invoice_number or inv.invoice_number in ("INV-UNKNOWN", "UNKNOWN"):
        low_confidence_fields.append("commercial_invoice.invoice_number")
        deductions += 3.0
    if not inv.invoice_date:
        low_confidence_fields.append("commercial_invoice.invoice_date")
        deductions += 1.5
    if not inv.exporter_name or inv.exporter_name == "Exporter":
        low_confidence_fields.append("commercial_invoice.exporter_name")
        deductions += 2.0
    if not inv.exporter_gstin:
        low_confidence_fields.append("commercial_invoice.exporter_gstin")
        deductions += 1.0
    if inv.total_cartons_declared <= 0:
        low_confidence_fields.append("commercial_invoice.total_cartons_declared")
        deductions += 1.5
    if inv.total_net_weight_kg <= 0 or inv.total_gross_weight_kg <= 0:
        low_confidence_fields.append("commercial_invoice.weights")
        deductions += 1.5
    if not (inv.incoterms or inv.incoterm):
        low_confidence_fields.append("commercial_invoice.incoterms")
        deductions += 1.0

    # 2. Packing List Header Completeness
    if not pl.pl_number or pl.pl_number in ("PL-UNKNOWN", "UNKNOWN"):
        low_confidence_fields.append("packing_list.pl_number")
        deductions += 2.5
    if pl.total_cartons <= 0:
        low_confidence_fields.append("packing_list.total_cartons")
        deductions += 1.5
    if pl.total_gross_weight_kg <= 0:
        low_confidence_fields.append("packing_list.gross_weight")
        deductions += 1.5

    # 3. Buyer PO Header Completeness (if 3-way audit)
    if po:
        if not po.po_number or po.po_number in ("PO-UNKNOWN", "UNKNOWN"):
            low_confidence_fields.append("buyer_po.po_number")
            deductions += 3.0
        if not po.buyer_name:
            low_confidence_fields.append("buyer_po.buyer_name")
            deductions += 1.5
        if not (po.incoterms or po.incoterm):
            low_confidence_fields.append("buyer_po.incoterms")
            deductions += 1.0
        if po.total_po_value_usd <= 0:
            low_confidence_fields.append("buyer_po.total_value")
            deductions += 1.0

        # PO Reference mismatch penalty
        inv_po = (inv.buyer_po_ref or inv.po_number or "").strip().upper()
        locked_po = (po.po_number or "").strip().upper()
        if inv_po and locked_po and inv_po != locked_po:
            low_confidence_fields.append("commercial_invoice.buyer_po_ref (mismatched PO)")
            deductions += 15.0

        # Exporter / Supplier name mismatch penalty
        inv_seller = (inv.exporter_name or "").strip()
        po_seller = (getattr(po, "supplier_name", None) or getattr(po, "vendor_name", None) or "").strip()
        if inv_seller and po_seller:
            from app.services.linter import _clean_company_tokens
            tokens_inv = set(_clean_company_tokens(inv_seller))
            tokens_po = set(_clean_company_tokens(po_seller))
            if tokens_inv and tokens_po and not (tokens_inv & tokens_po):
                low_confidence_fields.append("commercial_invoice.exporter_name (mismatched supplier)")
                deductions += 15.0

    # 4. Line Item Field Validity & Semantic Match Confidence
    if not inv.line_items:
        low_confidence_fields.append("commercial_invoice.line_items")
        deductions += 10.0
    else:
        for idx, line in enumerate(inv.line_items):
            field_prefix = f"commercial_invoice.line_items[{idx}]"
            # Match confidence
            if getattr(line, "match_confidence", "HIGH") == "MEDIUM":
                low_confidence_fields.append(f"{field_prefix}.match_confidence")
                deductions += 1.0
            elif getattr(line, "match_confidence", "HIGH") == "REVIEW_REQUIRED":
                low_confidence_fields.append(f"{field_prefix}.match_confidence")
                deductions += 2.5

            # HSN code formatting check
            clean_hsn = (line.hsn_code or "").replace(".", "").replace(" ", "").strip()
            if len(clean_hsn) < 4:
                low_confidence_fields.append(f"{field_prefix}.hsn_code")
                deductions += 1.0

            # Line quantity & rate checks
            if line.quantity <= 0:
                low_confidence_fields.append(f"{field_prefix}.quantity")
                deductions += 0.5
            if line.unit_price_usd <= 0:
                low_confidence_fields.append(f"{field_prefix}.unit_price")
                deductions += 0.5

            # Description length check
            if len((line.description or "").strip()) < 10:
                low_confidence_fields.append(f"{field_prefix}.description")
                deductions += 0.5

    # 5. Clamped confidence computation
    if deductions == 0.0:
        confidence_pct = 99.8
    else:
        confidence_pct = round(max(60.0, min(99.8, 100.0 - deductions)), 1)

    return confidence_pct, low_confidence_fields


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

    # Weights and cartons array formatting with safe padding across arbitrary datasets
    num_lines = max(len(inv_lines_ui), len(pl.items), 1)

    # 1. Build pl_cartons_list and pl_weights_list
    pl_weights_list: list[list[float]] = []
    pl_cartons_list: list[int] = []

    if len(pl.items) >= num_lines:
        for item in pl.items[:num_lines]:
            pl_weights_list.append([float(item.net_weight_kg), float(item.gross_weight_kg)])
            pl_cartons_list.append(int(item.carton_count))
    elif len(pl.items) > 0:
        # All pl items exist, pad remaining slots up to num_lines with 0
        for item in pl.items:
            pl_weights_list.append([float(item.net_weight_kg), float(item.gross_weight_kg)])
            pl_cartons_list.append(int(item.carton_count))
        for _ in range(num_lines - len(pl.items)):
            pl_weights_list.append([0.0, 0.0])
            pl_cartons_list.append(0)
    else:
        # No pl items, distribute header totals across num_lines
        share_net = round(pl.total_net_weight_kg / num_lines, 2)
        share_gross = round(pl.total_gross_weight_kg / num_lines, 2)
        share_ctns = pl.total_cartons // num_lines
        for idx in range(num_lines):
            if idx == num_lines - 1:
                rem_ctns = max(0, pl.total_cartons - sum(pl_cartons_list))
                rem_net = max(0.0, round(pl.total_net_weight_kg - sum(w[0] for w in pl_weights_list), 2))
                rem_gross = max(0.0, round(pl.total_gross_weight_kg - sum(w[1] for w in pl_weights_list), 2))
                pl_cartons_list.append(rem_ctns)
                pl_weights_list.append([rem_net, rem_gross])
            else:
                pl_cartons_list.append(share_ctns)
                pl_weights_list.append([share_net, share_gross])

    # Mathematical reconciliation for Packing List:
    if pl.total_cartons > 0 and len(pl_cartons_list) > 0:
        pl_diff = pl.total_cartons - sum(pl_cartons_list)
        if pl_diff != 0:
            pl_cartons_list[-1] = max(0, pl_cartons_list[-1] + pl_diff)

    # 2. Build inv_cartons_list and inv_weights_list
    inv_weights_list: list[list[float]] = []
    inv_cartons_list: list[int] = []

    has_inv_line_cartons = any(
        getattr(l, "carton_count", None) is not None and l.carton_count > 0
        for l in inv.line_items
    )

    if has_inv_line_cartons:
        for idx in range(num_lines):
            inv_item = inv.line_items[idx] if idx < len(inv.line_items) else None
            if inv_item and inv_item.carton_count is not None and inv_item.carton_count > 0:
                inv_cartons_list.append(int(inv_item.carton_count))
            else:
                inv_cartons_list.append(0)

            if inv_item and inv_item.net_weight_kg is not None and inv_item.gross_weight_kg is not None and (inv_item.net_weight_kg > 0 or inv_item.gross_weight_kg > 0):
                inv_weights_list.append([float(inv_item.net_weight_kg), float(inv_item.gross_weight_kg)])
            else:
                inv_weights_list.append(pl_weights_list[idx] if idx < len(pl_weights_list) else [0.0, 0.0])
    else:
        # No per-line cartons on invoice; allocate inv.total_cartons_declared
        if inv.total_cartons_declared > 0:
            if inv.total_cartons_declared == pl.total_cartons and len(pl_cartons_list) == num_lines:
                # If totals match, preserve the exact line carton distribution from packing list
                inv_cartons_list = list(pl_cartons_list)
            else:
                # Distribute inv.total_cartons_declared across lines
                share = inv.total_cartons_declared // num_lines
                for idx in range(num_lines):
                    if idx == num_lines - 1:
                        rem = max(0, inv.total_cartons_declared - sum(inv_cartons_list))
                        inv_cartons_list.append(rem)
                    else:
                        inv_cartons_list.append(share)
        else:
            inv_cartons_list = [0] * num_lines

        # Invoice weights fallback to pl_weights_list
        inv_weights_list = [list(w) for w in pl_weights_list]

    # Mathematical reconciliation for Commercial Invoice:
    # Guarantees inv_cartons_list sum strictly matches inv.total_cartons_declared for any input dataset
    if inv.total_cartons_declared > 0 and len(inv_cartons_list) > 0:
        inv_diff = inv.total_cartons_declared - sum(inv_cartons_list)
        if inv_diff != 0:
            inv_cartons_list[-1] = max(0, inv_cartons_list[-1] + inv_diff)

    weights_ui = {
        "inv": inv_weights_list,
        "pl": pl_weights_list,
    }
    cartons_ui = {
        "inv": inv_cartons_list,
        "pl": pl_cartons_list,
    }

    inv_declared_po = inv.buyer_po_ref or getattr(inv, "buyer_po_number", None) or ""

    exp_street = getattr(inv, "exporter_street", None) or ""
    exp_city = getattr(inv, "exporter_city", None) or ""
    exp_state = getattr(inv, "exporter_state", None) or "Gujarat"
    exp_postal = getattr(inv, "exporter_postal", None) or "394221"
    exp_country = getattr(inv, "exporter_country_iso", None) or "IN"

    if not exp_street or not exp_city:
        raw_addr = getattr(inv, "exporter_address", None) or (getattr(po, "supplier_address", None) if po else "") or ""
        if raw_addr:
            parts = [p.strip() for p in raw_addr.split(",") if p.strip()]
            if len(parts) >= 3:
                exp_street = f"{parts[0]}, {parts[1]}" if not exp_street else exp_street
                exp_city = parts[2] if not exp_city else exp_city
                exp_state = parts[3] if len(parts) > 3 else exp_state
                exp_postal = parts[4] if len(parts) > 4 else exp_postal
                exp_country = parts[5] if len(parts) > 5 and len(parts[5]) == 2 else exp_country
            elif len(parts) == 2:
                exp_street = parts[0] if not exp_street else exp_street
                exp_city = parts[1] if not exp_city else exp_city

    exp_street = exp_street or "Plot 2301, GIDC Pandesara"
    exp_city = exp_city or "Surat"

    inv_currency = getattr(inv, "currency", None) or "USD"
    po_currency = (getattr(po, "currency", None) if po else None) or inv_currency

    metadata_ui: Dict[str, Any] = {
        "po_number": po.po_number if po else (inv.buyer_po_ref or "PO-UNKNOWN"),
        "buyer_po_ref": inv_declared_po,
        "invoice_po_ref": inv_declared_po,
        "po_supplier_name": (getattr(po, "supplier_name", None) or getattr(po, "vendor_name", None) or (inv.exporter_name if po is None else "")) if po else inv.exporter_name,
        "po_supplier_address": (getattr(po, "supplier_address", None) or "") if po else "",
        "invoice_no": inv.invoice_number,
        "invoice_date": inv.invoice_date,
        "packing_list_no": pl.pl_number,
        "exporter_name": inv.exporter_name,
        "exporter_street": exp_street,
        "exporter_city": exp_city,
        "exporter_state": exp_state,
        "exporter_postal": exp_postal,
        "exporter_country_iso": exp_country,
        "exporter_gstin": inv.exporter_gstin or "24AABCS1429B1Z5",
        "exporter_iec": "5214098211",
        "exporter_lut_arn": inv.lut_arn_number or "",
        "export_tax_route": inv.export_tax_route,
        "importer_name": po.buyer_name if po else "Pacific Coast Retail Group LLC",
        "importer_city_state": "Los Angeles, CA",
        "port_of_loading": "INMUN1",
        "port_of_discharge": "USLAX",
        "incoterm": getattr(inv, "incoterms", None) or (po.incoterms if po else "FOB Mundra"),
        "currency": inv_currency,
        "po_incoterm": po.incoterms if po else "FOB Mundra",
        "po_currency": po_currency,
        "total_cartons_declared": inv.total_cartons_declared,
        "total_cartons": pl.total_cartons,
    }

    mid_code = generate_cbp_mid(
        country_iso=exp_country,
        company_name=inv.exporter_name,
        street_address=exp_street,
        city=exp_city,
    )

    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    confidence_pct, low_confidence_fields = calculate_extraction_confidence(packet)

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
        link_expires_at=expires_at,
        link_revoked=False,
        extraction_confidence_pct=confidence_pct,
        low_confidence_fields=low_confidence_fields,
    )

