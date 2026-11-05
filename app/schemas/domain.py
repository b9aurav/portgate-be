from __future__ import annotations

from typing import List, Literal, Optional
from pydantic import BaseModel, Field

FindingCategory = Literal[
    "DETERMINISTIC_CONFLICT",  # Hard math / internal document conflict (e.g., Invoice vs PL cartons)
    "COMMERCIAL_VARIANCE",     # Commercial difference vs Buyer PO (e.g., unit price or split-shipment qty)
    "COMPLIANCE_ADVISORY",     # Conditional regulatory or classification check (e.g., HSN/HTS root or LUT route)
]

SeverityLevel = Literal["CRITICAL", "WARNING", "INFO"]

ResolutionState = Literal[
    "UNRESOLVED",
    "REVISION_REQUESTED",
    "ACCEPTED_EXCEPTION",
    "ESCALATED_TO_BROKER",
    "CLEARED_BY_REVISION",
    "CLEARED_IN_REVISION",
]

AuditOverallStatus = Literal[
    "ACTION_REQUIRED",
    "CLEARED_WITH_EXCEPTIONS",
    "VERIFIED_CLEAN",
]


class POLineItem(BaseModel):
    line_no: int
    sku: str
    description: str
    hts_code: str = Field(description="US HTS code on Buyer PO, e.g., 6302.21.5020")
    quantity: float
    uom: str = "PCS"
    unit_price_usd: float
    total_usd: float


class BuyerPO(BaseModel):
    po_number: str
    buyer_name: str
    supplier_name: Optional[str] = "Shreeji Textiles & Apparel Pvt. Ltd."
    vendor_name: Optional[str] = None
    supplier_address: Optional[str] = "Plot 2301, GIDC Pandesara, Surat, Gujarat, 394221, IN"
    po_date: str
    incoterms: str = "FOB MUNDRA"
    incoterm: Optional[str] = None
    currency: Optional[str] = "USD"
    quantity_tolerance_pct: float = 5.0
    line_items: List[POLineItem] = Field(default_factory=list)
    total_po_value_usd: float = 0.0


class InvoiceLineItem(BaseModel):
    line_no: int
    po_line_ref: Optional[int] = None
    description: str
    hsn_code: str = Field(description="Indian HSN code on Tally Invoice, e.g., 6302.21.00")
    quantity: float
    uom: str = "PCS"
    unit_price_usd: float
    total_usd: float
    match_confidence: Literal["HIGH", "MEDIUM", "REVIEW_REQUIRED"] = "HIGH"
    carton_count: Optional[int] = None
    net_weight_kg: Optional[float] = None
    gross_weight_kg: Optional[float] = None


class CommercialInvoice(BaseModel):
    invoice_number: str
    invoice_date: str
    exporter_name: str
    exporter_gstin: Optional[str] = None
    export_tax_route: Literal["LUT_BOND", "IGST_PAYMENT", "UNKNOWN"] = "LUT_BOND"
    lut_arn_number: Optional[str] = None
    igst_amount_usd: Optional[float] = 0.0
    buyer_po_ref: Optional[str] = None
    buyer_po_number: Optional[str] = None
    incoterms: Optional[str] = "FOB MUNDRA"
    incoterm: Optional[str] = None
    total_cartons_declared: int = 0
    total_net_weight_kg: float = 0.0
    total_gross_weight_kg: float = 0.0
    total_invoice_value_usd: float = 0.0
    line_items: List[InvoiceLineItem] = Field(default_factory=list)


class PackingListCartonGroup(BaseModel):
    line_no: int
    description: str
    carton_count: int
    total_quantity: float
    uom: str = "PCS"
    net_weight_kg: float
    gross_weight_kg: float


class PackingList(BaseModel):
    pl_number: str
    invoice_ref: str
    total_cartons: int = 0
    total_quantity: float = 0.0
    total_net_weight_kg: float = 0.0
    total_gross_weight_kg: float = 0.0
    items: List[PackingListCartonGroup] = Field(default_factory=list)


class ShipmentPacket(BaseModel):
    shipment_id: str = "SHP-2026-8841"
    corridor: str = "IN-MUN -> US-LAX"
    buyer_po: Optional[BuyerPO] = None
    commercial_invoice: CommercialInvoice
    packing_list: PackingList

