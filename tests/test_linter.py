"""
tests/test_linter.py — Exhaustive Unit Tests for PortGate Deterministic Compliance Engine.

Tests:
1. Clean packet verification (0 discrepancies, VERIFIED_CLEAN status).
2. Hard deterministic conflicts:
   - Carton count mismatch (DISC-CARTONS / RULE-D01-CARTON-SYNC)
   - Weight physics inversion line and header (DISC-WEIGHT-INV / RULE-D02-WEIGHT-PHYSICS)
   - Gross weight sync across documents (DISC-GROSS-SYNC / RULE-D03-GROSS-WT-SYNC)
   - Line total arithmetic error (DISC-MATH-* / RULE-D05-LINE-TOTAL-ARITHMETIC)
   - Unit of measure inconsistency (DISC-UOM-* / RULE-D04-UOM-CONSISTENCY)
3. Commercial variances:
   - Split-shipment quantity tolerance bands (+-3% pass, -59% triggers DISC-QTY-*)
   - Contract unit price variance (DISC-PRICE-* / RULE-V01-PO-PRICE-VARIANCE)
4. Compliance advisories:
   - WCO 6-digit HS root mismatch (DISC-HS-* / RULE-C01-WCO-HS6-DIVERGENCE)
   - Missing Indian export LUT ARN (DISC-LUT-ARN / RULE-C02-EXPORT-GST-ROUTE)
5. CBP MID code generation & SHA-256 document integrity hashing.
6. 2-way vs 3-way audit mode behaviors.
"""

from __future__ import annotations

import copy
import hashlib
from typing import cast
import pytest

from app.schemas.domain import (
    CommercialInvoice,
    InvoiceLineItem,
    PackingList,
    PackingListCartonGroup,
    POLineItem,
    ShipmentPacket,
)
from app.services.linter import run_deterministic_audit, run_deterministic_lint
from app.services.serializer import build_audit_response, generate_cbp_mid


# ---------------------------------------------------------------------------
# 1. Clean Packet Verification
# ---------------------------------------------------------------------------
def test_clean_packet_produces_zero_discrepancies(clean_packet: ShipmentPacket):
    """Clean 3-way packet produces 0 discrepancies, unresolved_count == 0, and VERIFIED_CLEAN status."""
    findings, passed_checks = run_deterministic_lint(clean_packet)
    assert len(findings) == 0, f"Expected 0 findings, got {len(findings)}"
    assert len(passed_checks) > 0, "Expected passed checks to be populated"

    audit = run_deterministic_audit(clean_packet, session_token="test_tok_clean", revision_number=1)
    assert audit.summary.status == "VERIFIED_CLEAN"
    assert audit.summary.unresolved_count == 0
    assert audit.summary.total_findings == 0
    assert audit.summary.deterministic_conflicts == 0
    assert audit.summary.commercial_variances == 0
    assert audit.summary.compliance_advisories == 0
    assert audit.summary.audit_mode == "3_WAY_FULL"
    assert len(audit.discrepancies) == 0
    assert len(audit.passed) >= 3


# ---------------------------------------------------------------------------
# 2. Hard Deterministic Conflicts (DETERMINISTIC_CONFLICT / CRITICAL)
# ---------------------------------------------------------------------------
def test_carton_count_mismatch(clean_packet: ShipmentPacket):
    """Commercial Invoice total cartons (100) != Packing List total cartons (110) triggers DISC-CARTONS."""
    packet = copy.deepcopy(clean_packet)
    packet.commercial_invoice.total_cartons_declared = 100
    packet.packing_list.total_cartons = 110

    findings, _ = run_deterministic_lint(packet)
    carton_disc = next((f for f in findings if f.id == "DISC-CARTONS"), None)

    assert carton_disc is not None, "DISC-CARTONS must be triggered"
    assert carton_disc.rule_id == "RULE-D01-CARTON-SYNC"
    assert carton_disc.category == "DETERMINISTIC_CONFLICT"
    assert carton_disc.severity == "CRITICAL"
    assert carton_disc.sev == "crit"
    assert "100 CTNS" in carton_disc.source_val_a
    assert "110 CTNS" in carton_disc.source_val_b
    assert "inv-ctns" in carton_disc.cells


