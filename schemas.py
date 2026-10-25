"""
schemas.py — PortGate Pydantic models.

Two categories:
  1. LLM Structured Output schemas (prefixed with Extracted*)
     Used as the response_schema in Gemini API calls so the model
     returns a guaranteed-valid object — never raw text.

  2. API Response schemas (suffixed with *Response or *Out)
     Exactly match the JSON contract consumed by portgate.html.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# ── LLM EXTRACTION SCHEMAS (Gemini structured output) ──────────────────────
# ---------------------------------------------------------------------------


class ExtractedPOLine(BaseModel):
    """One line item from the US Buyer Purchase Order."""

    sku: str = Field(description="Buyer SKU code, e.g. PCR-M-TEE-NVY-L")
    desc: str = Field(description="Full English description of the goods")
    hts: str = Field(
        description=(
            "US HTS 10-digit code as printed on the PO, "
            "including dots, e.g. 6109.10.0012"
        )
    )
    qty: int = Field(description="Total PO quantity in pieces")
    prior: int = Field(
        default=0,
        description="Quantity already shipped before this shipment (0 if first shipment)"
    )
    open: int = Field(
        description="Open/remaining PO balance = qty - prior"
    )
    price: float = Field(description="Unit price in USD")


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

    desc: str = Field(description="Description of goods exactly as printed")
    hsn: str = Field(
        description=(
            "HSN/SAC code as printed in the invoice HSN column, "
            "e.g. 6109.10.00 or 6205.20.00. Do NOT correct or infer."
        )
    )
    qty: int = Field(description="Quantity in pieces")
    rate: float = Field(description="Unit rate in USD")
    line_total: float = Field(
        description="Printed line amount = qty * rate (USD, excluding IGST)"
    )
    net_wt_kg: float = Field(
        description="Net weight in KG from the Weight Statement table for this line"
    )
    gross_wt_kg: float = Field(
        description="Gross weight in KG from the Weight Statement table for this line"
    )
    ctns: int = Field(description="Number of cartons for this line item")


class ExtractedPLLine(BaseModel):
    """One line item from the Exporter Packing List."""

    desc: str = Field(description="Description of goods")
    ctns: int = Field(description="Number of cartons")
    net_wt_kg: float = Field(description="Net weight in KG")
    gross_wt_kg: float = Field(description="Gross weight in KG")


class ExtractedMetadata(BaseModel):
    """Document-level header fields extracted from all three documents."""

    po_number: str = Field(description="Buyer PO number, e.g. PO-PCR-45012")
    invoice_no: str = Field(
        description="Commercial invoice number, e.g. SHR/EXP/26-27/0412"
    )
    invoice_date: str = Field(description="Invoice date as printed, e.g. 14-Oct-2026")
    packing_list_no: str = Field(
        description="Packing list document number, e.g. SHR/PL/0412"
    )
    shipment_ref: str = Field(
        description="Internal shipment reference, e.g. SHP-2026-8841"
    )

    # Exporter (Indian side)
    exporter_name: str
    exporter_street: str = Field(
        description="Street / plot address line, e.g. Plot 2301, GIDC Pandesara"
    )
    exporter_city: str
    exporter_state: str
    exporter_postal: str
    exporter_country_iso: str = Field(
        description="ISO 3166-1 alpha-2 country code, always IN for India"
    )
    exporter_gstin: str = Field(
        default="",
        description="Indian GSTIN, 15 alphanumeric chars, e.g. 24AABCS1429B1Z5. Return empty string if not found."
    )
    exporter_iec: str = Field(
        default="",
        description="Indian Importer-Exporter Code, 10 digits, e.g. 5214098211. Return empty string if not found."
    )
    exporter_lut_arn: str = Field(
        default="",
        description="LUT/Bond ARN from the invoice header, e.g. AD240426009182Z. Return empty string if not present."
    )

    # Importer (US side)
    importer_name: str
    importer_city_state: str = Field(description="e.g. Los Angeles, CA")

    # Logistics
    port_of_loading: str = Field(description="UN/LOCODE, e.g. INMUN1")
    port_of_discharge: str = Field(description="UN/LOCODE, e.g. USLAX")
    incoterm: str = Field(description="Incoterm as printed on the invoice, e.g. FOB Mundra")
    currency: str = Field(description="ISO 4217 currency code on the invoice, should be USD")
    # Cross-document consistency fields (extracted from PO for linter cross-check)
    po_incoterm: str = Field(
        default="",
        description="Incoterm as printed on the Buyer PO, e.g. FOB Mundra. Return empty string if not found."
    )
    po_currency: str = Field(
        default="USD",
        description="Currency as printed on the Buyer PO, usually USD."
    )


class ExtractedShipmentPacket(BaseModel):
    """
    Top-level structured output that Gemini must populate.

    The LLM MUST:
    1. Semantically align each inv_lines entry to its corresponding po_lines
       entry by matching product descriptions (even when wording differs).
       Preserve the ORDER of inv_lines to match po_lines (aligned by index).
    2. Strip all Indian domestic tax rows from inv_lines.
    3. Populate pl_lines in the same order as inv_lines.
    """

    metadata: ExtractedMetadata
    po_lines: list[ExtractedPOLine]
    inv_lines: list[ExtractedInvLine]
    pl_lines: list[ExtractedPLLine]


# ---------------------------------------------------------------------------
# ── API RESPONSE SCHEMAS (returned to portgate.html) ───────────────────────
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    crit = "crit"
    warn = "warn"


class DiscrepancyItem(BaseModel):
    """A single compliance discrepancy — matches ITEMS[] in portgate.html."""

    id: str = Field(description="e.g. ERR-01, WARN-01")
    sev: Severity
    title: str
    cells: list[str] = Field(
        description=(
            "List of cell IDs to highlight in the UI, e.g. "
            "['inv-hsn1', 'po-hts1']"
        )
    )
    locs: list[list[str]] = Field(
        description=(
            "List of [tab_key, human_label] pairs, "
            "e.g. [['inv', 'Tally invoice'], ['po', 'Buyer PO']]"
        )
    )
    body: str = Field(description="Detailed explanation paragraph")
    impact: str = Field(description="Customs / business impact statement")
    fix: str = Field(description="Corrective action for the supplier")


class AuditSummary(BaseModel):
    status: str = Field(description="CLEAR or BLOCKED")
    critical_count: int
    warning_count: int
    passed_count: int


class MetadataOut(BaseModel):
    po_number: str
    invoice_no: str
    invoice_date: str
    packing_list_no: str
    exporter_name: str
    exporter_street: str
    exporter_city: str
    exporter_state: str
    exporter_postal: str
    exporter_country_iso: str
    exporter_gstin: str
    exporter_iec: str
    exporter_lut_arn: str
    importer_name: str
    importer_city_state: str
    port_of_loading: str
    port_of_discharge: str
    incoterm: str
    currency: str
    po_incoterm: str = ""
    po_currency: str = "USD"


class POLineOut(BaseModel):
    sku: str
    desc: str
    hts: str
    qty: int
    prior: int
    open: int
    price: float


class InvLineOut(BaseModel):
    desc: str
    hsn: str
    qty: int
    rate: float


class WeightsOut(BaseModel):
    """Parallel arrays of [net_kg, gross_kg] per line for inv and pl."""

    inv: list[list[float]]
    pl: list[list[float]]


class CartonsOut(BaseModel):
    inv: list[int]
    pl: list[int]


class AuditResponse(BaseModel):
    """
    Root response model for POST /api/audit.
    Field names and structure MUST match the JavaScript state variables
    in portgate.html exactly so the frontend can consume it without
    any transformation.
    """

    shipment_ref: str
    metadata: MetadataOut
    mid_code: str
    po_lines: list[POLineOut]
    inv_lines: list[InvLineOut]
    weights: WeightsOut
    cartons: CartonsOut
    items: list[DiscrepancyItem]
    passed: list[str]
    summary: AuditSummary
