"""
app/services/extractor.py — Multimodal PDF ingestion service using Google GenAI.

Sends up to 3 raw PDF bytes (PO, Invoice, Packing List) to Gemini with a
structured-output response schema so we receive a guaranteed-valid
ExtractedShipmentPacket — never raw text.

The LLM is instructed ONLY to extract and semantically align data verbatim.
All arithmetic, compliance decisions, and severities happen in linter.py.
"""

from __future__ import annotations

import copy
import io
import logging
import time
from pathlib import Path
from typing import Any, Optional

from app import config
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

logger = logging.getLogger("portgate.extractor")

# ---------------------------------------------------------------------------
# Client Helper
# ---------------------------------------------------------------------------
def _get_client():
    """Lazily create and return a google.genai.Client."""
    try:
        from google import genai  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "google-genai is not installed. Run: pip install google-genai"
        ) from exc

    if not config.GOOGLE_API_KEY:
        raise RuntimeError(
            "GOOGLE_API_KEY environment variable is not set. "
            "Add it to your .env file or export it in your environment."
        )
    return genai.Client(api_key=config.GOOGLE_API_KEY)


# ---------------------------------------------------------------------------
# System Prompt (Updated with Explicit LUT ARN Header Extraction Rules)
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """\
You are a senior global trade-document parser and cross-border customs extraction engine, with deep specialisation in India-to-US (Tally Prime / GST / LUT) and global export corridors.

You will receive up to three trade documents (as PDFs or structured reference context):
  1. Buyer Purchase Order (from a buyer ERP such as NetSuite, SAP, or ApparelMagic)
  2. Exporter Commercial Invoice (from Tally Prime, Zoho, SAP, or local exporter ERP)
  3. Exporter Packing List

Your SOLE task is to EXTRACT verbatim data and SEMANTICALLY ALIGN line items across documents.
You must NEVER perform arithmetic, recalculate line totals, fix typos, or issue any compliance verdict.

════════════════════════════════════════════════════════════
1. VERBATIM EXTRACTION & ANTI-AUTO-CORRECTION RULES (CRITICAL)
════════════════════════════════════════════════════════════
• NEVER compute `line_total` as `quantity * rate`. Always extract the EXACT printed number in the Amount/Total column, even if it is mathematically wrong (e.g., if 500 × $9.50 prints as "$4,850.00", you MUST extract 4850.00).
• Capture HSN / SAC / HTS / HS codes EXACTLY as printed on each respective document, including dots or spaces. Do NOT normalise or copy the PO's code onto the Invoice.
• Capture Units of Measure (UOM) verbatim as printed on each document (e.g., "PCS", "SETS", "SQM", "NOS", "KGS").

════════════════════════════════════════════════════════════
2. COMMERCIAL INVOICE RULES (TALLY PRIME & INDIAN GST/LUT)
════════════════════════════════════════════════════════════
Exporters (especially on Tally Prime) frequently print tax rows, declarations, and sub-totals inside the main line-item table grid.

DO NOT EXTRACT THESE AS PRODUCT LINE ITEMS (`inv_lines`):
─────────────────────────────────────────────────────────
• Tax / Duty rows inside the item grid:
    - "IGST @ 0%", "IGST @ 18%", "CGST", "SGST", "VAT", "Export Duty"
    - "Export Under LUT", "Supply meant for export under Bond/LUT", "ARN:"
    - "Freight", "Insurance", "Rounding Off", "Less: Discount"
    - "E.&O.E." or "E. & O. E."
• Sub-total rows, "Total" summary rows, HSN/SAC tax-summary sub-tables at the bottom of the invoice, bank detail blocks, or "Authorised Signatory" blocks.

DO EXPLICITLY CAPTURE THESE INTO INVOICE HEADER METADATA (`metadata`):
─────────────────────────────────────────────────────────
• Even though tax/LUT rows are excluded from `inv_lines`, inspect header/declaration/footer blocks to extract:
    - Invoice No, Invoice Date, Buyer PO Reference, Incoterms, Currency
    - Port of Loading, Port of Discharge, Country of Origin
    - Exporter Tax/Customs IDs: GSTIN, IEC
    - Indian Export Tax Route (`export_tax_route`):
        * If invoice states "Export Under LUT", "Supply meant for export under Bond/LUT", or shows "IGST @ 0%", set `export_tax_route` = "LUT_BOND".
        * If invoice shows actual IGST tax payment charged/collected, set `export_tax_route` = "IGST_PAYMENT".
        * Default to "LUT_BOND" for zero-rated Indian exports.
    - LUT Application Reference Number (`exporter_lut_arn`):
        * Search headers, LUT declaration boxes, or LUT notes for an ARN string starting with "ARN:" or "AD..." (e.g., "AD240426009182Z").
        * If NO valid ARN is found on an LUT invoice, return an empty string `""` so the downstream rules engine can flag the missing LUT ARN.
    - Printed Header Totals: Total Invoice Value, Total Cartons, Total Net Weight (KG), Total Gross Weight (KG).

DO CAPTURE THESE INTO `inv_lines`:
─────────────────────────────────────────────────────────
• Product rows only: original printed `description`, `hsn`, `qty`, `rate`, `line_total`.
• Weight & Carton Statement (often printed in a separate sub-table at the bottom of Tally invoices): attach `net_wt_kg`, `gross_wt_kg`, and `ctns` to the corresponding product line.

════════════════════════════════════════════════════════════
3. CROSS-DOCUMENT SEMANTIC ALIGNMENT
════════════════════════════════════════════════════════════
• Each entry in `inv_lines` must correspond to the SAME product in `po_lines` at the same index `idx`.
• Match line items semantically by product description, even when wording differs between PO and Invoice.
• Order `inv_lines` so that `inv_lines[i]` matches `po_lines[i]`.
• If the packing list itemizes lines, align `pl_lines[i]` with `inv_lines[i]`.
"""


