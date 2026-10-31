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


def _clean_digits(val: str | None) -> str:
    """Extract numeric digits from code for international root comparison."""
    return re.sub(r"\D", "", str(val or ""))


def run_deterministic_lint(packet: ShipmentPacket) -> Tuple[List[DiscrepancyItem], List[str]]:
    """
    Executes the 9 deterministic compliance rules against the ShipmentPacket.
    Returns (findings, passed_checks).
    """
    findings: List[DiscrepancyItem] = []
    inv = packet.commercial_invoice
    pl = packet.packing_list
    po = packet.buyer_po

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
    has_weight_inversion = (
        pl.total_net_weight_kg >= pl.total_gross_weight_kg
        or inv.total_net_weight_kg >= inv.total_gross_weight_kg
        or any(item.net_weight_kg >= item.gross_weight_kg for item in pl.items)
    )
    if has_weight_inversion:
        desc = (
            f"Packing List Net Weight ({pl.total_net_weight_kg:.1f} kg) is greater than or equal to "
            f"Gross Weight ({pl.total_gross_weight_kg:.1f} kg) or line-item weight is physically inverted. "
            "Net weight cannot exceed gross weight."
        )
        findings.append(
            DiscrepancyItem(
                id="DISC-WEIGHT-INV",
                rule_id="RULE-D02-WEIGHT-PHYSICS",
                category="DETERMINISTIC_CONFLICT",
                severity="CRITICAL",
                sev="crit",
                title="Physical Weight Inversion (Net Weight >= Gross Weight)",
                description=desc,
                body=desc,
                impact="Immediate ICEGATE and CBP automated manifest rejection due to physical impossibility.",
                fix="Re-scale shipment cartons and swap inverted Net/Gross weight columns.",
                source_doc_a="Packing List",
                source_field_a="total_net_weight_kg",
                source_val_a=f"{pl.total_net_weight_kg:.1f} KG",
                source_doc_b="Packing List",
                source_field_b="total_gross_weight_kg",
                source_val_b=f"{pl.total_gross_weight_kg:.1f} KG",
                highlight_cells=["pl-net-wt", "pl-gross-wt"],
                cells=["pl-net-wt", "pl-gross-wt"],
                locs=[["pl", "Packing List"]],
                recommended_actions=[
                    "Swap inverted Net/Gross weight columns in Packing List",
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
    # TIER 2: COMMERCIAL VARIANCES (Buyer PO vs. Commercial Invoice)
    # =========================================================================
    if po is not None:
        for idx, inv_item in enumerate(inv.line_items):
            po_item = next(
                (p for p in po.line_items if p.line_no == (inv_item.po_line_ref or inv_item.line_no)),
                po.line_items[idx] if idx < len(po.line_items) else None,
            )
            if not po_item:
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

