# test_linter.py
import json
from models import (
    ShipmentPacket,
    BuyerPO,
    POLineItem,
    CommercialInvoice,
    InvoiceLineItem,
    PackingList,
    PackingListCartonGroup,
)
from linter import run_deterministic_audit


def build_test_packet(include_po: bool = True, tax_route: str = "LUT_BOND") -> ShipmentPacket:
    po = (
        BuyerPO(
            po_number="PO-9921",
            buyer_name="Pacific Coast Retail Group",
            po_date="2026-09-15",
            quantity_tolerance_pct=5.0,
            total_po_value_usd=10000.0,
            line_items=[
                POLineItem(
                    line_no=1,
                    sku="BED-KING-100",
                    description="100% Cotton Percale Sheet Set - King",
                    hts_code="6302.21.5020",  # 6-digit root: 630221
                    quantity=1000,
                    uom="SETS",
                    unit_price_usd=10.00,
                    total_usd=10000.0,
                )
            ],
        )
        if include_po
        else None
    )

    invoice = CommercialInvoice(
        invoice_number="INV-2026-441",
        invoice_date="2026-10-30",
        exporter_name="Shreeji Textiles Pvt Ltd",
        exporter_gstin="24AABCS1429B1Z",
        export_tax_route=tax_route,  # "LUT_BOND" or "IGST_PAYMENT"
        lut_arn_number=None,         # Deliberately missing to test Rule C2
        total_cartons_declared=100,  # Conflicts with PL (95 cartons)
        total_net_weight_kg=1200.0,
        total_gross_weight_kg=1350.0,
        total_invoice_value_usd=10500.0,
        line_items=[
            InvoiceLineItem(
                line_no=1,
                po_line_ref=1,
                description="COTTON BEDSHEET KING",
                hsn_code="6205.20.00",  # Deliberate 6-digit root clash (620520 vs 630221)
                quantity=1000,
                uom="PCS",              # UOM conflict vs PL ("SETS")
                unit_price_usd=10.50,   # +$0.50 (+5%) price variance vs PO ($10.00)
                total_usd=10500.0,
            )
        ],
    )

    packing_list = PackingList(
        pl_number="PL-884",
        invoice_ref="INV-2026-441",
        total_cartons=95,              # Conflicts with Invoice (100 cartons)
        total_quantity=1000,
        total_net_weight_kg=1400.0,    # Physical Weight Inversion (Net 1400 > Gross 1320)
        total_gross_weight_kg=1320.0,
        items=[
            PackingListCartonGroup(
                line_no=1,
                description="COTTON BEDSHEET KING",
                carton_count=95,
                total_quantity=1000,
                uom="SETS",
                net_weight_kg=1400.0,
                gross_weight_kg=1320.0,
            )
        ],
    )

    return ShipmentPacket(
        shipment_id="SHP-2026-8841",
        buyer_po=po,
        commercial_invoice=invoice,
        packing_list=packing_list,
    )


if __name__ == "__main__":
    print("=== TEST 1: 3-WAY FULL AUDIT (PO + INVOICE + PL, LUT ROUTE) ===")
    res_3way = run_deterministic_audit(build_test_packet(include_po=True, tax_route="LUT_BOND"))
    print(json.dumps(res_3way.summary.model_dump(), indent=2))
    for d in res_3way.discrepancies:
        print(f"  [{d.category}] ({d.severity}) {d.rule_id}: {d.title}")

    assert res_3way.summary.status == "ACTION_REQUIRED"
    assert res_3way.summary.deterministic_conflicts == 4  # Cartons, Weight Inversion, Gross Sync, UOM
    assert res_3way.summary.commercial_variances == 1     # Price variance (+5%)
    assert res_3way.summary.compliance_advisories == 2    # HS6 root divergence + Missing LUT ARN

    print("\n=== TEST 2: 2-WAY AUDIT (NO PO UPLOADED, IGST PAYMENT ROUTE) ===")
    res_2way = run_deterministic_audit(build_test_packet(include_po=False, tax_route="IGST_PAYMENT"))
    print(json.dumps(res_2way.summary.model_dump(), indent=2))
    assert res_2way.summary.audit_mode == "2_WAY_INVOICE_PL"
    assert res_2way.summary.commercial_variances == 0
    assert res_2way.summary.compliance_advisories == 0    # LUT rule skipped when IGST_PAYMENT is used!
    print("\n[OK] All deterministic linter assertions passed!")