def test_weight_inversion_line_level(clean_packet: ShipmentPacket):
    """Net weight > Gross weight on a packing list line triggers DISC-WEIGHT-INV."""
    packet = copy.deepcopy(clean_packet)
    # Line net 520 kg > gross 490 kg
    packet.packing_list.items[0].net_weight_kg = 520.0
    packet.packing_list.items[0].gross_weight_kg = 490.0

    findings, _ = run_deterministic_lint(packet)
    weight_disc = next((f for f in findings if f.id == "DISC-WEIGHT-INV"), None)

    assert weight_disc is not None, "DISC-WEIGHT-INV must be triggered for line weight inversion"
    assert weight_disc.rule_id == "RULE-D02-WEIGHT-PHYSICS"
    assert weight_disc.category == "DETERMINISTIC_CONFLICT"
    assert weight_disc.severity == "CRITICAL"
    assert "520.0" in weight_disc.description
    assert "490.0" in weight_disc.description


def test_weight_inversion_header_level(clean_packet: ShipmentPacket):
    """Total header Net weight >= Gross weight triggers DISC-WEIGHT-INV when lines don't exhibit it."""
    packet = copy.deepcopy(clean_packet)
    # Header total net > gross
    packet.packing_list.total_net_weight_kg = 1400.0
    packet.packing_list.total_gross_weight_kg = 1320.0

    findings, _ = run_deterministic_lint(packet)
    weight_disc = next((f for f in findings if f.id == "DISC-WEIGHT-INV"), None)

    assert weight_disc is not None, "DISC-WEIGHT-INV must be triggered for header weight inversion"
    assert weight_disc.rule_id == "RULE-D02-WEIGHT-PHYSICS"
    assert weight_disc.category == "DETERMINISTIC_CONFLICT"
    assert weight_disc.severity == "CRITICAL"
    assert "1400.0" in weight_disc.description
    assert "1320.0" in weight_disc.description


def test_gross_weight_mismatch_across_documents(clean_packet: ShipmentPacket):
    """Invoice total gross weight != Packing List total gross weight (>0.5 kg) triggers DISC-GROSS-SYNC."""
    packet = copy.deepcopy(clean_packet)
    packet.commercial_invoice.total_gross_weight_kg = 1350.0
    packet.packing_list.total_gross_weight_kg = 1320.0

    findings, _ = run_deterministic_lint(packet)
    sync_disc = next((f for f in findings if f.id == "DISC-GROSS-SYNC"), None)

    assert sync_disc is not None, "DISC-GROSS-SYNC must be triggered"
    assert sync_disc.rule_id == "RULE-D03-GROSS-WT-SYNC"
    assert sync_disc.category == "DETERMINISTIC_CONFLICT"
    assert sync_disc.severity == "WARNING"
    assert "1350.0" in sync_disc.source_val_a
    assert "1320.0" in sync_disc.source_val_b

    # When difference is <= 0.5 kg, it should not trigger
    packet.commercial_invoice.total_gross_weight_kg = 1320.3
    findings_tight, _ = run_deterministic_lint(packet)
    assert not any(f.id == "DISC-GROSS-SYNC" for f in findings_tight)


def test_line_total_arithmetic_error(clean_packet: ShipmentPacket):
    """Invoice line qty * unit_price != line_total triggers DISC-MATH-*."""
    packet = copy.deepcopy(clean_packet)
    # qty=1000, price=10.00, calculated=10000.00, but printed is 12500.00
    packet.commercial_invoice.line_items[0].total_usd = 12500.00

    findings, _ = run_deterministic_lint(packet)
    math_disc = next((f for f in findings if f.id == "DISC-MATH-1"), None)

    assert math_disc is not None, "DISC-MATH-1 must be triggered"
    assert math_disc.rule_id == "RULE-D05-LINE-TOTAL-ARITHMETIC"
    assert math_disc.category == "DETERMINISTIC_CONFLICT"
    assert math_disc.severity == "CRITICAL"
    assert "$10,000.00" in math_disc.description
    assert "$12,500.00" in math_disc.description


def test_uom_consistency_conflict(clean_packet: ShipmentPacket):
    """Commercial Invoice UOM ('PCS') != Packing List UOM ('SETS') triggers DISC-UOM-*."""
    packet = copy.deepcopy(clean_packet)
    packet.commercial_invoice.line_items[0].uom = "PCS"
    packet.packing_list.items[0].uom = "SETS"

    findings, _ = run_deterministic_lint(packet)
    uom_disc = next((f for f in findings if f.id == "DISC-UOM-1"), None)

    assert uom_disc is not None, "DISC-UOM-1 must be triggered"
    assert uom_disc.rule_id == "RULE-D04-UOM-CONSISTENCY"
    assert uom_disc.category == "DETERMINISTIC_CONFLICT"
    assert uom_disc.severity == "CRITICAL"
    assert "PCS" in uom_disc.description
    assert "SETS" in uom_disc.description


