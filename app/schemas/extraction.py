from __future__ import annotations

from typing import List
from pydantic import BaseModel, Field


class ExtractedPOLine(BaseModel):
    """One line item from the US Buyer Purchase Order."""

    sku: str = Field(default="SKU-1", description="Buyer SKU code, e.g. PCR-M-TEE-NVY-L")
    desc: str = Field(default="Goods", description="Full English description of the goods")
    hts: str = Field(
        default="6109.10.00",
        description="US HTS 10-digit code as printed on the PO, including dots, e.g. 6109.10.0012",
    )
    qty: int = Field(default=0, description="Total PO quantity in pieces")
    prior: int = Field(
        default=0,
        description="Quantity already shipped before this shipment (0 if first shipment)",
    )
    open: int = Field(
        default=0,
        description="Open/remaining PO balance = qty - prior",
    )
    price: float = Field(default=0.0, description="Unit price in USD")


class ExtractedInvLine(BaseModel):
    """
    One line item from the Indian Tally Prime Commercial Invoice.

    Extraction rules for the LLM:
    - IGNORE rows that are pure Indian domestic tax noise:
      'IGST @ 0%', 'CGST', 'SGST', 'Export Under LUT/Bond', 'ARN', 'E.&O.E.'
    - IGNORE sub-totals and tax amount rows.
    - Capture the EXACT HSN/SAC code printed in the HSN column — do NOT
      infer or correct it; the compliance engine will flag mismatches.
    - Weight Statement table: capture net_wt_kg and gross_wt_kg per line.
    - line_total should be qty * rate as printed (excluding any tax).
    """

    desc: str = Field(default="Goods", description="Description of goods exactly as printed")
    hsn: str = Field(
        default="6109.10.00",
        description="HSN/SAC code as printed in the invoice HSN column, e.g. 6109.10.00 or 6205.20.00. Do NOT correct or infer.",
    )
    qty: int = Field(default=0, description="Quantity in pieces")
    rate: float = Field(default=0.0, description="Unit rate in USD")
    line_total: float = Field(
        default=0.0,
        description="Printed line amount = qty * rate (USD, excluding IGST)",
    )
    net_wt_kg: float = Field(
        default=0.0,
        description="Net weight in KG from the Weight Statement table for this line",
    )
    gross_wt_kg: float = Field(
        default=0.0,
        description="Gross weight in KG from the Weight Statement table for this line",
    )
    ctns: int = Field(default=0, description="Number of cartons for this line item")


class ExtractedPLLine(BaseModel):
    """One line item from the Exporter Packing List."""

    desc: str = Field(default="Goods", description="Description of goods")
    ctns: int = Field(default=0, description="Number of cartons")
    net_wt_kg: float = Field(default=0.0, description="Net weight in KG")
    gross_wt_kg: float = Field(default=0.0, description="Gross weight in KG")


class ExtractedMetadata(BaseModel):
    """Document-level header fields extracted from all three documents."""

    po_number: str = Field(default="PO-UNKNOWN", description="Buyer PO number, e.g. PO-PCR-45012")
    invoice_no: str = Field(
        default="INV-UNKNOWN",
        description="Commercial invoice number, e.g. SHR/EXP/26-27/0412",
    )
    invoice_date: str = Field(default="14-Oct-2026", description="Invoice date as printed, e.g. 14-Oct-2026")
    packing_list_no: str = Field(
        default="PL-UNKNOWN",
        description="Packing list document number, e.g. SHR/PL/0412",
    )
    shipment_ref: str = Field(
        default="SHP-2026-8841",
        description="Internal shipment reference, e.g. SHP-2026-8841",
    )

    # Exporter (Indian side)
    exporter_name: str = Field(default="Exporter", description="Exporter company name")
    exporter_street: str = Field(
        default="",
        description="Street / plot address line, e.g. Plot 2301, GIDC Pandesara",
    )
    exporter_city: str = Field(default="", description="Exporter city")
    exporter_state: str = Field(default="", description="Exporter state")
    exporter_postal: str = Field(default="", description="Exporter postal code")
    exporter_country_iso: str = Field(
        default="IN",
        description="ISO 3166-1 alpha-2 country code, always IN for India",
    )
    exporter_gstin: str = Field(
        default="",
        description="Indian GSTIN, 15 alphanumeric chars, e.g. 24AABCS1429B1Z5. Return empty string if not found.",
    )
    exporter_iec: str = Field(
        default="",
        description="Indian Importer-Exporter Code, 10 digits, e.g. 5214098211. Return empty string if not found.",
    )
    exporter_lut_arn: str = Field(
        default="",
        description="LUT/Bond ARN from the invoice header, e.g. AD240426009182Z. Return empty string if not present.",
    )
    export_tax_route: str = Field(
        default="LUT_BOND",
        description="Indian export tax route: 'LUT_BOND' or 'IGST_PAYMENT'. Default to 'LUT_BOND'.",
    )

    # Importer (US side)
    importer_name: str = Field(default="Pacific Coast Retail Group LLC", description="Importer company name")
    importer_city_state: str = Field(default="Los Angeles, CA", description="e.g. Los Angeles, CA")

    # Logistics
    port_of_loading: str = Field(default="INMUN1", description="UN/LOCODE, e.g. INMUN1")
    port_of_discharge: str = Field(default="USLAX", description="UN/LOCODE, e.g. USLAX")
    incoterm: str = Field(default="FOB Mundra", description="Incoterm as printed on the invoice, e.g. FOB Mundra")
    currency: str = Field(default="USD", description="ISO 4217 currency code on the invoice, should be USD")
    po_incoterm: str = Field(
        default="",
        description="Incoterm as printed on the Buyer PO, e.g. FOB Mundra. Return empty string if not found.",
    )
    po_currency: str = Field(
        default="USD",
        description="Currency as printed on the Buyer PO, usually USD.",
    )


class ExtractedShipmentPacket(BaseModel):
    """
    Top-level structured output that Gemini must populate.
    """

    metadata: ExtractedMetadata
    po_lines: List[ExtractedPOLine] = Field(default_factory=list)
    inv_lines: List[ExtractedInvLine] = Field(default_factory=list)
    pl_lines: List[ExtractedPLLine] = Field(default_factory=list)

