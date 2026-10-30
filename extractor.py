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
You are a senior trade-document parser specialised in India-to-US export shipments.

You will receive up to three PDF documents:
  1. US Buyer Purchase Order (from a US ERP such as NetSuite)
  2. Indian Exporter Commercial Invoice (from Tally Prime)
  3. Exporter Packing List

Your SOLE task is to EXTRACT data and SEMANTICALLY ALIGN line items across documents.
You must NEVER perform arithmetic, compute totals, or issue any compliance verdict.

════════════════════════════════════════════════════════════
EXTRACTION RULES FOR THE TALLY PRIME COMMERCIAL INVOICE
════════════════════════════════════════════════════════════

Tally Prime invoices for zero-rated exports contain several sections
that must be COMPLETELY IGNORED:

  IGNORE these rows / blocks:
  ─────────────────────────────────────────────────────────
  • Any row whose description contains:
      - "IGST @ 0%"
      - "CGST"
      - "SGST"
      - "E.&O.E." or "E. & O. E."
      - "Export Under LUT"
      - "Supply meant for export"
  • The LUT/Bond declaration header paragraph
  • The ARN (Application Reference Number) row (starts with "ARN:")
  • Any sub-total / tax-summary table at the bottom of the invoice
  • Bank details, declaration footer, or "Authorised Signatory" blocks

  CAPTURE these sections:
  ─────────────────────────────────────────────────────────
  • Line items table: sl.no, description, HSN/SAC, quantity, rate, amount
    — Capture the HSN/SAC EXACTLY as printed; do NOT correct or infer.
    — If a line prints "6205.20.00" but the PO says "6109.10", capture "6205.20.00".
  • Weight Statement table (often a separate sub-table at the bottom):
    Line | Net Wt (KGS) | Gross Wt (KGS) — capture per line.
  • Carton count per line (from either the weight table or a separate column).
  • Header metadata: Invoice No, Date, Buyer PO ref, Port of Loading,
    Port of Discharge, Incoterm, Currency, GSTIN, IEC, LUT ARN.

════════════════════════════════════════════════════════════
SEMANTIC ALIGNMENT RULES
════════════════════════════════════════════════════════════

Match each Invoice line to its corresponding PO line by semantic product
similarity, NOT by position.  Examples of correct matches:

  PO description                              → Invoice description
  ──────────────────────────────────────────────────────────────────
  "Men's 100% Combed Cotton Crewneck Tee"    → "100% COTTON MENS ROUND NECK T-SHIRT"
  "Women's Pique Polo Shirt"                 → "LADIES COTTON PIQUE COLLAR T-SHIRT"
  "Heavyweight Fleece Pullover Hoodie"       → "FLEECE PULLOVER HOODIE HEAVY"

Return inv_lines in the SAME ORDER as their matched po_lines (i.e., inv_lines[0]
corresponds to po_lines[0], inv_lines[1] to po_lines[1], etc.).

════════════════════════════════════════════════════════════
PACKING LIST RULES
════════════════════════════════════════════════════════════

Extract pl_lines in the same order as inv_lines.
Capture: description, cartons (ctns), net_wt_kg, gross_wt_kg per line.

════════════════════════════════════════════════════════════
OUTPUT FORMAT
════════════════════════════════════════════════════════════

Return a single valid JSON object conforming to the ExtractedShipmentPacket
Pydantic schema.  Do NOT include markdown fences, commentary, or any text
outside the JSON object.
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