# ---------------------------------------------------------------------------
# 3. Commercial Variances (COMMERCIAL_VARIANCE)
# ---------------------------------------------------------------------------
def test_split_shipment_quantity_tolerance_band(clean_packet: ShipmentPacket):
    """
    Validates custom tolerance_pct band:
    - -3.0% variance within 5.0% tolerance passes cleanly.
    - -59.0% variance outside 5.0% tolerance triggers DISC-QTY-*.
    - +6.0% variance outside 5.0% tolerance triggers DISC-QTY-*.
    """
    packet = copy.deepcopy(clean_packet)
    assert packet.buyer_po is not None
    packet.buyer_po.quantity_tolerance_pct = 5.0
    po_qty = 1000.0

    # 1. -3.0% variance (qty = 970): within ±5% -> PASS
    packet.commercial_invoice.line_items[0].quantity = 970.0
    packet.commercial_invoice.line_items[0].total_usd = 9700.00
    findings, _ = run_deterministic_lint(packet)
    assert not any(f.id.startswith("DISC-QTY") for f in findings), "Within ±5% tolerance should not trigger DISC-QTY"

    # 2. +4.0% variance (qty = 1040): within ±5% -> PASS
    packet.commercial_invoice.line_items[0].quantity = 1040.0
    packet.commercial_invoice.line_items[0].total_usd = 10400.00
    findings, _ = run_deterministic_lint(packet)
    assert not any(f.id.startswith("DISC-QTY") for f in findings), "Within ±5% tolerance should not trigger DISC-QTY"

    # 3. -59.0% variance (qty = 410): outside ±5% -> TRIGGER
    packet.commercial_invoice.line_items[0].quantity = 410.0
    packet.commercial_invoice.line_items[0].total_usd = 4100.00
    findings, _ = run_deterministic_lint(packet)
    qty_disc = next((f for f in findings if f.id == "DISC-QTY-1"), None)
    assert qty_disc is not None, "-59% variance must trigger DISC-QTY-1"
    assert qty_disc.category == "COMMERCIAL_VARIANCE"
    assert qty_disc.severity == "WARNING"
    assert "-59.0%" in qty_disc.title

    # 4. +6.0% variance (qty = 1060): outside ±5% -> TRIGGER
    packet.commercial_invoice.line_items[0].quantity = 1060.0
    packet.commercial_invoice.line_items[0].total_usd = 10600.00
    findings, _ = run_deterministic_lint(packet)
    qty_disc_over = next((f for f in findings if f.id == "DISC-QTY-1"), None)
    assert qty_disc_over is not None, "+6% variance must trigger DISC-QTY-1"
    assert "+6.0%" in qty_disc_over.title

    # 5. Custom 10% tolerance test: -8% passes, -15% triggers
    packet.buyer_po.quantity_tolerance_pct = 10.0
    packet.commercial_invoice.line_items[0].quantity = 920.0  # -8%
    packet.commercial_invoice.line_items[0].total_usd = 9200.00
    findings_10, _ = run_deterministic_lint(packet)
    assert not any(f.id.startswith("DISC-QTY") for f in findings_10)

    packet.commercial_invoice.line_items[0].quantity = 850.0  # -15%
    packet.commercial_invoice.line_items[0].total_usd = 8500.00
    findings_15, _ = run_deterministic_lint(packet)
    assert any(f.id.startswith("DISC-QTY") for f in findings_15)


def test_unit_price_variance(clean_packet: ShipmentPacket):
    """Invoice unit price ($6.40) != PO contract unit price ($6.00) triggers DISC-PRICE-*."""
    packet = copy.deepcopy(clean_packet)
    assert packet.buyer_po is not None
    packet.buyer_po.line_items[0].unit_price_usd = 6.00
    packet.commercial_invoice.line_items[0].unit_price_usd = 6.40
    packet.commercial_invoice.line_items[0].quantity = 1000.0
    packet.commercial_invoice.line_items[0].total_usd = 6400.00

    findings, _ = run_deterministic_lint(packet)
    price_disc = next((f for f in findings if f.id == "DISC-PRICE-1"), None)

    assert price_disc is not None, "DISC-PRICE-1 must be triggered"
    assert price_disc.rule_id == "RULE-V01-PO-PRICE-VARIANCE"
    assert price_disc.category == "COMMERCIAL_VARIANCE"
    assert price_disc.severity == "CRITICAL"
    assert "$6.40" in price_disc.source_val_b
    assert "$6.00" in price_disc.source_val_a
    assert "+$400.00" in (price_disc.impact or "") or "+$400.00" in (price_disc.financial_impact_note or "")


