"""
sessions.py — In-memory Magic Link Session Store for PortGate.

MAGIC_LINK_SESSIONS holds one entry per magic-link token. It is seeded on
module import with a default token ("shreeji-po-9921") so smoke tests
and live demos work immediately after server restart.

Each session dict shape:
    {
        "token":             str,
        "shipment_id":       str,
        "corridor":          str,
        "exporter_name":     str,
        "buyer_po":          models.BuyerPO,          # Pydantic model
        "latest_audit":      Optional[models.AuditResponse],
        "created_at":        str  (ISO-8601),
        "submission_status": Literal[...],
    }
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict

import models as m

# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------
MAGIC_LINK_SESSIONS: Dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Default seed token — "shreeji-po-9921"
# ---------------------------------------------------------------------------

def _seed_default_session() -> None:
    """Seed MAGIC_LINK_SESSIONS with the canonical Shreeji demo PO."""
    token = "shreeji-po-9921"

    buyer_po = m.BuyerPO(
        po_number="PO-PCR-45012",
        buyer_name="Pacific Coast Retail Group LLC",
        po_date="2026-09-01",
        incoterms="FOB MUNDRA",
        quantity_tolerance_pct=5.0,
        total_po_value_usd=36_200.00,
        line_items=[
            m.POLineItem(
                line_no=1,
                sku="PCR-M-TEE-NVY-L",
                description="Men's 100% Combed Cotton Crewneck Tee (Knitted)",
                hts_code="6109.10.0012",
                quantity=2000,
                uom="PCS",
                unit_price_usd=4.50,
                total_usd=9000.00,
            ),
            m.POLineItem(
                line_no=2,
                sku="PCR-W-POLO-WHT-M",
                description="Women's Pique Polo Shirt",
                hts_code="6106.10.0010",
                quantity=2000,
                uom="PCS",
                unit_price_usd=6.00,
                total_usd=12_000.00,
            ),
            m.POLineItem(
                line_no=3,
                sku="PCR-H-HOOD-GRY-XL",
                description="Heavyweight Fleece Pullover Hoodie",
                hts_code="6110.20.2079",
                quantity=1000,
                uom="PCS",
                unit_price_usd=11.00,
                total_usd=11_000.00,
            ),
            m.POLineItem(
                line_no=4,
                sku="PCR-K-POLO-BLU-S",
                description="Kids' Cotton Pique Polo Shirt (Blue)",
                hts_code="6112.11.0010",
                quantity=500,
                uom="PCS",
                unit_price_usd=8.50,
                total_usd=4_250.00,
            ),
        ],
    )

    MAGIC_LINK_SESSIONS[token] = {
        "token": token,
        "shipment_id": "SHP-2026-8841",
        "corridor": "IN-MUN → US-LAX",
        "exporter_name": "Shreeji Textiles & Apparel Pvt. Ltd.",
        "buyer_po": buyer_po,
        "latest_audit": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "submission_status": "AWAITING_SUPPLIER_UPLOAD",
    }


# Run seed on import
_seed_default_session()