# ---------------------------------------------------------------------------
# Multimodal PDF Ingestion
# ---------------------------------------------------------------------------
def extract_shipment_packet(
    po_bytes: bytes,
    inv_bytes: bytes,
    pl_bytes: bytes,
    model: str | None = None,
) -> ExtractedShipmentPacket:
    """
    Calls Google Gemini multimodal API with the 3 documents and enforces
    structured JSON output validated against ExtractedShipmentPacket.
    """
    from google.genai import errors, types  # type: ignore

    client = _get_client()

    models_to_try = [
        model or config.GEMINI_MODEL,
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        config.GEMINI_FALLBACK_MODEL,
        "gemini-3.8-flash",
    ]
    unique_models = list(dict.fromkeys(models_to_try))

    def _pdf_to_parts(data: bytes) -> list[types.Part]:
        """
        Supplies native application/pdf parts directly to Gemini multimodal ingestion.
        Native PDF tokenization is 5-10x faster and avoids high-resolution bitmap bloat.
        """
        try:
            return [types.Part.from_bytes(data=data, mime_type="application/pdf")]
        except Exception:
            return [types.Part.from_bytes(data=data, mime_type="application/pdf")]

    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(
                    text="Please extract and semantically align the three attached documents "
                    "per your instructions. Return the structured JSON output."
                ),
                types.Part.from_text(text="DOCUMENT 1 — US BUYER PURCHASE ORDER:"),
                *_pdf_to_parts(po_bytes),
                types.Part.from_text(text="DOCUMENT 2 — INDIAN EXPORTER TALLY INVOICE:"),
                *_pdf_to_parts(inv_bytes),
                types.Part.from_text(text="DOCUMENT 3 — EXPORTER PACKING LIST:"),
                *_pdf_to_parts(pl_bytes),
            ],
        )
    ]

    config_kwargs: dict[str, Any] = dict(
        system_instruction=_SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=ExtractedShipmentPacket,
        temperature=0.0,
        max_output_tokens=4096,
        http_options=types.HttpOptions(timeout=60000),
    )

    gen_config = types.GenerateContentConfig(**config_kwargs)

    response = None
    last_err = None

    for m in unique_models:
        for attempt in range(2):
            try:
                t0 = time.perf_counter()
                logger.info("Calling Gemini model '%s' (attempt %d)...", m, attempt + 1)
                response = client.models.generate_content(
                    model=m,
                    contents=contents,
                    config=gen_config,
                )
                elapsed = time.perf_counter() - t0
                logger.info("Gemini '%s' responded in %.2fs", m, elapsed)
                break
            except errors.APIError as exc:
                last_err = exc
                if exc.code == 429:
                    wait_sec = 2 ** attempt
                    logger.warning("Model '%s' rate limited (429). Retrying in %ds...", m, wait_sec)
                    time.sleep(wait_sec)
                    continue
                elif exc.code in (503, 504, 404, 400):
                    logger.warning(
                        "Model '%s' failed with %s (%s). Falling back to next available model...",
                        m,
                        exc.code,
                        getattr(exc, "message", str(exc)),
                    )
                    break
                raise
        if response is not None:
            break

    if response is None:
        raise RuntimeError(f"All Gemini models busy or unavailable. Last error: {last_err}")

    raw_json = response.text
    if not raw_json:
        raise RuntimeError("Gemini returned an empty response. Verify PDFs are legible.")

    return ExtractedShipmentPacket.model_validate_json(raw_json)