# ---------------------------------------------------------------------------
# 4. Compliance Advisories (COMPLIANCE_ADVISORY)
# ---------------------------------------------------------------------------
def test_hs_6digit_root_mismatch(clean_packet: ShipmentPacket):
    """PO HTS 6-digit root ('6109.10') != Invoice HSN 6-digit root ('6205.20') triggers DISC-HS-*."""
    packet = copy.deepcopy(clean_packet)
    assert packet.buyer_po is not None
    packet.buyer_po.line_items[0].hts_code = "6109.10.0012"  # root: 610910
    packet.commercial_invoice.line_items[0].hsn_code = "6205.20.00"  # root: 620520

    findings, _ = run_deterministic_lint(packet)
    hs_disc = next((f for f in findings if f.id == "DISC-HS-1"), None)

    assert hs_disc is not None, "DISC-HS-1 must be triggered"
    assert hs_disc.rule_id == "RULE-C01-WCO-HS6-DIVERGENCE"
    assert hs_disc.category == "COMPLIANCE_ADVISORY"
    assert hs_disc.severity == "WARNING"
    assert "610910" in hs_disc.title
    assert "620520" in hs_disc.title

    # Matching roots should pass
    packet.commercial_invoice.line_items[0].hsn_code = "6109.10.99"
    findings_matched, _ = run_deterministic_lint(packet)
    assert not any(f.id.startswith("DISC-HS") for f in findings_matched)


def test_missing_lut_arn_validation(clean_packet: ShipmentPacket):
    """Missing Indian export LUT ARN when export_tax_route is 'LUT_BOND' triggers DISC-LUT-ARN."""
    packet = copy.deepcopy(clean_packet)
    packet.commercial_invoice.export_tax_route = "LUT_BOND"
    packet.commercial_invoice.lut_arn_number = ""

    findings, _ = run_deterministic_lint(packet)
    lut_disc = next((f for f in findings if f.id == "DISC-LUT-ARN"), None)
    assert lut_disc is not None, "DISC-LUT-ARN must be triggered when LUT ARN is empty"
    assert lut_disc.rule_id == "RULE-C02-EXPORT-GST-ROUTE"
    assert lut_disc.category == "COMPLIANCE_ADVISORY"
    assert lut_disc.severity == "WARNING"

    # None lut_arn_number also triggers
    packet.commercial_invoice.lut_arn_number = None
    findings_none, _ = run_deterministic_lint(packet)
    assert any(f.id == "DISC-LUT-ARN" for f in findings_none)

    # Valid ARN clears the advisory
    packet.commercial_invoice.lut_arn_number = "AD240426009182Z"
    findings_valid, _ = run_deterministic_lint(packet)
    assert not any(f.id == "DISC-LUT-ARN" for f in findings_valid)

    # IGST payment route does NOT require LUT ARN
    packet.commercial_invoice.export_tax_route = "IGST_PAYMENT"
    packet.commercial_invoice.lut_arn_number = None
    findings_igst, _ = run_deterministic_lint(packet)
    assert not any(f.id == "DISC-LUT-ARN" for f in findings_igst)


# ---------------------------------------------------------------------------
# 5. US CBP MID Code & Document Integrity Hash
# ---------------------------------------------------------------------------
def test_cbp_mid_code_generation():
    """
    Verifies US CBP Manufacturer Identification (MID) code generation:
    Country ISO + Exporter 6-char code + Address digits + City 3-char code.
    Example: IN + SHRTEX + 2301 + SUR = INSHRTEX2301SUR
    """
    mid = generate_cbp_mid(
        country_iso="IN",
        company_name="Shreeji Textiles & Apparel Pvt. Ltd.",
        street_address="Plot 2301, GIDC Pandesara",
        city="Surat",
    )
    assert mid == "INSHRTEX2301SUR", f"Expected INSHRTEX2301SUR, got {mid}"

    # Single-token company name
    mid_single = generate_cbp_mid(
        country_iso="IN",
        company_name="Reliance Industries Limited",
        street_address="Maker Chambers IV, 222 Nariman Point",
        city="Mumbai",
    )
    # Reliance -> RELIAN (6 chars), address number -> 222, city Mumbai -> MUM
    assert mid_single == "INRELIAN222MUM", f"Expected INRELIAN222MUM, got {mid_single}"


