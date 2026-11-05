"""
app/services/linter.py — 100% Deterministic Python Customs Rules Engine.

Evaluates trade document consistency across 3 tiers:
  Tier 1: Deterministic Conflicts (Internal Math & Invoice <-> Packing List)
    - RULE-D01-CARTON-SYNC          (CRITICAL / crit)
    - RULE-D02-WEIGHT-PHYSICS       (CRITICAL / crit)
    - RULE-D03-GROSS-WT-SYNC        (WARNING / warn)
    - RULE-D04-UOM-CONSISTENCY      (CRITICAL / crit)
    - RULE-D05-LINE-TOTAL-ARITHMETIC (CRITICAL / crit)

  Tier 2: Commercial Variances (Buyer PO vs. Invoice)
    - RULE-V01-PO-PRICE-VARIANCE    (CRITICAL / crit)
    - RULE-V02-PO-QTY-TOLERANCE     (WARNING / warn)

  Tier 3: Compliance Advisories (HSN/HTS Root & Indian Export GST Route)
    - RULE-C01-WCO-HS6-DIVERGENCE   (WARNING / warn)
    - RULE-C02-EXPORT-GST-ROUTE     (WARNING / warn)

The LLM is NEVER consulted here. All rules are pure deterministic Python.
"""

from __future__ import annotations

import re
from typing import List, Tuple

from app.schemas.api import AuditResponse, DiscrepancyItem
from app.schemas.domain import ShipmentPacket

CBP_VAGUE_DESCRIPTIONS_VERSION = "2025-09-ACE"

CBP_VAGUE_DESCRIPTIONS = frozenset({
    "apparel",
    "garments",
    "textiles",
    "cloth",
    "clothing",
    "parts",
    "auto parts",
    "samples",
    "general cargo",
    "merchandise",
    "tiles",
    "goods",
})


def _clean_digits(val: str | None) -> str:
    """Extract numeric digits from code for international root comparison."""
    return re.sub(r"\D", "", str(val or ""))


