"""
tests/conftest.py — Global Pytest Fixtures for PortGate Backend Test Suite.

Provides:
- Complete isolation from external APIs (Google Gemini, Supabase).
- Autouse in-memory session store sandbox resetting before every test.
- Canonical mock shipment packets: clean_packet, dirty_packet_v1, corrected_packet_v2.
- FastAPI TestClient fixture with isolated environment.
"""

from __future__ import annotations

import io
from typing import Generator
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.domain import (
    BuyerPO,
    CommercialInvoice,
    InvoiceLineItem,
    PackingList,
    PackingListCartonGroup,
    POLineItem,
    ShipmentPacket,
)
from app.schemas.extraction import (
    ExtractedInvLine,
    ExtractedMetadata,
    ExtractedPLLine,
    ExtractedPOLine,
    ExtractedShipmentPacket,
)
from app.services.session_store import session_store


# ---------------------------------------------------------------------------
# 1. Zero External API Calls & In-Memory Isolation Fixture
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def isolate_session_store(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """
    Guarantees 100% deterministic, zero-network-dependency testing:
    - Forces Supabase off (session_store._supabase = None).
    - Disables writing to sessions_store.json on disk.
    - Wipes in-memory session dictionaries clean before and after every test function.
    """
    # Disable Supabase persistence
    monkeypatch.setattr(session_store, "_supabase", None)
    # Prevent disk persistence during unit and integration tests
    monkeypatch.setattr(session_store, "_save_to_disk", lambda: None)
    monkeypatch.setattr(session_store, "_load_from_disk", lambda: False)

    # Clean in-memory dictionaries before test execution
    session_store._sessions.clear()
    session_store._owners.clear()
    session_store._files.clear()

    yield

    # Clean up after test execution
    session_store._sessions.clear()
    session_store._owners.clear()
    session_store._files.clear()


# ---------------------------------------------------------------------------
# 2. Mock LLM / Gemini PDF Extraction Fixture
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def mock_gemini_pdf_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Intercepts calls to extract_shipment_packet across both routes.py and extractor.py
    so PDF upload routes run deterministically in milliseconds without network calls.
    """
    def _fake_extract(po_bytes: bytes, inv_bytes: bytes, pl_bytes: bytes, model: str | None = None) -> ExtractedShipmentPacket:
        # Check if caller uploaded custom bytes or dummy bytes
        return ExtractedShipmentPacket(
            metadata=ExtractedMetadata(
                po_number="PO-PCR-45012",
                invoice_no="SHR/EXP/26-27/0412",
                invoice_date="14-Oct-2026",
                packing_list_no="SHR/PL/0412",
                shipment_ref="SHP-2026-8841",
                exporter_name="Shreeji Textiles & Apparel Pvt. Ltd.",
                exporter_street="Plot 2301, GIDC Pandesara",
                exporter_city="Surat",
                exporter_state="Gujarat",
                exporter_postal="394221",
                exporter_country_iso="IN",
                exporter_gstin="24AABCS1429B1Z5",
                exporter_iec="5214098211",
                exporter_lut_arn="AD240426009182Z",
                export_tax_route="LUT_BOND",
                importer_name="Pacific Coast Retail Group LLC",
                importer_city_state="Los Angeles, CA",
                port_of_loading="INMUN1",
                port_of_discharge="USLAX",
                incoterm="FOB Mundra",
                currency="USD",
                po_incoterm="FOB Mundra",
                po_currency="USD",
                total_cartons_declared=100,
                total_cartons=110,
            ),
            po_lines=[
                ExtractedPOLine(
                    sku="PCR-M-TEE-NVY-L",
                    desc="Men's 100% Combed Cotton Crewneck Tee (Knitted)",
                    hts="6109.10.0012",
                    qty=2000,
                    prior=0,
                    open=2000,
                    price=4.50,
                ),
                ExtractedPOLine(
                    sku="PCR-W-POLO-WHT-M",
                    desc="Women's Pique Polo Shirt",
                    hts="6106.10.0010",
                    qty=5000,
                    prior=3000,
                    open=2000,
                    price=6.00,
                ),
            ],
            inv_lines=[
                ExtractedInvLine(
                    desc="100% COTTON MENS ROUND NECK T-SHIRT (KNITTED)",
                    hsn="6109.10.00",
                    qty=2000,
                    rate=4.50,
                    line_total=9000.00,
                    net_wt_kg=380.0,
                    gross_wt_kg=420.0,
                    ctns=50,
                ),
                ExtractedInvLine(
                    desc="LADIES COTTON PIQUE COLLAR T-SHIRT WHITE",
                    hsn="6106.10.00",
                    qty=2000,
                    rate=6.00,
                    line_total=12000.00,
                    net_wt_kg=410.0,
                    gross_wt_kg=450.0,
                    ctns=50,
                ),
            ],
            pl_lines=[
                ExtractedPLLine(
                    desc="Cartons 1-50: 100% COTTON MENS ROUND NECK T-SHIRT",
                    ctns=50,
                    net_wt_kg=380.0,
                    gross_wt_kg=420.0,
                ),
                ExtractedPLLine(
                    desc="Cartons 51-100: LADIES COTTON PIQUE COLLAR T-SHIRT",
                    ctns=50,
                    net_wt_kg=410.0,
                    gross_wt_kg=450.0,
                ),
            ],
        )

    monkeypatch.setattr("app.services.extractor.extract_shipment_packet", _fake_extract)
    monkeypatch.setattr("app.api.routes.extract_shipment_packet", _fake_extract)


# ---------------------------------------------------------------------------
# 3. TestClient Fixture
# ---------------------------------------------------------------------------
@pytest.fixture
def client() -> TestClient:
    """FastAPI TestClient instance connected to app with zero network dependencies."""
    return TestClient(app)


# ---------------------------------------------------------------------------
# 4. Canonical Test ShipmentPacket Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def clean_packet() -> ShipmentPacket:
    """
    A 3-document ShipmentPacket (Buyer PO, Commercial Invoice, Packing List)
    where all math, weights, cartons, HS codes, and quantities match 100%.
    Produces 0 discrepancies, unresolved_count == 0, and status 'VERIFIED_CLEAN'.
    """
    po = BuyerPO(
        po_number="PO-CLEAN-001",
        buyer_name="Pacific Coast Retail Group LLC",
        po_date="14-Oct-2026",
        incoterms="FOB Mundra",
        quantity_tolerance_pct=5.0,
        total_po_value_usd=10000.0,
        line_items=[
            POLineItem(
                line_no=1,
                sku="PCR-CLEAN-TEE",
                description="Men's 100% Combed Cotton Crewneck Tee (Knitted)",
                hts_code="6109.10.0012",
                quantity=1000.0,
                uom="PCS",
                unit_price_usd=10.00,
                total_usd=10000.00,
            )
        ],
    )

    inv = CommercialInvoice(
        invoice_number="INV-CLEAN-001",
        invoice_date="14-Oct-2026",
        exporter_name="Shreeji Textiles & Apparel Pvt. Ltd.",
        exporter_gstin="24AABCS1429B1Z5",
        export_tax_route="LUT_BOND",
        lut_arn_number="AD240426009182Z",
        buyer_po_ref="PO-CLEAN-001",
        total_cartons_declared=50,
        total_net_weight_kg=200.0,
        total_gross_weight_kg=220.0,
        total_invoice_value_usd=10000.00,
        line_items=[
            InvoiceLineItem(
                line_no=1,
                po_line_ref=1,
                description="Men's 100% Combed Cotton Crewneck Tee (Knitted)",
                hsn_code="6109.10.00",
                quantity=1000.0,
                uom="PCS",
                unit_price_usd=10.00,
                total_usd=10000.00,
                carton_count=50,
                net_weight_kg=200.0,
                gross_weight_kg=220.0,
            )
        ],
    )

    pl = PackingList(
        pl_number="PL-CLEAN-001",
        invoice_ref="INV-CLEAN-001",
        total_cartons=50,
        total_quantity=1000.0,
        total_net_weight_kg=200.0,
        total_gross_weight_kg=220.0,
        items=[
            PackingListCartonGroup(
                line_no=1,
                description="Men's 100% Combed Cotton Crewneck Tee (Knitted)",
                carton_count=50,
                total_quantity=1000.0,
                uom="PCS",
                net_weight_kg=200.0,
                gross_weight_kg=220.0,
            )
        ],
    )

    return ShipmentPacket(
        shipment_id="SHP-2026-CLEAN",
        corridor="INMUN1 -> USLAX",
        buyer_po=po,
        commercial_invoice=inv,
        packing_list=pl,
    )


@pytest.fixture
def dirty_packet_v1() -> ShipmentPacket:
    """
    A packet containing every discrepancy class:
    1. Carton count mismatch: Inv 100 != PL 110 (DISC-CARTONS, DETERMINISTIC_CONFLICT)
    2. Weight physics inversion: PL line 3 Net 520 > Gross 490 (DISC-WEIGHT-INV, DETERMINISTIC_CONFLICT)
    3. Gross weight sync: Inv gross 1350 != PL gross 1320 (DISC-GROSS-SYNC, DETERMINISTIC_CONFLICT)
    4. Line extended math error: Line 3 1000 * $11 != $12,500 printed (DISC-MATH-3, DETERMINISTIC_CONFLICT)
    5. Unit price variance: Line 2 $6.40 vs PO $6.00 (DISC-PRICE-2, COMMERCIAL_VARIANCE)
    6. Quantity variance outside tolerance: Line 2 820 vs PO 2000 (-59% vs +-5%) (DISC-QTY-2, COMMERCIAL_VARIANCE)
    7. 6-digit HS root mismatch: Line 1 PO 6109.10 vs Inv 6205.20 (DISC-HS-1, COMPLIANCE_ADVISORY)
    8. Missing LUT ARN: LUT_BOND route with empty ARN (DISC-LUT-ARN, COMPLIANCE_ADVISORY)
    """
    po = BuyerPO(
        po_number="PO-PCR-45012",
        buyer_name="Pacific Coast Retail Group LLC",
        po_date="14-Oct-2026",
        incoterms="FOB Mundra",
        quantity_tolerance_pct=5.0,
        total_po_value_usd=32000.0,
        line_items=[
            POLineItem(
                line_no=1,
                sku="PCR-M-TEE-NVY-L",
                description="Men's 100% Combed Cotton Crewneck Tee (Knitted)",
                hts_code="6109.10.0012",  # 6-digit root: 610910
                quantity=2000.0,
                uom="PCS",
                unit_price_usd=4.50,
                total_usd=9000.00,
            ),
            POLineItem(
                line_no=2,
                sku="PCR-W-POLO-WHT-M",
                description="Women's Pique Polo Shirt",
                hts_code="6106.10.0010",
                quantity=2000.0,
                uom="PCS",
                unit_price_usd=6.00,      # Contract rate: $6.00
                total_usd=12000.00,
            ),
            POLineItem(
                line_no=3,
                sku="PCR-H-HOOD-GRY-XL",
                description="Heavyweight Fleece Pullover Hoodie",
                hts_code="6110.20.2079",
                quantity=1000.0,
                uom="PCS",
                unit_price_usd=11.00,
                total_usd=11000.00,
            ),
        ],
    )

    inv = CommercialInvoice(
        invoice_number="SHR/EXP/26-27/0412",
        invoice_date="14-Oct-2026",
        exporter_name="Shreeji Textiles & Apparel Pvt. Ltd.",
        exporter_gstin="24AABCS1429B1Z5",
        export_tax_route="LUT_BOND",
        lut_arn_number="",                # Missing LUT ARN -> triggers DISC-LUT-ARN
        buyer_po_ref="PO-PCR-45012",
        total_cartons_declared=100,       # 100 vs PL 110 -> triggers DISC-CARTONS
        total_net_weight_kg=1200.0,
        total_gross_weight_kg=1350.0,     # Inv Gross 1350 vs PL Gross 1320 -> triggers DISC-GROSS-SYNC
        total_invoice_value_usd=34620.00,
        line_items=[
            InvoiceLineItem(
                line_no=1,
                po_line_ref=1,
                description="100% COTTON MENS ROUND NECK T-SHIRT (KNITTED)",
                hsn_code="6205.20.00",    # Root 620520 vs PO 610910 -> triggers DISC-HS-1
                quantity=2000.0,
                uom="PCS",
                unit_price_usd=4.50,
                total_usd=9000.00,
                carton_count=40,
                net_weight_kg=380.0,
                gross_weight_kg=420.0,
            ),
            InvoiceLineItem(
                line_no=2,
                po_line_ref=2,
                description="LADIES COTTON PIQUE COLLAR T-SHIRT WHITE",
                hsn_code="6106.10.00",
                quantity=820.0,           # 820 vs PO 2000 (-59%) -> triggers DISC-QTY-2
                unit_price_usd=6.40,      # $6.40 vs PO $6.00 -> triggers DISC-PRICE-2
                total_usd=5248.00,
                carton_count=30,
                net_weight_kg=300.0,
                gross_weight_kg=340.0,
            ),
            InvoiceLineItem(
                line_no=3,
                po_line_ref=3,
                description="HEAVYWEIGHT FLEECE PULLOVER HOODIE ASH GREY",
                hsn_code="6110.20.00",
                quantity=1000.0,
                unit_price_usd=11.00,
                total_usd=12500.00,       # Math error: 1000 * 11 != 12500 -> triggers DISC-MATH-3
                carton_count=30,
                net_weight_kg=520.0,
                gross_weight_kg=490.0,
            ),
        ],
    )

    pl = PackingList(
        pl_number="SHR/PL/0412",
        invoice_ref="SHR/EXP/26-27/0412",
        total_cartons=110,                # 110 vs Inv 100 -> triggers DISC-CARTONS
        total_quantity=3820.0,
        total_net_weight_kg=1200.0,
        total_gross_weight_kg=1320.0,     # Gross 1320 vs Inv 1350
        items=[
            PackingListCartonGroup(
                line_no=1,
                description="100% COTTON MENS ROUND NECK T-SHIRT",
                carton_count=40,
                total_quantity=2000.0,
                uom="PCS",
                net_weight_kg=380.0,
                gross_weight_kg=420.0,
            ),
            PackingListCartonGroup(
                line_no=2,
                description="LADIES COTTON PIQUE COLLAR T-SHIRT",
                carton_count=30,
                total_quantity=820.0,
                uom="PCS",
                net_weight_kg=300.0,
                gross_weight_kg=340.0,
            ),
            PackingListCartonGroup(
                line_no=3,
                description="HEAVYWEIGHT FLEECE PULLOVER HOODIE",
                carton_count=40,
                total_quantity=1000.0,
                uom="PCS",
                net_weight_kg=520.0,      # Net 520 > Gross 490 -> triggers DISC-WEIGHT-INV
                gross_weight_kg=490.0,
            ),
        ],
    )

    return ShipmentPacket(
        shipment_id="SHP-2026-DIRTY",
        corridor="INMUN1 -> USLAX",
        buyer_po=po,
        commercial_invoice=inv,
        packing_list=pl,
    )


@pytest.fixture
def corrected_packet_v2() -> ShipmentPacket:
    """
    Supplier's revised packet where all hard deterministic conflicts (DETERMINISTIC_CONFLICT)
    are fixed, leaving only 1 commercial variance (DISC-QTY-2, COMMERCIAL_VARIANCE).
    """
    po = BuyerPO(
        po_number="PO-PCR-45012",
        buyer_name="Pacific Coast Retail Group LLC",
        po_date="14-Oct-2026",
        incoterms="FOB Mundra",
        quantity_tolerance_pct=5.0,
        total_po_value_usd=32000.0,
        line_items=[
            POLineItem(
                line_no=1,
                sku="PCR-M-TEE-NVY-L",
                description="Men's 100% Combed Cotton Crewneck Tee (Knitted)",
                hts_code="6109.10.0012",
                quantity=2000.0,
                uom="PCS",
                unit_price_usd=4.50,
                total_usd=9000.00,
            ),
            POLineItem(
                line_no=2,
                sku="PCR-W-POLO-WHT-M",
                description="Women's Pique Polo Shirt",
                hts_code="6106.10.0010",
                quantity=2000.0,
                uom="PCS",
                unit_price_usd=6.00,
                total_usd=12000.00,
            ),
            POLineItem(
                line_no=3,
                sku="PCR-H-HOOD-GRY-XL",
                description="Heavyweight Fleece Pullover Hoodie",
                hts_code="6110.20.2079",
                quantity=1000.0,
                uom="PCS",
                unit_price_usd=11.00,
                total_usd=11000.00,
            ),
        ],
    )

    inv = CommercialInvoice(
        invoice_number="SHR/EXP/26-27/0412",
        invoice_date="14-Oct-2026",
        exporter_name="Shreeji Textiles & Apparel Pvt. Ltd.",
        exporter_gstin="24AABCS1429B1Z5",
        export_tax_route="LUT_BOND",
        lut_arn_number="AD240426009182Z", # Fixed: LUT ARN provided
        buyer_po_ref="PO-PCR-45012",
        total_cartons_declared=110,       # Fixed: 110 == PL 110
        total_net_weight_kg=1170.0,
        total_gross_weight_kg=1320.0,     # Fixed: matches PL gross 1320.0
        total_invoice_value_usd=24920.00,
        line_items=[
            InvoiceLineItem(
                line_no=1,
                po_line_ref=1,
                description="100% COTTON MENS ROUND NECK T-SHIRT (KNITTED)",
                hsn_code="6109.10.00",    # Fixed: 6109.10 matches PO 6109.10
                quantity=2000.0,
                uom="PCS",
                unit_price_usd=4.50,
                total_usd=9000.00,
                carton_count=40,
                net_weight_kg=380.0,
                gross_weight_kg=420.0,
            ),
            InvoiceLineItem(
                line_no=2,
                po_line_ref=2,
                description="LADIES COTTON PIQUE COLLAR T-SHIRT WHITE",
                hsn_code="6106.10.00",
                quantity=820.0,           # Retained: -59% variance outside tolerance (COMMERCIAL_VARIANCE)
                unit_price_usd=6.00,      # Fixed: Price matches PO $6.00
                total_usd=4920.00,
                carton_count=30,
                net_weight_kg=300.0,
                gross_weight_kg=340.0,
            ),
            InvoiceLineItem(
                line_no=3,
                po_line_ref=3,
                description="HEAVYWEIGHT FLEECE PULLOVER HOODIE ASH GREY",
                hsn_code="6110.20.00",
                quantity=1000.0,
                unit_price_usd=11.00,
                total_usd=11000.00,       # Fixed math: 1000 * 11.00 = 11000.00
                carton_count=40,
                net_weight_kg=490.0,
                gross_weight_kg=560.0,
            ),
        ],
    )

    pl = PackingList(
        pl_number="SHR/PL/0412",
        invoice_ref="SHR/EXP/26-27/0412",
        total_cartons=110,                # Fixed: matches Inv 110
        total_quantity=3820.0,
        total_net_weight_kg=1170.0,       # Net < Gross
        total_gross_weight_kg=1320.0,
        items=[
            PackingListCartonGroup(
                line_no=1,
                description="100% COTTON MENS ROUND NECK T-SHIRT",
                carton_count=40,
                total_quantity=2000.0,
                uom="PCS",
                net_weight_kg=380.0,
                gross_weight_kg=420.0,
            ),
            PackingListCartonGroup(
                line_no=2,
                description="LADIES COTTON PIQUE COLLAR T-SHIRT",
                carton_count=30,
                total_quantity=820.0,
                uom="PCS",
                net_weight_kg=300.0,
                gross_weight_kg=340.0,
            ),
            PackingListCartonGroup(
                line_no=3,
                description="HEAVYWEIGHT FLEECE PULLOVER HOODIE",
                carton_count=40,
                total_quantity=1000.0,
                uom="PCS",
                net_weight_kg=490.0,      # Fixed: Net 490 <= Gross 560
                gross_weight_kg=560.0,
            ),
        ],
    )

    return ShipmentPacket(
        shipment_id="SHP-2026-DIRTY",
        corridor="INMUN1 -> USLAX",
        buyer_po=po,
        commercial_invoice=inv,
        packing_list=pl,
    )