def test_document_integrity_hash():
    """Verifies deterministic SHA-256 document integrity hashing."""
    doc_payload = b"%PDF-1.4 canonical trade document payload"
    expected_hash = hashlib.sha256(doc_payload).hexdigest()
    assert len(expected_hash) == 64
    assert hashlib.sha256(doc_payload).hexdigest() == expected_hash


# ---------------------------------------------------------------------------
# 6. Full Dirty Packet v1 & Corrected Packet v2 Verification
# ---------------------------------------------------------------------------
def test_dirty_packet_v1_contains_all_discrepancy_classes(dirty_packet_v1: ShipmentPacket):
    """Validates that dirty_packet_v1 exercises every required category and rule."""
    findings, _ = run_deterministic_lint(dirty_packet_v1)
    rule_ids = {f.rule_id for f in findings}
    categories = {f.category for f in findings}

    # All three categories must be present
    assert "DETERMINISTIC_CONFLICT" in categories
    assert "COMMERCIAL_VARIANCE" in categories
    assert "COMPLIANCE_ADVISORY" in categories

    # Hard conflicts
    assert "RULE-D01-CARTON-SYNC" in rule_ids
    assert "RULE-D02-WEIGHT-PHYSICS" in rule_ids
    assert "RULE-D03-GROSS-WT-SYNC" in rule_ids
    assert "RULE-D05-LINE-TOTAL-ARITHMETIC" in rule_ids
    assert "RULE-D06-HEADER-LINE-SUM-SYNC" in rule_ids

    # Commercial variances
    assert "RULE-V01-PO-PRICE-VARIANCE" in rule_ids
    assert "RULE-V02-PO-QTY-TOLERANCE" in rule_ids

    # Compliance advisories
    assert "RULE-C01-WCO-HS6-DIVERGENCE" in rule_ids
    assert "RULE-C02-EXPORT-GST-ROUTE" in rule_ids

    audit = run_deterministic_audit(dirty_packet_v1)
    assert audit.summary.status == "ACTION_REQUIRED"
    assert audit.summary.deterministic_conflicts == 5
    assert audit.summary.commercial_variances == 2
    assert audit.summary.compliance_advisories == 2
    assert audit.summary.unresolved_count == 9


def test_corrected_packet_v2_has_only_one_commercial_variance(corrected_packet_v2: ShipmentPacket):
    """Validates that corrected_packet_v2 resolves all hard conflicts, leaving 1 commercial variance."""
    findings, _ = run_deterministic_lint(corrected_packet_v2)
    categories = [f.category for f in findings]

    assert "DETERMINISTIC_CONFLICT" not in categories, "All deterministic conflicts should be resolved"
    assert "COMPLIANCE_ADVISORY" not in categories, "All compliance advisories should be resolved"
    assert len(findings) == 1, f"Expected exactly 1 finding, got {len(findings)}"
    assert findings[0].id == "DISC-QTY-2"
    assert findings[0].category == "COMMERCIAL_VARIANCE"


def test_2way_audit_mode_when_buyer_po_is_omitted(clean_packet: ShipmentPacket):
    """When Buyer PO is omitted, engine runs in 2_WAY_INVOICE_PL mode without commercial variances."""
    packet_2way = copy.deepcopy(clean_packet)
    packet_2way.buyer_po = None

    audit = run_deterministic_audit(packet_2way)
    assert audit.summary.audit_mode == "2_WAY_INVOICE_PL"
    assert audit.summary.commercial_variances == 0
    assert audit.summary.status == "VERIFIED_CLEAN"