def _extract_incoterm_code(text: str | None) -> str | None:
    """Extract standard 3-letter Incoterm code from delivery terms string."""
    if not text:
        return None
    match = re.search(r"\b(EXW|FCA|CPT|CIP|DAP|DPU|DDP|FAS|FOB|CFR|CIF)\b", text, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    cleaned = text.strip()[:3].upper()
    return cleaned if len(cleaned) == 3 and cleaned.isalpha() else None


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


def _match_inv_line_to_po(inv_item: InvoiceLineItem, po_lines: List[POLineItem]) -> Optional[POLineItem]:
    """
    Semantically matches a Commercial Invoice line item to its corresponding
    line on the locked Buyer PO using SKU, exact 6-digit HS root, line ref compatibility,
    and product description commodity keywords.
    """
    inv_desc = (inv_item.description or "").lower()
    inv_hsn_root = _clean_digits(inv_item.hsn_code)[:6]
    inv_hsn_ch = inv_hsn_root[:2] if len(inv_hsn_root) >= 2 else ""

    # 1. Exact or partial SKU match
    for p in po_lines:
        p_sku = (p.sku or "").lower().strip()
        if p_sku and (p_sku in inv_desc or p_sku == getattr(inv_item, "sku", "").lower().strip()):
            return p

    # 2. 6-digit HS root exact match
    for p in po_lines:
        p_hts_root = _clean_digits(p.hts_code)[:6]
        if inv_hsn_root and p_hts_root and inv_hsn_root == p_hts_root:
            return p

    # 3. Line ref match ONLY IF commodities are compatible
    # (Textile chapters 50-63 are mutually compatible apparel chapters;
    # but Chapter 69 Ceramics is strictly incompatible with Chapter 61 Apparel)
    if inv_item.po_line_ref:
        for p in po_lines:
            if p.line_no == inv_item.po_line_ref:
                p_hts_ch = _clean_digits(p.hts_code)[:2]
                is_compatible = True
                if inv_hsn_ch and p_hts_ch:
                    if inv_hsn_ch != p_hts_ch:
                        textile_chapters = {str(c).zfill(2) for c in range(50, 64)}
                        if not (inv_hsn_ch in textile_chapters and p_hts_ch in textile_chapters):
                            is_compatible = False
                if is_compatible:
                    return p

    # 4. Keyword semantic overlap match (e.g. "t-shirt", "polo", "hoodie", "fleece")
    inv_words = set(re.findall(r"[a-z]{4,}", inv_desc)) - {"with", "size", "inch", "pack", "white", "black", "item", "good", "goods"}
    best_p = None
    best_overlap = 0
    for p in po_lines:
        p_words = set(re.findall(r"[a-z]{4,}", (p.description or "").lower())) - {"with", "size", "inch", "pack", "white", "black", "item", "good", "goods"}
        overlap = len(inv_words & p_words)
        p_hts_ch = _clean_digits(p.hts_code)[:2]
        is_compatible = True
        if inv_hsn_ch and p_hts_ch and inv_hsn_ch != p_hts_ch:
            textile_chapters = {str(c).zfill(2) for c in range(50, 64)}
            if not (inv_hsn_ch in textile_chapters and p_hts_ch in textile_chapters):
                is_compatible = False
        if overlap > best_overlap and is_compatible:
            best_overlap = overlap
            best_p = p

    if best_overlap >= 1:
        return best_p

    return None


def run_deterministic_lint(packet: ShipmentPacket) -> Tuple[List[DiscrepancyItem], List[str]]:
    """
    Executes the deterministic compliance rules against the ShipmentPacket.
    Returns (findings, passed_checks).
    """
    findings: List[DiscrepancyItem] = []
    inv = packet.commercial_invoice
    pl = packet.packing_list
    po = packet.buyer_po

    # =========================================================================
    # TIER 0: FAIL-CLOSED MANDATORY FIELD CHECKS (Header & Arithmetic Validation)
    # =========================================================================

    # Check 0a: Mandatory customs fields present and non-zero
    missing_required = (
        inv.total_cartons_declared <= 0
        or pl.total_cartons <= 0
        or inv.total_gross_weight_kg <= 0
        or pl.total_gross_weight_kg <= 0
        or inv.total_net_weight_kg <= 0
        or pl.total_net_weight_kg <= 0
        or len(inv.line_items) == 0
        or (po is not None and len(po.line_items) == 0)
    )
    if missing_required:
        desc_missing = (
            "Mandatory customs declaration missing or zero. Commercial invoice and packing list "
            "must declare positive total cartons, net weight, gross weight, and line items. "
            "Packet fails closed to prevent customs seizure."
        )
        findings.append(
            DiscrepancyItem(
                id="DISC-MISSING-REQUIRED",
                rule_id="RULE-D00-FAIL-CLOSED-MANDATORY",
                category="DETERMINISTIC_CONFLICT",
                severity="CRITICAL",
                sev="crit",
                title="Missing Mandatory Weight or Carton Declaration",
                description=desc_missing,
                body=desc_missing,
                impact="Immediate customs entry filing failure; packet fails closed under 19 CFR regulations.",
                fix="Supplier must declare positive total cartons, gross/net weights, and complete item lines.",
                source_doc_a="Commercial Invoice",
                source_field_a="total_cartons_declared / weights",
                source_val_a=f"{inv.total_cartons_declared} CTNS, Net {inv.total_net_weight_kg:.1f}kg, Gross {inv.total_gross_weight_kg:.1f}kg",
                source_doc_b="Packing List",
                source_field_b="total_cartons / weights",
                source_val_b=f"{pl.total_cartons} CTNS, Net {pl.total_net_weight_kg:.1f}kg, Gross {pl.total_gross_weight_kg:.1f}kg",
                highlight_cells=["inv-ctns", "pl-ctns"],
                cells=["inv-ctns", "pl-ctns"],
                locs=[["inv", "Commercial Invoice"], ["pl", "Packing List"]],
                recommended_actions=[
                    "Ensure Commercial Invoice declares non-zero total cartons, net weight, and gross weight",
                    "Ensure Packing List declares non-zero total cartons, net weight, and gross weight",
                    "Ensure document line items are fully itemized",
                ],
            )
        )

    # Check 0b: Header vs. Line Sum Arithmetic
    if len(inv.line_items) > 0:
        inv_lines_sum = round(sum(l.total_usd for l in inv.line_items), 2)
        if abs(inv_lines_sum - inv.total_invoice_value_usd) > 0.05:
            diff_sum = abs(inv_lines_sum - inv.total_invoice_value_usd)
            desc_sum = (
                f"Commercial Invoice header declares ${inv.total_invoice_value_usd:,.2f}, "
                f"but sum of line items equals ${inv_lines_sum:,.2f} (variance of ${diff_sum:,.2f}). "
                "Extended line items must reconcile with header total."
            )
            findings.append(
                DiscrepancyItem(
                    id="DISC-INVOICE-TOTAL-SUM",
                    rule_id="RULE-D06-HEADER-LINE-SUM-SYNC",
                    category="DETERMINISTIC_CONFLICT",
                    severity="CRITICAL",
                    sev="crit",
                    title="Commercial Invoice Header Total vs. Line Sum Mismatch",
                    description=desc_sum,
                    body=desc_sum,
                    impact="CBP Form 7501 entry rejection for header total value mismatch vs. line item aggregate.",
                    fix="Regenerate invoice in ERP to ensure line item totals sum exactly to invoice header total.",
                    source_doc_a="Invoice Line Items Sum",
                    source_field_a="sum(line_items.total_usd)",
                    source_val_a=f"${inv_lines_sum:,.2f}",
                    source_doc_b="Commercial Invoice Header",
                    source_field_b="total_invoice_value_usd",
                    source_val_b=f"${inv.total_invoice_value_usd:,.2f}",
                    highlight_cells=["inv-total-amt"],
                    cells=["inv-total-amt"],
                    locs=[["inv", "Commercial Invoice"]],
                    recommended_actions=[
                        "Reconcile invoice header total with sum of extended line totals",
                        "Regenerate Tally invoice with matching header amount",
                    ],
                )
            )

    # =========================================================================
    # TIER 1: DETERMINISTIC CONFLICTS (Internal Math & Invoice <-> Packing List)
    # =========================================================================

    # 1. RULE-D01-CARTON-SYNC (CRITICAL)
    if inv.total_cartons_declared != pl.total_cartons:
        diff = abs(inv.total_cartons_declared - pl.total_cartons)
        desc = (
            f"Commercial Invoice declares {inv.total_cartons_declared} cartons, "
            f"but Packing List totals {pl.total_cartons} cartons ({diff} carton variance). "
            "Both documents must agree before shipping bill and customs manifest generation."
        )
        findings.append(
            DiscrepancyItem(
                id="DISC-CARTONS",
                rule_id="RULE-D01-CARTON-SYNC",
                category="DETERMINISTIC_CONFLICT",
                severity="CRITICAL",
                sev="crit",
                title="Carton Count Mismatch (Invoice vs. Packing List)",
                description=desc,
                body=desc,
                impact="Mismatched carton counts trigger mandatory physical container exams and customs holds.",
                fix="Regenerate invoice or packing list in exporter ERP so total carton count is identical.",
                source_doc_a="Commercial Invoice",
                source_field_a="total_cartons_declared",
                source_val_a=f"{inv.total_cartons_declared} CTNS",
                source_doc_b="Packing List",
                source_field_b="total_cartons",
                source_val_b=f"{pl.total_cartons} CTNS",
                highlight_cells=["inv-ctns", "pl-ctns"],
                cells=["inv-ctns", "pl-ctns"],
                locs=[["inv", "Commercial Invoice"], ["pl", "Packing List"]],
                recommended_actions=[
                    "Verify physical warehouse tally sheet",
                    "Regenerate either Tally Invoice or Packing List so carton counts match",
                ],
            )
        )

    # 2. RULE-D02-WEIGHT-PHYSICS (CRITICAL)
    inverted_pl_items = [
        item for item in pl.items
        if item.net_weight_kg >= item.gross_weight_kg
    ]
    header_pl_inverted = pl.total_net_weight_kg >= pl.total_gross_weight_kg
    header_inv_inverted = inv.total_net_weight_kg >= inv.total_gross_weight_kg

    if inverted_pl_items:
        first = inverted_pl_items[0]
        line_num = first.line_no
        item_desc = first.description
        net_val = first.net_weight_kg
        gross_val = first.gross_weight_kg
        diff = net_val - gross_val

        desc = (
            f"Line #{line_num} ({item_desc}): Inverted weights declared on Packing List — "
            f"Net Weight ({net_val:.1f} kg) exceeds Gross Weight ({gross_val:.1f} kg) by {diff:.1f} kg. "
            f"Net cargo weight cannot physically exceed carton gross weight. "
            f"(Total header weights: Net {pl.total_net_weight_kg:.1f} kg vs Gross {pl.total_gross_weight_kg:.1f} kg)."
        )
        title = f"Line #{line_num}: Physical Weight Inversion (Net {net_val:.1f} kg > Gross {gross_val:.1f} kg)"
        fix = f"Re-scale Line #{line_num} cartons and swap inverted Net ({net_val:.1f} kg) vs Gross ({gross_val:.1f} kg) weights."
        cells = [f"pl-net{line_num}", f"pl-gross{line_num}", f"inv-net{line_num}", f"inv-gross{line_num}"]

        findings.append(
            DiscrepancyItem(
                id="DISC-WEIGHT-INV",
                rule_id="RULE-D02-WEIGHT-PHYSICS",
                category="DETERMINISTIC_CONFLICT",
                severity="CRITICAL",
                sev="crit",
                title=title,
                description=desc,
                body=desc,
                impact="Immediate ICEGATE and CBP automated manifest rejection due to physical impossibility.",
                fix=fix,
                source_doc_a="Packing List",
                source_field_a=f"Line #{line_num} Net Weight",
                source_val_a=f"{net_val:.1f} KG",
                source_doc_b="Packing List",
                source_field_b=f"Line #{line_num} Gross Weight",
                source_val_b=f"{gross_val:.1f} KG",
                highlight_cells=cells,
                cells=cells,
                locs=[["pl", "Packing List"], ["inv", "Commercial Invoice"]],
                recommended_actions=[
                    f"Correct Line #{line_num} Net ({net_val:.1f} kg) and Gross ({gross_val:.1f} kg) weights in Packing List",
                    "Perform certified scale weighment before port gate-in",
                ],
            )
        )
    elif header_pl_inverted or header_inv_inverted:
        doc_name = "Packing List" if header_pl_inverted else "Commercial Invoice"
        net_wt = pl.total_net_weight_kg if header_pl_inverted else inv.total_net_weight_kg
        gross_wt = pl.total_gross_weight_kg if header_pl_inverted else inv.total_gross_weight_kg
        diff = net_wt - gross_wt
        desc = (
            f"{doc_name} Total Net Weight ({net_wt:.1f} kg) is greater than or equal to "
            f"Total Gross Weight ({gross_wt:.1f} kg) by {diff:.1f} kg. Net cargo weight cannot exceed gross weight."
        )
        title = f"Total Weight Inversion (Net {net_wt:.1f} kg >= Gross {gross_wt:.1f} kg)"
        fix = f"Re-scale shipment cartons and swap inverted Net ({net_wt:.1f} kg) vs Gross ({gross_wt:.1f} kg) weights."
        cells = ["pl-net-wt", "pl-gross-wt", "inv-net", "inv-gross"]

        findings.append(
            DiscrepancyItem(
                id="DISC-WEIGHT-INV",
                rule_id="RULE-D02-WEIGHT-PHYSICS",
                category="DETERMINISTIC_CONFLICT",
                severity="CRITICAL",
                sev="crit",
                title=title,
                description=desc,
                body=desc,
                impact="Immediate ICEGATE and CBP automated manifest rejection due to physical impossibility.",
                fix=fix,
                source_doc_a=doc_name,
                source_field_a="total_net_weight_kg",
                source_val_a=f"{net_wt:.1f} KG",
                source_doc_b=doc_name,
                source_field_b="total_gross_weight_kg",
                source_val_b=f"{gross_wt:.1f} KG",
                highlight_cells=cells,
                cells=cells,
                locs=[["pl", "Packing List"], ["inv", "Commercial Invoice"]],
                recommended_actions=[
                    "Swap inverted Net/Gross weight header totals",
                    "Perform certified scale weighment before port gate-in",
                ],
            )
        )

    # 3. RULE-D03-GROSS-WT-SYNC (WARNING)
    if abs(inv.total_gross_weight_kg - pl.total_gross_weight_kg) > 0.5:
        desc = (
            f"Invoice declares {inv.total_gross_weight_kg:.1f} kg Gross Weight vs. "
            f"Packing List {pl.total_gross_weight_kg:.1f} kg."
        )
        findings.append(
            DiscrepancyItem(
                id="DISC-GROSS-SYNC",
                rule_id="RULE-D03-GROSS-WT-SYNC",
                category="DETERMINISTIC_CONFLICT",
                severity="WARNING",
                sev="warn",
                title="Gross Weight Disagreement Across Invoice & Packing List",
                description=desc,
                body=desc,
                impact="Carrier bill of lading weight disagreement causing VGM discrepancy fines.",
                fix="Align invoice weight statement table with final packing list scale gross weight.",
                source_doc_a="Commercial Invoice",
                source_field_a="total_gross_weight_kg",
                source_val_a=f"{inv.total_gross_weight_kg:.1f} KG",
                source_doc_b="Packing List",
                source_field_b="total_gross_weight_kg",
                source_val_b=f"{pl.total_gross_weight_kg:.1f} KG",
                highlight_cells=["inv-gross-wt", "pl-gross-wt"],
                cells=["inv-gross-wt", "pl-gross-wt"],
                locs=[["inv", "Commercial Invoice"], ["pl", "Packing List"]],
                recommended_actions=[
                    "Align Invoice header gross weight with final Packing List scale weight",
                ],
            )
        )

    # 4. RULE-D04-UOM-CONSISTENCY (CRITICAL)
    for idx, inv_item in enumerate(inv.line_items):
        if idx < len(pl.items):
            pl_item = pl.items[idx]
            if inv_item.uom.upper() != pl_item.uom.upper():
                desc = (
                    f"Invoice bills Line #{inv_item.line_no} in '{inv_item.uom}', "
                    f"while Packing List declares '{pl_item.uom}'."
                )
                findings.append(
                    DiscrepancyItem(
                        id=f"DISC-UOM-{inv_item.line_no}",
                        rule_id="RULE-D04-UOM-CONSISTENCY",
                        category="DETERMINISTIC_CONFLICT",
                        severity="CRITICAL",
                        sev="crit",
                        title=f"Line #{inv_item.line_no}: UOM Conflict ({inv_item.uom} vs. {pl_item.uom})",
                        description=desc,
                        body=desc,
                        impact="Unit of measure conflict causes tariff quantity re-assessment and customs delays.",
                        fix="Standardize billing unit of measure between invoice and packing list.",
                        source_doc_a="Commercial Invoice",
                        source_field_a=f"line_items[{idx}].uom",
                        source_val_a=f"{inv_item.quantity:g} {inv_item.uom}",
                        source_doc_b="Packing List",
                        source_field_b=f"items[{idx}].uom",
                        source_val_b=f"{pl_item.total_quantity:g} {pl_item.uom}",
                        highlight_cells=[f"inv-qty{inv_item.line_no}", f"pl-qty{inv_item.line_no}"],
                        cells=[f"inv-qty{inv_item.line_no}", f"pl-qty{inv_item.line_no}"],
                        locs=[["inv", "Commercial Invoice"], ["pl", "Packing List"]],
                        recommended_actions=[
                            "Standardize unit of measure across Invoice and Packing List",
                        ],
                    )
                )

    # 5. RULE-D05-LINE-TOTAL-ARITHMETIC (CRITICAL)
    for idx, inv_item in enumerate(inv.line_items):
        calc_total = round(inv_item.quantity * inv_item.unit_price_usd, 2)
        if abs(calc_total - inv_item.total_usd) > 0.05:
            desc = (
                f"Commercial Invoice line #{inv_item.line_no} printed amount is ${inv_item.total_usd:,.2f}, "
                f"but calculated total ({inv_item.quantity:g} × ${inv_item.unit_price_usd:.2f}) equals ${calc_total:,.2f}. "
                "All line item arithmetic must verify before customs entry filing."
            )
            findings.append(
                DiscrepancyItem(
                    id=f"DISC-MATH-{inv_item.line_no}",
                    rule_id="RULE-D05-LINE-TOTAL-ARITHMETIC",
                    category="DETERMINISTIC_CONFLICT",
                    severity="CRITICAL",
                    sev="crit",
                    title=f"Line #{inv_item.line_no}: Arithmetic Discrepancy (${calc_total:.2f} calc vs ${inv_item.total_usd:.2f} printed)",
                    description=desc,
                    body=desc,
                    impact="CBP Form 7501 entry reject for commercial value mismatch; potential penalty under 19 U.S.C. §1592.",
                    fix="Regenerate invoice in Tally ERP with correct extended line total math.",
                    source_doc_a="Calculated Total (Qty × Rate)",
                    source_field_a=f"line_items[{idx}].total_usd_calculated",
                    source_val_a=f"${calc_total:.2f}",
                    source_doc_b="Commercial Invoice",
                    source_field_b=f"line_items[{idx}].total_usd",
                    source_val_b=f"${inv_item.total_usd:.2f}",
                    highlight_cells=[f"inv-qty{inv_item.line_no}", f"inv-rate{inv_item.line_no}", f"inv-amt{inv_item.line_no}"],
                    cells=[f"inv-qty{inv_item.line_no}", f"inv-rate{inv_item.line_no}", f"inv-amt{inv_item.line_no}"],
                    locs=[["inv", "Commercial Invoice"]],
                    recommended_actions=[
                        "Correct line-total arithmetic in Tally ERP and re-export invoice",
                    ],
                )
            )

    # =========================================================================
    # TIER 2: COMMERCIAL VARIANCES & BASELINE CONTRACT VERIFICATION
    # =========================================================================
    if po is not None:
        # Check 2a: Strict PO Reference Match (DISC-PO-MISMATCH)
        po_ref = (getattr(inv, "buyer_po_ref", None) or getattr(inv, "buyer_po_number", None) or getattr(inv, "po_number", None) or "").strip()
        locked_po_no = (po.po_number or "").strip()
        norm_inv_po = re.sub(r"[^A-Za-z0-9]", "", po_ref).upper()
        norm_locked_po = re.sub(r"[^A-Za-z0-9]", "", locked_po_no).upper()
        if po_ref and locked_po_no and norm_inv_po != norm_locked_po:
            desc_po_mismatch = (
                f"Commercial Invoice declares Buyer PO reference '{po_ref}', "
                f"which does not match the authoritative locked Purchase Order '{locked_po_no}'. "
                "Under 19 CFR § 141.86, customs entry documentation must accurately reference the governing commercial contract."
            )
            findings.append(
                DiscrepancyItem(
                    id="DISC-PO-MISMATCH",
                    rule_id="RULE-C01-PO-REFERENCE-MISMATCH",
                    category="DETERMINISTIC_CONFLICT",
                    severity="CRITICAL",
                    sev="crit",
                    title=f"Wrong Shipment Document: Invoice References PO '{po_ref}' Instead of Locked PO '{locked_po_no}'",
                    description=desc_po_mismatch,
                    body=desc_po_mismatch,
                    impact="Immediate CBP entry rejection under 19 CFR § 141.86. Invoice is billed against an alien PO contract.",
                    fix=f"Upload commercial invoice referencing locked Purchase Order '{locked_po_no}'.",
                    source_doc_a="Buyer PO",
                    source_field_a="po_number",
                    source_val_a=locked_po_no,
                    source_doc_b="Commercial Invoice",
                    source_field_b="buyer_po_ref",
                    source_val_b=po_ref,
                    highlight_cells=["po-number", "inv-po-ref"],
                    cells=["po-number", "inv-po-ref"],
                    locs=[["po", "Buyer PO"], ["inv", "Commercial Invoice"]],
                    recommended_actions=[
                        f"Reject revised document: invoice references '{po_ref}' instead of '{locked_po_no}'",
                        f"Request supplier submit invoice specifically issued for PO '{locked_po_no}'",
                    ],
                )
            )

        # Check 2b: Exporter / Supplier Identity Mismatch (DISC-SUPPLIER-MISMATCH)
        po_supplier = getattr(po, "supplier_name", None) or getattr(po, "vendor_name", None) or ""
        inv_exporter = inv.exporter_name or ""
        if po_supplier and inv_exporter:
            po_tokens = set(_clean_company_tokens(po_supplier))
            inv_tokens = set(_clean_company_tokens(inv_exporter))
            if po_tokens and inv_tokens and not (po_tokens & inv_tokens):
                desc_sup_mismatch = (
                    f"Commercial Invoice is issued by '{inv_exporter}', "
                    f"which does not match the authorized contracting vendor '{po_supplier}' on the locked Buyer PO. "
                    "Customs valuation and CBP Manufacturer Identification (MID) require the invoice exporter to match the authorized supplier."
                )
                findings.append(
                    DiscrepancyItem(
                        id="DISC-SUPPLIER-MISMATCH",
                        rule_id="RULE-C02-SUPPLIER-IDENTITY-MISMATCH",
                        category="DETERMINISTIC_CONFLICT",
                        severity="CRITICAL",
                        sev="crit",
                        title="Exporter Identity Mismatch Against Locked Buyer PO",
                        description=desc_sup_mismatch,
                        body=desc_sup_mismatch,
                        impact="Severe CBP MID and corporate identity mismatch. Cargo manifests cannot clear under an alien corporate identity.",
                        fix=f"Ensure Commercial Invoice is issued by contracted supplier '{po_supplier}'.",
                        source_doc_a="Buyer PO",
                        source_field_a="supplier_name",
                        source_val_a=po_supplier,
                        source_doc_b="Commercial Invoice",
                        source_field_b="exporter_name",
                        source_val_b=inv_exporter,
                        highlight_cells=["po-seller", "inv-exporter"],
                        cells=["po-seller", "inv-exporter"],
                        locs=[["po", "Buyer PO"], ["inv", "Commercial Invoice"]],
                        recommended_actions=[
                            f"Confirm whether '{inv_exporter}' is an authorized sister factory or sub-contractor",
                            f"If unauthorized, reject document and demand invoice from contracted vendor '{po_supplier}'",
                        ],
                    )
                )

        # Check 2c: Line-level PO matching
        for idx, inv_item in enumerate(inv.line_items):
            po_item = _match_inv_line_to_po(inv_item, po.line_items)
            if not po_item:
                desc_unexp = (
                    f"Commercial Invoice line #{inv_item.line_no} ('{inv_item.description}', HSN {inv_item.hsn_code}) "
                    f"cannot be matched to any authorized line item on locked Buyer PO '{po.po_number}'. "
                    "All billed items on Commercial Invoice must correspond to items authorized on the governing commercial contract."
                )
                findings.append(
                    DiscrepancyItem(
                        id=f"DISC-UNEXPECTED-LINE-{inv_item.line_no}",
                        rule_id="RULE-C00-UNORDERED-ITEM",
                        category="DETERMINISTIC_CONFLICT",
                        severity="CRITICAL",
                        sev="crit",
                        title=f"Line #{inv_item.line_no}: Unordered Item Not Present on Locked Buyer PO",
                        description=desc_unexp,
                        body=desc_unexp,
                        impact="Immediate CBP Form 7501 entry hold; uncontracted commodity subject to border inspection.",
                        fix=f"Remove unordered commodity '{inv_item.description[:30]}' from invoice or issue Buyer PO amendment.",
                        source_doc_a="Buyer PO",
                        source_field_a="line_items",
                        source_val_a="Not present on PO",
                        source_doc_b="Commercial Invoice",
                        source_field_b=f"line_items[{idx}].description",
                        source_val_b=f"{inv_item.description} ({inv_item.quantity:g} {inv_item.uom})",
                        highlight_cells=[f"inv-desc{inv_item.line_no}"],
                        cells=[f"inv-desc{inv_item.line_no}"],
                        locs=[["inv", "Commercial Invoice"], ["po", "Buyer PO"]],
                        recommended_actions=[
                            "Verify if item was shipped by mistake",
                            "Request revised Commercial Invoice omitting uncontracted item",
                        ],
                    )
                )
                continue

            # 6. RULE-V01-PO-PRICE-VARIANCE (CRITICAL)
            price_diff = inv_item.unit_price_usd - po_item.unit_price_usd
            if abs(price_diff) >= 0.005:
                pct_diff = (price_diff / po_item.unit_price_usd) * 100 if po_item.unit_price_usd else 0.0
                total_impact = price_diff * inv_item.quantity
                sign = "+" if total_impact >= 0 else "-"
                impact_str = f"{sign}${abs(total_impact):,.2f}"
                desc = (
                    f"Tally Invoice bills ${inv_item.unit_price_usd:.2f}/{inv_item.uom} vs. "
                    f"Buyer PO ${po_item.unit_price_usd:.2f}/{po_item.uom} ({pct_diff:+.1f}%). "
                    "Buyer must authorize commercial overage or request invoice correction."
                )
                findings.append(
                    DiscrepancyItem(
                        id=f"DISC-PRICE-{inv_item.line_no}",
                        rule_id="RULE-V01-PO-PRICE-VARIANCE",
                        category="COMMERCIAL_VARIANCE",
                        severity="CRITICAL",
                        sev="crit",
                        title=f"Line #{inv_item.line_no}: Unit Price Variance vs. Buyer PO ({pct_diff:+.1f}%)",
                        description=desc,
                        body=desc,
                        impact=f"Commercial variance: {impact_str} USD across {inv_item.quantity:g} {inv_item.uom}.",
                        fix="If revised price was agreed: Accept Variance & attach PO Amendment. Otherwise request corrected invoice at PO rate.",
                        source_doc_a="Buyer PO",
                        source_field_a=f"line_items[{idx}].unit_price_usd",
                        source_val_a=f"${po_item.unit_price_usd:.2f}",
                        source_doc_b="Commercial Invoice",
                        source_field_b=f"line_items[{idx}].unit_price_usd",
                        source_val_b=f"${inv_item.unit_price_usd:.2f}",
                        highlight_cells=[f"po-price{inv_item.line_no}", f"inv-rate{inv_item.line_no}"],
                        cells=[f"po-price{inv_item.line_no}", f"inv-rate{inv_item.line_no}"],
                        locs=[["inv", "Commercial Invoice"], ["po", "Buyer PO"]],
                        financial_impact_note=f"Net invoice impact: {impact_str} USD across {inv_item.quantity:g} {inv_item.uom}",
                        recommended_actions=[
                            "If revised price was agreed: Accept Variance & attach Buyer PO Amendment / Email",
                            "If billing typo: Request Revised Tally Invoice at PO rate",
                        ],
                    )
                )

            # 7. RULE-V02-PO-QTY-TOLERANCE (WARNING)
            if po_item.quantity > 0:
                qty_diff_pct = ((inv_item.quantity - po_item.quantity) / po_item.quantity) * 100.0
                if abs(qty_diff_pct) > po.quantity_tolerance_pct:
                    desc = (
                        f"Invoice quantity ({inv_item.quantity:g} {inv_item.uom}) deviates from "
                        f"PO quantity ({po_item.quantity:g} {po_item.uom}) beyond allowed ±{po.quantity_tolerance_pct:g}% tolerance."
                    )
                    findings.append(
                        DiscrepancyItem(
                            id=f"DISC-QTY-{inv_item.line_no}",
                            rule_id="RULE-V02-PO-QTY-TOLERANCE",
                            category="COMMERCIAL_VARIANCE",
                            severity="WARNING",
                            sev="warn",
                            title=f"Line #{inv_item.line_no}: Quantity Variance ({qty_diff_pct:+.1f}%) Exceeds ±{po.quantity_tolerance_pct:g}% PO Tolerance",
                            description=desc,
                            body=desc,
                            impact="Split shipment overage beyond agreed order quantity.",
                            fix="Approve as authorized split shipment variance or request supplier re-issue at permitted balance.",
                            source_doc_a="Buyer PO",
                            source_field_a=f"line_items[{idx}].quantity",
                            source_val_a=f"{po_item.quantity:g} {po_item.uom}",
                            source_doc_b="Commercial Invoice",
                            source_field_b=f"line_items[{idx}].quantity",
                            source_val_b=f"{inv_item.quantity:g} {inv_item.uom}",
                            highlight_cells=[f"po-open{inv_item.line_no}", f"inv-qty{inv_item.line_no}"],
                            cells=[f"po-open{inv_item.line_no}", f"inv-qty{inv_item.line_no}"],
                            locs=[["inv", "Commercial Invoice"], ["po", "Buyer PO"]],
                            recommended_actions=[
                                "Approve as authorized split/short shipment",
                                "Request supplier correction if quantity was mis-keyed",
                            ],
                        )
                    )

            # =========================================================================
            # TIER 3: COMPLIANCE ADVISORIES (HSN/HTS Root & Indian Export GST Route)
            # =========================================================================

            # 8. RULE-C01-WCO-HS6-DIVERGENCE (WARNING)
            inv_hsn_root = _clean_digits(inv_item.hsn_code)[:6]
            po_hts_root = _clean_digits(po_item.hts_code)[:6]
            if inv_hsn_root and po_hts_root and inv_hsn_root != po_hts_root:
                desc = (
                    f"Supplier Tally HSN ({inv_item.hsn_code}) and Buyer PO reference HTS ({po_item.hts_code}) "
                    f"differ at the international 6-digit WCO subheading level ({inv_hsn_root} vs {po_hts_root}). "
                    "Note: Buyer PO HTS is a commercial reference only—confirm binding US classification with licensed customs broker."
                )
                findings.append(
                    DiscrepancyItem(
                        id=f"DISC-HS-{inv_item.line_no}",
                        rule_id="RULE-C01-WCO-HS6-DIVERGENCE",
                        category="COMPLIANCE_ADVISORY",
                        severity="WARNING",
                        sev="warn",
                        title=f"Line #{inv_item.line_no}: 6-Digit HS Root Divergence (Invoice {inv_hsn_root} vs. PO {po_hts_root})",
                        description=desc,
                        body=desc,
                        impact="Potential duty rate differential or customs entry reclassification by CBP.",
                        fix="Escalate to licensed US customs broker to verify proper classification.",
                        source_doc_a="Buyer PO (Commercial Ref)",
                        source_field_a=f"line_items[{idx}].hts_code",
                        source_val_a=po_item.hts_code,
                        source_doc_b="Commercial Invoice (Supplier HSN)",
                        source_field_b=f"line_items[{idx}].hsn_code",
                        source_val_b=inv_item.hsn_code,
                        highlight_cells=[f"po-hts{inv_item.line_no}", f"inv-hsn{inv_item.line_no}"],
                        cells=[f"po-hts{inv_item.line_no}", f"inv-hsn{inv_item.line_no}"],
                        locs=[["inv", "Commercial Invoice"], ["po", "Buyer PO"]],
                        recommended_actions=[
                            "Escalate to Licensed US Customs Broker to confirm binding 10-digit HTSUS",
                            "Request Supplier HSN review if woven vs. knit chapter was misselected in Tally",
                        ],
                    )
                )

        # 8. RULE-V03-INCOTERM-VALUATION (WARNING / COMMERCIAL_VARIANCE)
        po_inco_raw = getattr(po, "incoterm", None) or getattr(po, "incoterms", None) or ""
        inv_inco_raw = getattr(inv, "incoterm", None) or getattr(inv, "incoterms", None) or ""
        po_inco_code = _extract_incoterm_code(po_inco_raw)
        inv_inco_code = _extract_incoterm_code(inv_inco_raw)

        if po_inco_code and inv_inco_code and po_inco_code != inv_inco_code:
            desc_inco = (
                f"Buyer PO specifies {po_inco_code} while Commercial Invoice declares {inv_inco_code}. "
                "Under 19 USC 1401a, US Transaction Value excludes international freight and insurance "
                "(CIF/CFR invoices require itemized freight/insurance deductions at entry). "
                "Confirm whether Incoterm change was commercially authorized."
            )
            findings.append(
                DiscrepancyItem(
                    id="DISC-INCOTERM",
                    rule_id="RULE-V03-INCOTERM-VALUATION",
                    category="COMMERCIAL_VARIANCE",
                    severity="WARNING",
                    sev="warn",
                    title="Incoterm Valuation Conflict (PO vs. Commercial Invoice)",
                    description=desc_inco,
                    body=desc_inco,
                    impact="US CBP transaction valuation audit risk under 19 USC 1401a for incorrect freight/insurance declaration.",
                    fix="Reconcile delivery terms: regenerate Commercial Invoice to match contract PO term, or execute an authorized PO Amendment.",
                    source_doc_a="Buyer PO",
                    source_field_a="incoterms",
                    source_val_a=str(po_inco_raw),
                    source_doc_b="Commercial Invoice",
                    source_field_b="incoterms",
                    source_val_b=str(inv_inco_raw),
                    highlight_cells=["po-incoterm", "inv-incoterm"],
                    cells=["po-incoterm", "inv-incoterm"],
                    locs=[["inv", "Commercial Invoice"], ["po", "Buyer PO"]],
                    recommended_actions=[
                        "Confirm agreed shipping Incoterm in buyer contract",
                        "Regenerate Commercial Invoice with PO Incoterm or execute formal PO Amendment",
                    ],
                )
            )

    # 9. RULE-C02-EXPORT-GST-ROUTE (WARNING)
    if inv.export_tax_route == "LUT_BOND" and not (inv.lut_arn_number and inv.lut_arn_number.strip()):
        desc_lut = (
            "Invoice indicates zero-rated export under Letter of Undertaking (IGST = $0.00), "
            "but the mandatory CBIC LUT Application Reference Number (ARN) is omitted from the header."
        )
        findings.append(
            DiscrepancyItem(
                id="DISC-LUT-ARN",
                rule_id="RULE-C02-EXPORT-GST-ROUTE",
                category="COMPLIANCE_ADVISORY",
                severity="WARNING",
                sev="warn",
                cells=["inv-lut"],
                locs=[["inv", "Commercial Invoice"]],
                body=desc_lut,
                impact="Without LUT/Bond ARN, Indian Customs and CBP will treat shipment as non-exempt, requiring IGST payment or bond.",
                fix="Supplier must obtain LUT from GST portal and print ARN on Tally Invoice header.",
                title="Missing LUT Bond ARN for Zero-Rated Export (LUT Route Selected)",
                description=desc_lut,
                source_doc_a="Commercial Invoice",
                source_field_a="export_tax_route",
                source_val_a="EXPORT UNDER LUT (IGST $0)",
                source_doc_b="Commercial Invoice",
                source_field_b="lut_arn_number",
                source_val_b="MISSING",
                highlight_cells=["inv-lut"],
                recommended_actions=[
                    "Add valid FY LUT ARN reference on Tally Invoice header",
                    "Or confirm shipment is exported under IGST Payment route",
                ],
            )
        )

    # 10. RULE-C03-CBP-VAGUE-CARGO-DESC (WARNING)
    for idx, inv_item in enumerate(inv.line_items):
        raw_desc = (inv_item.description or "").strip()
        cleaned_desc = raw_desc.lower()
        if len(raw_desc) < 10 or cleaned_desc in CBP_VAGUE_DESCRIPTIONS:
            desc_vague = (
                f"Commercial Invoice line #{inv_item.line_no} cargo description '{raw_desc}' is too generic. "
                "Invoice description serves as the pre-shipment proxy for Bill of Lading / ACE Cargo Manifest screening. "
                "Under CBP ACE Cargo Messaging rules (updated Sept 2025), generic commodity descriptions risk automated manifest rejection prior to vessel loading. "
                "Specific product identity, construction, and material composition are required for 10-digit HTSUS confirmation."
            )
            findings.append(
                DiscrepancyItem(
                    id=f"DISC-DESC-VAGUE-{inv_item.line_no}",
                    rule_id="RULE-C03-CBP-VAGUE-CARGO-DESC",
                    category="COMPLIANCE_ADVISORY",
                    severity="WARNING",
                    sev="warn",
                    title=f"Line #{inv_item.line_no}: Vague CBP Commodity Description",
                    description=desc_vague,
                    body=desc_vague,
                    impact="CBP ACE manifest rejection or mandatory physical container hold under 19 CFR 4.7a generic cargo rules.",
                    fix="Amend cargo description on invoice to include specific material composition (e.g. '100% Combed Cotton Knitted T-Shirt').",
                    source_doc_a="Commercial Invoice",
                    source_field_a=f"line_items[{idx}].description",
                    source_val_a=raw_desc,
                    source_doc_b=f"CBP ACE Cargo Manifest Rule ({CBP_VAGUE_DESCRIPTIONS_VERSION})",
                    source_field_b="acceptable_description_rule",
                    source_val_b="Requires specific product type + material",
                    highlight_cells=[f"inv-desc{inv_item.line_no}"],
                    cells=[f"inv-desc{inv_item.line_no}"],
                    locs=[["inv", "Commercial Invoice"]],
                    recommended_actions=[
                        "Update Tally invoice description with precise fiber composition and garment type",
                        "Align description with Buyer PO product specification",
                    ],
                )
            )

    # Compile Passed Checks
    passed_checks: List[str] = []
    if inv.total_cartons_declared == pl.total_cartons:
        passed_checks.append(
            f"Carton parity verified: {inv.total_cartons_declared} CTNS on Invoice equals Packing List"
        )
    if pl.total_net_weight_kg < pl.total_gross_weight_kg:
        passed_checks.append(
            f"Weight physics verified: Net ({pl.total_net_weight_kg:.1f} kg) < Gross ({pl.total_gross_weight_kg:.1f} kg)"
        )
    if po:
        passed_checks.append("Currency USD consistent across all documents")
        passed_checks.append(f"Incoterms {po.incoterms} agreed on Buyer PO")
        passed_checks.append("Port of loading INMUN1 matches shipping route")

    return findings, passed_checks


def run_deterministic_audit(
    packet: ShipmentPacket,
    session_token: str = "",
    revision_number: int = 1,
) -> AuditResponse:
    """
    Backwards-compatible convenience function combining linting and serialization.
    """
    from app.services.serializer import build_audit_response

    findings, passed_checks = run_deterministic_lint(packet)
    return build_audit_response(
        packet=packet,
        findings=findings,
        passed_checks=passed_checks,
        session_token=session_token,
        revision_number=revision_number,
    )