# ---------------------------------------------------------------------------
# Conversion: ExtractedShipmentPacket -> ShipmentPacket (Domain Model)
# ---------------------------------------------------------------------------
def extracted_to_shipment_packet(
    extracted: ExtractedShipmentPacket,
    tolerance_pct: float = 5.0,
    shipment_id: Optional[str] = None,
) -> ShipmentPacket:
    """
    Transforms Gemini's raw extraction object into the domain ShipmentPacket.
    Guarantees null-safety across ragged line items and missing header fields.
    """
    meta = extracted.metadata

    po_line_items = [
        POLineItem(
            line_no=idx + 1,
            sku=po.sku or f"SKU-{idx + 1}",
            description=po.desc or "Product",
            hts_code=po.hts or "6109.10.00",
            quantity=float(po.qty or 0),
            uom="PCS",
            unit_price_usd=float(po.price or 0.0),
            total_usd=float(po.qty or 0) * float(po.price or 0.0),
        )
        for idx, po in enumerate(extracted.po_lines or [])
    ]

    buyer_po = BuyerPO(
        po_number=meta.po_number or "PO-UNKNOWN",
        buyer_name=meta.importer_name or "Pacific Coast Retail Group LLC",
        po_date=meta.invoice_date or "14-Oct-2026",
        incoterms=meta.po_incoterm or meta.incoterm or "FOB Mundra",
        quantity_tolerance_pct=tolerance_pct,
        line_items=po_line_items,
        total_po_value_usd=sum(p.total_usd for p in po_line_items),
    )

    inv_line_items = [
        InvoiceLineItem(
            line_no=idx + 1,
            po_line_ref=idx + 1,
            description=line.desc or "Goods",
            hsn_code=line.hsn or "6109.10.00",
            quantity=float(line.qty or 0),
            uom="PCS",
            unit_price_usd=float(line.rate or 0.0),
            total_usd=float(
                line.line_total
                if line.line_total is not None
                else (line.qty or 0) * (line.rate or 0.0)
            ),
            carton_count=int(getattr(line, "ctns", 0) or 0) if getattr(line, "ctns", None) is not None else None,
            net_weight_kg=float(getattr(line, "net_wt_kg", 0.0) or 0.0) if getattr(line, "net_wt_kg", None) is not None else None,
            gross_weight_kg=float(getattr(line, "gross_wt_kg", 0.0) or 0.0) if getattr(line, "gross_wt_kg", None) is not None else None,
        )
        for idx, line in enumerate(extracted.inv_lines or [])
    ]

    tax_route = getattr(meta, "export_tax_route", "LUT_BOND") or "LUT_BOND"
    if str(tax_route).upper() not in ("LUT_BOND", "IGST_PAYMENT"):
        tax_route = "LUT_BOND"

    # Robust Commercial Invoice carton total resolution
    inv_lines_ctns_sum = sum(int(getattr(l, "ctns", 0) or 0) for l in (extracted.inv_lines or []))
    meta_inv_ctns = getattr(meta, "total_cartons_declared", None)
    total_inv_cartons = (
        int(meta_inv_ctns)
        if (meta_inv_ctns is not None and int(meta_inv_ctns) > 0)
        else inv_lines_ctns_sum
    )

    commercial_invoice = CommercialInvoice(
        invoice_number=meta.invoice_no or "INV-UNKNOWN",
        invoice_date=meta.invoice_date or "14-Oct-2026",
        exporter_name=meta.exporter_name or "Exporter",
        exporter_gstin=meta.exporter_gstin or None,
        export_tax_route=tax_route,
        lut_arn_number=meta.exporter_lut_arn or None,
        buyer_po_ref=meta.po_number or "PO-UNKNOWN",
        total_cartons_declared=total_inv_cartons,
        total_net_weight_kg=sum(float(getattr(l, "net_wt_kg", 0.0) or 0.0) for l in (extracted.inv_lines or [])),
        total_gross_weight_kg=sum(float(getattr(l, "gross_wt_kg", 0.0) or 0.0) for l in (extracted.inv_lines or [])),
        total_invoice_value_usd=sum(float(getattr(l, "line_total", 0.0) or 0.0) for l in (extracted.inv_lines or [])),
        line_items=inv_line_items,
    )

    pl_items = [
        PackingListCartonGroup(
            line_no=idx + 1,
            description=line.desc or f"Carton Group #{idx + 1}",
            carton_count=int(getattr(line, "ctns", 10) or 10),
            total_quantity=float(
                (extracted.inv_lines[idx].qty or 0)
                if idx < len(extracted.inv_lines or [])
                else 0.0
            ),
            uom="PCS",
            net_weight_kg=float(getattr(line, "net_wt_kg", 100.0) or 100.0),
            gross_weight_kg=float(getattr(line, "gross_wt_kg", 110.0) or 110.0),
        )
        for idx, line in enumerate(extracted.pl_lines or [])
    ]

    # Robust Packing List carton total resolution
    pl_lines_ctns_sum = sum(p.carton_count for p in pl_items)
    meta_pl_ctns = getattr(meta, "total_cartons", None)
    total_pl_cartons = (
        int(meta_pl_ctns)
        if (meta_pl_ctns is not None and int(meta_pl_ctns) > 0)
        else (pl_lines_ctns_sum or 100)
    )

    packing_list = PackingList(
        pl_number=meta.packing_list_no or "PL-UNKNOWN",
        invoice_ref=meta.invoice_no or "INV-UNKNOWN",
        total_cartons=total_pl_cartons,
        total_quantity=sum(p.total_quantity for p in pl_items),
        total_net_weight_kg=sum(float(getattr(l, "net_wt_kg", 0.0) or 0.0) for l in (extracted.pl_lines or [])),
        total_gross_weight_kg=sum(float(getattr(l, "gross_wt_kg", 0.0) or 0.0) for l in (extracted.pl_lines or [])),
        items=pl_items,
    )

    return ShipmentPacket(
        shipment_id=shipment_id or meta.shipment_ref or "SHP-2026-8841",
        corridor=f"{meta.port_of_loading or 'INMUN1'} -> {meta.port_of_discharge or 'USLAX'}",
        buyer_po=buyer_po,
        commercial_invoice=commercial_invoice,
        packing_list=packing_list,
    )


