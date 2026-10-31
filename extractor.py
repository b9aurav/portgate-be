"""
extractor.py — Multimodal PDF ingestion service using Google GenAI.

Sends up to 3 raw PDF bytes (PO, Invoice, Packing List) to Gemini with a
structured-output response schema so we receive a guaranteed-valid
ExtractedShipmentPacket — never raw text.

The LLM is instructed ONLY to extract and semantically align data.
All arithmetic and compliance decisions happen in linter.py.

Environment:
    GOOGLE_API_KEY   — required for live mode
    GEMINI_MODEL     — optional, defaults to gemini-3.8-flash
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from schemas import ExtractedShipmentPacket  # noqa: E402 (after dotenv)

# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------
_DEFAULT_MODEL = "gemini-3.8-flash"
_FALLBACK_MODEL = "gemini-3.6-flash"


def _get_client():
    """Lazily create and return a google.genai.Client."""
    try:
        from google import genai  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "google-genai is not installed. Run: pip install google-genai"
        ) from exc

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY environment variable is not set. "
            "Add it to your .env file or export it in your shell."
        )
    return genai.Client(api_key=api_key)


# ---------------------------------------------------------------------------
# System prompt
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
• NEVER compute `amount` as `quantity * rate`. Always extract the EXACT printed number in the Amount/Total column, even if it is mathematically wrong (e.g., if 500 × $9.50 prints as "$4,850.00", you MUST extract 4850.00).
• Capture HSN / SAC / HTS / HS codes EXACTLY as printed on each respective document, including dots or spaces. Do NOT normalise or copy the PO's code onto the Invoice.
• Capture Units of Measure (UOM) verbatim as printed on each document (e.g., "PCS", "SETS", "SQM", "NOS", "KGS").

════════════════════════════════════════════════════════════
2. COMMERCIAL INVOICE RULES (TALLY PRIME & GLOBAL ERPs)
════════════════════════════════════════════════════════════
Exporters (especially on Tally Prime) frequently print tax rows, declarations, and sub-totals inside the main line-item table grid.

DO NOT EXTRACT THESE AS PRODUCT LINE ITEMS (`inv_lines`):
─────────────────────────────────────────────────────────
• Tax / Duty rows inside the item grid:
    - "IGST @ 0%", "IGST @ 18%", "CGST", "SGST", "VAT", "Export Duty"
    - "Export Under LUT", "Supply meant for export under Bond/LUT"
    - "Freight", "Insurance", "Rounding Off", "Less: Discount"
    - "E.&O.E." or "E. & O. E."
• Sub-total rows, "Total" summary rows, HSN/SAC tax-summary sub-tables at the bottom of the invoice, bank detail blocks, or "Authorised Signatory" blocks.

DO CAPTURE THESE INTO INVOICE HEADER METADATA:
─────────────────────────────────────────────────────────
• Even though tax/LUT rows are excluded from `inv_lines`, DO inspect header/footer blocks to extract header metadata fields:
    - Invoice No, Invoice Date, Buyer PO Reference, Incoterms, Currency
    - Port of Loading, Port of Discharge, Country of Origin
    - Exporter Tax/Customs IDs: GSTIN, IEC, LUT ARN (starts with "ARN:" or "AD..."), and Export Tax Route ("LUT_BOND" vs "IGST_PAYMENT") if applicable.
    - Printed Header Totals: Total Invoice Value, Total Cartons, Total Net Weight (KG), Total Gross Weight (KG).

DO CAPTURE THESE INTO `inv_lines`:
─────────────────────────────────────────────────────────
• Product rows only: original printed `line_number` (sl.no), `description`, `hsn_code`, `quantity`, `uom`, `unit_price_usd` (rate), `total_usd` (printed amount).
• Per-line Weight & Carton Statement (often printed in a separate sub-table at the bottom of Tally invoices): attach `net_wt_kg`, `gross_wt_kg`, and `cartons` to the corresponding product line.

════════════════════════════════════════════════════════════
3. SEMANTIC ALIGNMENT & ORDERING RULES
════════════════════════════════════════════════════════════
Exporters rarely use the buyer's exact SKU phrasing. Match each Invoice line (`inv_lines`) and Packing List line (`pl_lines`) to its corresponding Buyer PO line (`po_lines`) by semantic product similarity, NOT by row position.

Examples of valid semantic matches:
  PO Description                             → Exporter Invoice / PL Description
  ──────────────────────────────────────────────────────────────────────────────
  "Men's 100% Combed Cotton Crewneck Tee"    → "100% COTTON MENS ROUND NECK T-SHIRT"
  "Women's Pique Polo Shirt"                 → "LADIES COTTON PIQUE COLLAR T-SHIRT"
  "Heavyweight Fleece Pullover Hoodie"       → "FLEECE PULLOVER HOODIE HEAVY"
  "Rustic Terracotta Exterior Patio Paver"   → "RUSTIC TERRACOTTA EXTERIOR PATIO PAVER 450X450 CLAY"

• Return `inv_lines` and `pl_lines` ordered so that index `i` corresponds to `po_lines[i]` (`inv_lines[0]` matches `po_lines[0]`, etc.).
• If any document contains an extra/unmatched product line that has no counterpart in the PO, append it at the end of the array rather than dropping it.

════════════════════════════════════════════════════════════
4. PACKING LIST RULES
════════════════════════════════════════════════════════════
• Extract `pl_lines` aligned in the exact same semantic order as `inv_lines`.
• Capture per line: `description`, `quantity`, `uom`, `cartons` (ctns/boxes/pallets), `net_wt_kg`, and `gross_wt_kg`.
• Capture Packing List header totals (`total_cartons`, `total_net_wt_kg`, `total_gross_wt_kg`) verbatim as printed in the Packing List summary row.

════════════════════════════════════════════════════════════
5. OUTPUT FORMAT
════════════════════════════════════════════════════════════
Return a single valid JSON object conforming strictly to the `ExtractedShipmentPacket` Pydantic schema. Do NOT include markdown code fences (```json), commentary, or any text outside the JSON object.
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_shipment_packet(
    po_bytes: bytes,
    inv_bytes: bytes,
    pl_bytes: bytes,
    model: str | None = None,
) -> ExtractedShipmentPacket:
    """
    Send three PDF documents to Gemini and return a structured packet.

    Args:
        po_bytes:  Raw bytes of the US Buyer PO PDF.
        inv_bytes: Raw bytes of the Tally Prime Commercial Invoice PDF.
        pl_bytes:  Raw bytes of the Packing List PDF.
        model:     Gemini model name (defaults to GEMINI_MODEL env var or
                   gemini-3.8-flash-preview-05-20).

    Returns:
        A fully populated ExtractedShipmentPacket validated by Pydantic.

    Raises:
        RuntimeError: If GOOGLE_API_KEY is not set or the API call fails.
        pydantic.ValidationError: If the model returns a schema-invalid response.
    """
    import time
    from google import genai  # type: ignore
    from google.genai import errors
    from google.genai import types  # type: ignore

    client = _get_client()
    selected_model = model or os.getenv("GEMINI_MODEL", _DEFAULT_MODEL)

    models_to_try = [
        selected_model,
        os.getenv("GEMINI_FALLBACK_MODEL", _FALLBACK_MODEL),
        "gemini-3.8-flash",
    ]

    unique_models = list(dict.fromkeys(models_to_try))

    response = None
    last_err = None

    # Build inline PDF parts
    import io
    import pypdfium2 as pdfium  # type: ignore

    def _pdf_to_parts(data: bytes) -> list[types.Part]:
        """Convert PDF bytes into fast inline PNG image parts (150 DPI)."""
        try:
            pdf = pdfium.PdfDocument(data)
            parts: list[types.Part] = []
            for page in pdf:
                # scale=2.0 renders at ~144 DPI — crisp text, tiny payload (~60KB)
                bitmap = page.render(scale=2.0)
                pil_img = bitmap.to_pil()
                buf = io.BytesIO()
                pil_img.save(buf, format="PNG", optimize=True)
                parts.append(
                    types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png")
                )
            pdf.close()
            return parts
        except Exception:
            # Fallback to raw PDF if pypdfium2 isn't installed or fails
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

    config_kwargs = dict(
        system_instruction=_SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=ExtractedShipmentPacket,
        temperature=0.0,
        max_output_tokens=4096,
        http_options=types.HttpOptions(timeout=35000),  # 35s max per attempt (in ms)
    )

    if hasattr(types, "ThinkingConfig"):
        config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)

    config = types.GenerateContentConfig(**config_kwargs)

    for m in unique_models:
        for attempt in range(3):  # 3 attempts per model
            try:
                t0 = time.perf_counter()
                print(f"[PortGate] Calling Gemini model '{m}' (attempt {attempt + 1})...")
                response = client.models.generate_content(
                    model=m,
                    contents=contents,
                    config=config,
                )
                elapsed = time.perf_counter() - t0
                print(f"[PortGate] Gemini '{m}' responded in {elapsed:.2f}s")
                break  # Success!
            except errors.APIError as exc:
                last_err = exc
                # Retry on 503 (Overloaded/Unavailable) or 429 (Rate limit)
                if exc.code in (503, 429):
                    wait_sec = 2 ** attempt  # exponential: 1s, 2s, 4s
                    print(f"[PortGate] Model '{m}' returned {exc.code}. Retrying in {wait_sec}s... (attempt {attempt + 1}/3)")
                    time.sleep(wait_sec)
                    continue
                raise  # Raise immediately on 400/401/404 errors
        if response is not None:
            break

    if response is None:
        raise RuntimeError(f"All Gemini models busy/unavailable. Last error: {last_err}")

    # Guard against empty model response
    raw_json = response.text
    if not raw_json:
        raise RuntimeError(
            "Gemini returned an empty response. The model may have refused to process "
            "one of the PDFs (e.g. due to safety filters or an unreadable document). "
            "Check the response finish_reason and try again, or use mock=true for testing."
        )
    return ExtractedShipmentPacket.model_validate_json(raw_json)


# ---------------------------------------------------------------------------
# Convenience: load PDFs from disk (useful for development / CLI testing)
# ---------------------------------------------------------------------------

def extract_from_files(
    po_path: str | Path,
    inv_path: str | Path,
    pl_path: str | Path,
    model: str | None = None,
) -> ExtractedShipmentPacket:
    """
    Helper that reads three PDF files from disk and calls extract_shipment_packet.

    Args:
        po_path:  Path to the PO PDF file.
        inv_path: Path to the Invoice PDF file.
        pl_path:  Path to the Packing List PDF file.
        model:    Optional model override.

    Returns:
        ExtractedShipmentPacket
    """
    po_bytes = Path(po_path).read_bytes()
    inv_bytes = Path(inv_path).read_bytes()
    pl_bytes = Path(pl_path).read_bytes()
    return extract_shipment_packet(po_bytes, inv_bytes, pl_bytes, model=model)
