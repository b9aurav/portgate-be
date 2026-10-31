"""
main_helpers.py — Helper utilities for PortGate backend.
"""

from __future__ import annotations

import copy
import models as m


def build_mock_revised_packet(packet: m.ShipmentPacket) -> m.ShipmentPacket:
    """
    Simulates a supplier correcting their Commercial Invoice and Packing List:
    - Resolves carton mismatch (aligns invoice cartons to packing list cartons)
    - Fixes weight physics inversion (net <= gross)
    - Standardizes UOM across documents
    - Corrects line total math arithmetic
    - Aligns HSN root to PO HTS root
    - Corrects unit prices to match agreed PO rates
    """
    revised = copy.deepcopy(packet)
    inv = revised.commercial_invoice
    pl = revised.packing_list
    po = revised.buyer_po

    # 1. Carton count sync: invoice matches packing list
    inv.total_cartons_declared = pl.total_cartons

    # 2. Weight physics fix: ensure net < gross
    if pl.total_net_weight_kg >= pl.total_gross_weight_kg:
        pl.total_net_weight_kg = round(pl.total_gross_weight_kg * 0.90, 1)

    inv.total_net_weight_kg = pl.total_net_weight_kg
    inv.total_gross_weight_kg = pl.total_gross_weight_kg

    for item in pl.items:
        if item.net_weight_kg >= item.gross_weight_kg:
            item.net_weight_kg = round(item.gross_weight_kg * 0.90, 1)

    # 3. Synchronize line items with PO (rates, UOM, HSN, line total math)
    if po:
        for idx, inv_item in enumerate(inv.line_items):
            po_item = next(
                (p for p in po.line_items if p.line_no == (inv_item.po_line_ref or inv_item.line_no)),
                po.line_items[idx] if idx < len(po.line_items) else None,
            )
            if po_item:
                # Correct unit price to PO agreed price
                inv_item.unit_price_usd = po_item.unit_price_usd
                # Correct UOM
                inv_item.uom = po_item.uom
                # Correct HSN to match PO HTS root
                hsn_root = po_item.hts_code.replace(".", "")[:6]
                inv_item.hsn_code = f"{hsn_root[:4]}.{hsn_root[4:6]}.00"
                # Correct line total math (quantity * unit_price_usd)
                inv_item.total_usd = round(inv_item.quantity * inv_item.unit_price_usd, 2)

    # Recalculate invoice total
    inv.total_invoice_value_usd = sum(l.total_usd for l in inv.line_items)

    # Ensure LUT ARN is valid
    if inv.export_tax_route == "LUT_BOND" and not inv.lut_arn_number:
        inv.lut_arn_number = "AD240426009182Z"

    return revised