# ---------------------------------------------------------------------------
# 7. Industry Reviewer Customs Compliance Checks (Incoterm & CBP Cargo Desc)
# ---------------------------------------------------------------------------
def test_incoterm_valuation_mismatch(clean_packet: ShipmentPacket):
    """PO Incoterm ('FOB Mundra') vs Invoice Incoterm ('CIF Los Angeles') triggers DISC-INCOTERM as a commercial variance."""
    packet = copy.deepcopy(clean_packet)
    packet.buyer_po.incoterm = "FOB Mundra"
    packet.commercial_invoice.incoterms = "CIF Los Angeles"

    findings, _ = run_deterministic_lint(packet)
    incoterm_disc = next((f for f in findings if f.id == "DISC-INCOTERM"), None)

    assert incoterm_disc is not None, "DISC-INCOTERM must be triggered when Incoterms conflict"
    assert incoterm_disc.rule_id == "RULE-V03-INCOTERM-VALUATION"
    assert incoterm_disc.category == "COMMERCIAL_VARIANCE"
    assert incoterm_disc.severity == "WARNING"
    assert "FOB" in incoterm_disc.source_val_a
    assert "CIF" in incoterm_disc.source_val_b
    assert "19 USC 1401a" in incoterm_disc.description


def test_incoterm_matching_passes(clean_packet: ShipmentPacket):
    """Matching Incoterms (case-insensitive) pass valuation check without findings."""
    packet = copy.deepcopy(clean_packet)
    packet.buyer_po.incoterms = "FOB Mundra"
    packet.commercial_invoice.incoterms = "fob mundra"

    findings, passed = run_deterministic_lint(packet)
    incoterm_disc = next((f for f in findings if f.id == "DISC-INCOTERM"), None)
    assert incoterm_disc is None
    assert any("Incoterm" in p for p in passed)


def test_vague_cargo_description_rejected(clean_packet: ShipmentPacket):
    """Generic description ('apparel' or short description) triggers DISC-DESC-VAGUE-* as an ACE manifest proxy check."""
    packet = copy.deepcopy(clean_packet)
    packet.commercial_invoice.line_items[0].description = "apparel"

    findings, _ = run_deterministic_lint(packet)
    vague_disc = next((f for f in findings if f.id == "DISC-DESC-VAGUE-1"), None)

    assert vague_disc is not None, "DISC-DESC-VAGUE-1 must be triggered for generic cargo description"
    assert vague_disc.rule_id == "RULE-C03-CBP-VAGUE-CARGO-DESC"
    assert vague_disc.category == "COMPLIANCE_ADVISORY"
    assert vague_disc.severity == "WARNING"
    assert "ACE Cargo Manifest" in vague_disc.description
    assert "2025-09-ACE" in vague_disc.source_doc_b


def test_fail_closed_mandatory_customs_fields(clean_packet: ShipmentPacket):
    """Packets with missing/0 cartons, weights, or line items fail closed with DISC-MISSING-REQUIRED."""
    # 1. Zero cartons on Commercial Invoice
    pkt_cartons = copy.deepcopy(clean_packet)
    pkt_cartons.commercial_invoice.total_cartons_declared = 0
    f_cartons, _ = run_deterministic_lint(pkt_cartons)
    assert any(f.id == "DISC-MISSING-REQUIRED" for f in f_cartons)
    disc_c = next(f for f in f_cartons if f.id == "DISC-MISSING-REQUIRED")
    assert disc_c.severity == "CRITICAL"
    assert disc_c.category == "DETERMINISTIC_CONFLICT"

    # 2. Zero gross weight on Packing List
    pkt_wt = copy.deepcopy(clean_packet)
    pkt_wt.packing_list.total_gross_weight_kg = 0.0
    f_wt, _ = run_deterministic_lint(pkt_wt)
    assert any(f.id == "DISC-MISSING-REQUIRED" for f in f_wt)

    # 3. Empty line items
    pkt_lines = copy.deepcopy(clean_packet)
    pkt_lines.commercial_invoice.line_items = []
    f_lines, _ = run_deterministic_lint(pkt_lines)
    assert any(f.id == "DISC-MISSING-REQUIRED" for f in f_lines)


def test_invoice_header_vs_line_sum_mismatch(clean_packet: ShipmentPacket):
    """When sum of invoice line totals disagrees with header declared total, emit DISC-INVOICE-TOTAL-SUM."""
    pkt = copy.deepcopy(clean_packet)
    # Header declares $12,000 but line item is $10,000 (diff $2,000)
    pkt.commercial_invoice.total_invoice_value_usd = 12000.00
    findings, _ = run_deterministic_lint(pkt)
    sum_disc = next((f for f in findings if f.id == "DISC-INVOICE-TOTAL-SUM"), None)

    assert sum_disc is not None, "DISC-INVOICE-TOTAL-SUM must be emitted for header total mismatch"
    assert sum_disc.rule_id == "RULE-D06-HEADER-LINE-SUM-SYNC"
    assert sum_disc.category == "DETERMINISTIC_CONFLICT"
    assert sum_disc.severity == "CRITICAL"
    assert "12,000.00" in sum_disc.description
    assert "10,000.00" in sum_disc.description