# ---------------------------------------------------------------------------
# Mock & Simulation Packet Builders (Canonical Shreeji Textiles Dataset)
# ---------------------------------------------------------------------------
def build_mock_packet() -> ExtractedShipmentPacket:
    """
    Returns the canonical Shreeji Textiles sabotaged packet with planted discrepancies
    for testing without calling external Gemini endpoints.
    """
    metadata = ExtractedMetadata(
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
    )

    po_lines = [
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
        ExtractedPOLine(
            sku="PCR-H-HOOD-GRY-XL",
            desc="Heavyweight Fleece Pullover Hoodie",
            hts="6110.20.2079",
            qty=1000,
            prior=0,
            open=1000,
            price=11.00,
        ),
    ]

    inv_lines = [
        ExtractedInvLine(
            desc="100% COTTON MENS ROUND NECK T-SHIRT (KNITTED) BATCH #44A",
            hsn="6205.20.00",  # Planting 6205.20 vs 6109.10 HS root divergence
            qty=2000,
            rate=4.50,
            line_total=9000.00,
            net_wt_kg=380.0,
            gross_wt_kg=420.0,
            ctns=40,
        ),
        ExtractedInvLine(
            desc="LADIES COTTON PIQUE COLLAR T-SHIRT WHITE",
            hsn="6106.10.00",
            qty=2050,  # Planting overage (+2.5%)
            rate=6.40,  # Planting price variance ($6.40 vs $6.00)
            line_total=13120.00,
            net_wt_kg=410.0,
            gross_wt_kg=450.0,
            ctns=30,
        ),
        ExtractedInvLine(
            desc="HEAVYWEIGHT FLEECE PULLOVER HOODIE ASH GREY",
            hsn="6110.20.00",
            qty=1000,
            rate=11.00,
            line_total=11000.00,
            net_wt_kg=520.0,
            gross_wt_kg=490.0,  # Planting physical weight inversion (Net 520 > Gross 490)
            ctns=30,  # Planting carton drift (total 100 CTNS vs PL 110 CTNS)
        ),
    ]

    pl_lines = [
        ExtractedPLLine(
            desc="Cartons 1-40: 100% COTTON MENS ROUND NECK T-SHIRT",
            ctns=40,
            net_wt_kg=380.0,
            gross_wt_kg=420.0,
        ),
        ExtractedPLLine(
            desc="Cartons 41-70: LADIES COTTON PIQUE COLLAR T-SHIRT",
            ctns=30,
            net_wt_kg=410.0,
            gross_wt_kg=450.0,
        ),
        ExtractedPLLine(
            desc="Cartons 71-110: HEAVYWEIGHT FLEECE PULLOVER HOODIE",
            ctns=40,  # Causes 110 CTNS total vs 100 CTNS on invoice
            net_wt_kg=520.0,
            gross_wt_kg=490.0,
        ),
    ]

    return ExtractedShipmentPacket(
        metadata=metadata,
        po_lines=po_lines,
        inv_lines=inv_lines,
        pl_lines=pl_lines,
    )


def build_mock_revised_packet(packet: ShipmentPacket) -> ShipmentPacket:
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
    for idx, inv_item in enumerate(inv.line_items):
        if idx < len(pl.items):
            inv_item.carton_count = pl.items[idx].carton_count
            inv_item.net_weight_kg = pl.items[idx].net_weight_kg
            inv_item.gross_weight_kg = pl.items[idx].gross_weight_kg

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
                inv_item.unit_price_usd = po_item.unit_price_usd
                inv_item.uom = po_item.uom
                hsn_root = po_item.hts_code.replace(".", "")[:6]
                inv_item.hsn_code = f"{hsn_root[:4]}.{hsn_root[4:6]}.00"
                inv_item.total_usd = round(inv_item.quantity * inv_item.unit_price_usd, 2)

    inv.total_invoice_value_usd = sum(l.total_usd for l in inv.line_items)

    if inv.export_tax_route == "LUT_BOND" and not inv.lut_arn_number:
        inv.lut_arn_number = "AD240426009182Z"

    return revised