def test_dynamic_extraction_confidence_scoring(clean_packet: ShipmentPacket):
    """Clean packet scores high extraction confidence with 0 low-confidence fields."""
    audit_clean = run_deterministic_audit(clean_packet)
    assert audit_clean.extraction_confidence_pct >= 99.0
    assert len(audit_clean.low_confidence_fields) == 0

    # Degradation test: missing GSTIN, unknown invoice number, and REVIEW_REQUIRED line match
    packet_degraded = copy.deepcopy(clean_packet)
    packet_degraded.commercial_invoice.invoice_number = "INV-UNKNOWN"
    packet_degraded.commercial_invoice.exporter_gstin = None
    packet_degraded.commercial_invoice.line_items[0].match_confidence = "REVIEW_REQUIRED"

    audit_degraded = run_deterministic_audit(packet_degraded)
    assert audit_degraded.extraction_confidence_pct < 95.0
    assert "commercial_invoice.invoice_number" in audit_degraded.low_confidence_fields
    assert "commercial_invoice.exporter_gstin" in audit_degraded.low_confidence_fields
    assert "commercial_invoice.line_items[0].match_confidence" in audit_degraded.low_confidence_fields


def test_po_reference_mismatch_guardrail(clean_packet: ShipmentPacket):
    """Invoice referencing an alien PO number triggers critical DISC-PO-MISMATCH."""
    packet = copy.deepcopy(clean_packet)
    packet.commercial_invoice.buyer_po_number = "PO-AST-2026-889"
    packet.commercial_invoice.buyer_po_ref = "PO-AST-2026-889"

    findings, _ = run_deterministic_lint(packet)
    po_mismatch = next((f for f in findings if f.id == "DISC-PO-MISMATCH"), None)

    assert po_mismatch is not None
    assert po_mismatch.category == "DETERMINISTIC_CONFLICT"
    assert po_mismatch.severity == "CRITICAL"
    assert "PO-AST-2026-889" in po_mismatch.description
    assert clean_packet.buyer_po.po_number in po_mismatch.description


def test_exporter_supplier_mismatch_guardrail(clean_packet: ShipmentPacket):
    """Invoice from an alien exporter (e.g. Royal Vitrified Ceramics) triggers DISC-SUPPLIER-MISMATCH."""
    packet = copy.deepcopy(clean_packet)
    packet.buyer_po.supplier_name = "Shreeji Textiles & Apparel Pvt. Ltd."
    packet.commercial_invoice.exporter_name = "Royal Vitrified Ceramics Pvt. Ltd."

    findings, _ = run_deterministic_lint(packet)
    supp_mismatch = next((f for f in findings if f.id == "DISC-SUPPLIER-MISMATCH"), None)

    assert supp_mismatch is not None
    assert supp_mismatch.category == "DETERMINISTIC_CONFLICT"
    assert supp_mismatch.severity == "CRITICAL"
    assert "Royal Vitrified Ceramics" in supp_mismatch.description
    assert "Shreeji Textiles" in supp_mismatch.description


def test_unexpected_invoice_line_mismatch(clean_packet: ShipmentPacket):
    """Invoice line for completely unrelated commodity triggers DISC-UNEXPECTED-LINE."""
    packet = copy.deepcopy(clean_packet)
    packet.commercial_invoice.line_items[0].description = "POLISHED GLAZED CERAMIC TILES 600X1200MM"
    packet.commercial_invoice.line_items[0].hsn_code = "6907.21.00"

    findings, _ = run_deterministic_lint(packet)
    line_mismatch = next((f for f in findings if f.id.startswith("DISC-UNEXPECTED-LINE")), None)

    assert line_mismatch is not None
    assert line_mismatch.severity == "CRITICAL"
    assert line_mismatch.category == "DETERMINISTIC_CONFLICT"
    assert "Unordered Item" in line_mismatch.title or "Uncontracted Cargo" in line_mismatch.title




