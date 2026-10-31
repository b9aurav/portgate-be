"""
main.py — PortGate FastAPI application.

Endpoints:
    GET  /api/health                               — Liveness probe
    POST /api/audit                                — 3-way compliance audit (3 PDF uploads)

    POST /api/links/create                         — Create a magic-link session from a Buyer PO
    GET  /api/links/{token}                        — Fetch session state (public, zero-auth)
    POST /api/links/{token}/audit-files            — Supplier uploads Invoice + PL PDFs
    POST /api/links/{token}/simulate-upload        — Simulate upload with built-in Tally packets
    PATCH /api/links/{token}/discrepancies/{did}   — Update a single discrepancy resolution

CORS is enabled for all origins (configure ALLOWED_ORIGINS in production).

Environment variables (.env):
    GOOGLE_API_KEY    — Required for live PDF extraction via Gemini
    GEMINI_MODEL      — Optional, defaults to gemini-3.8-flash-preview-05-20
    ALLOWED_ORIGINS   — Comma-separated list of allowed CORS origins
                        (defaults to "*" for development)
    LOG_LEVEL         — Uvicorn log level (default: info)

Run:
    uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from extractor import extract_shipment_packet
from linter import _build_mock_packet, run_audit, run_deterministic_audit
import models as m
from schemas import AuditResponse
from sessions import MAGIC_LINK_SESSIONS

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("portgate")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="PortGate",
    summary="Pre-Shipment 3-Way Customs Compliance Engine — India→US Corridor",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
_raw_origins = os.getenv("ALLOWED_ORIGINS", "*")
allowed_origins: list[str] = (
    ["*"] if _raw_origins.strip() == "*"
    else [o.strip() for o in _raw_origins.split(",") if o.strip()]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request-ID middleware for traceability
# ---------------------------------------------------------------------------
@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = str(uuid.uuid4())[:8]
    request.state.request_id = request_id
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    response.headers["X-Request-Id"] = request_id
    response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.1f}"
    logger.info(
        "%s %s → %d  (%.0f ms) [%s]",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
        request_id,
    )
    return response


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------
@app.get(
    "/api/health",
    summary="Liveness probe",
    tags=["ops"],
)
async def health():
    """Return 200 OK with service metadata. Use as a Kubernetes / ELB probe."""
    return {
        "status": "ok",
        "service": "portgate",
        "version": "1.0.0",
        "gemini_model": os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        "api_key_configured": bool(os.getenv("GOOGLE_API_KEY")),
    }


# ---------------------------------------------------------------------------
# Audit endpoint (legacy 3-way PDF upload)
# ---------------------------------------------------------------------------
_MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MB per document


async def _read_upload(upload: UploadFile, label: str) -> bytes:
    """Read an uploaded file into bytes, enforcing the size limit."""
    data = await upload.read()
    if len(data) > _MAX_FILE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"{label} exceeds the {_MAX_FILE_BYTES // (1024*1024)} MB limit.",
        )
    if not data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{label} is empty.",
        )
    return data


@app.post(
    "/api/audit",
    response_model=AuditResponse,
    summary="Run 3-way pre-shipment compliance audit",
    tags=["compliance"],
    responses={
        200: {"description": "Audit completed — see response for CLEAR or BLOCKED status"},
        400: {"description": "Malformed request or unsupported file type"},
        422: {"description": "Validation error (missing field, empty file, etc.)"},
        500: {"description": "LLM extraction failure or internal error"},
        503: {"description": "Gemini API key not configured — mock mode suggested"},
    },
)
async def audit(
    po_pdf: Annotated[
        UploadFile,
        File(description="US Buyer Purchase Order (PDF)"),
    ],
    inv_pdf: Annotated[
        UploadFile,
        File(description="Indian Exporter Tally Prime Commercial Invoice (PDF)"),
    ],
    pl_pdf: Annotated[
        UploadFile,
        File(description="Exporter Packing List (PDF)"),
    ],
    tolerance_pct: Annotated[
        float,
        Form(
            ge=0.0,
            le=25.0,
            description=(
                "Split-shipment over-quantity tolerance in percent. "
                "Default 5.0. Quantities within this band emit WARN; "
                "above it emit CRIT."
            ),
        ),
    ] = 5.0,
    mock: Annotated[
        bool,
        Form(
            description=(
                "If true, skip PDF extraction and run the deterministic "
                "engine against the built-in sabotaged mock packet. "
                "Useful for UI wiring and integration testing without "
                "consuming Gemini tokens."
            ),
        ),
    ] = False,
) -> AuditResponse:
    """
    Accept three PDF documents and return a full compliance audit report.

    The pipeline is:
    1. **Extract** — Gemini multimodal LLM reads the PDFs and returns a
       structured `ExtractedShipmentPacket` (semantic alignment included).
    2. **Lint** — The deterministic Python rules engine evaluates all 5
       rule categories and produces discrepancy items + passed checks.
    3. **Respond** — The validated `AuditResponse` is serialised as JSON.

    Set `mock=true` in the form body to bypass Gemini and test the engine
    against the built-in golden dataset immediately.
    """
    if mock:
        logger.info("Mock mode: using built-in sabotaged packet")
        packet = _build_mock_packet()
    else:
        # Validate content type (loose check — Gemini will error on non-PDFs)
        for upload, label in [
            (po_pdf, "PO PDF"),
            (inv_pdf, "Invoice PDF"),
            (pl_pdf, "Packing List PDF"),
        ]:
            ct = upload.content_type or ""
            if ct and "pdf" not in ct.lower() and "octet-stream" not in ct.lower():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"{label}: expected application/pdf, got '{ct}'. "
                        "Please upload PDF files only."
                    ),
                )

        logger.info("Reading uploaded PDFs...")
        po_bytes = await _read_upload(po_pdf, "PO PDF")
        inv_bytes = await _read_upload(inv_pdf, "Invoice PDF")
        pl_bytes = await _read_upload(pl_pdf, "Packing List PDF")

        logger.info(
            "Sending to Gemini: PO=%d bytes, Inv=%d bytes, PL=%d bytes",
            len(po_bytes),
            len(inv_bytes),
            len(pl_bytes),
        )

        try:
            packet = extract_shipment_packet(po_bytes, inv_bytes, pl_bytes)
        except RuntimeError as exc:
            # Gemini API key not set or network error
            logger.error("Extraction failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"PDF extraction failed: {exc}. "
                    "Set GOOGLE_API_KEY in your .env file, or pass mock=true "
                    "to test with the built-in packet."
                ),
            ) from exc
        except Exception as exc:
            logger.exception("Unexpected extraction error")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Unexpected extraction error: {type(exc).__name__}: {exc}",
            ) from exc

    logger.info("Running deterministic compliance engine (tolerance=%.1f%%)", tolerance_pct)
    result = run_audit(packet, tolerance_pct=tolerance_pct)

    logger.info(
        "Audit complete — %s | crit=%d warn=%d passed=%d | MID=%s",
        result.summary.status,
        result.summary.critical_count,
        result.summary.warning_count,
        result.summary.passed_count,
        result.mid_code,
    )
    return result


# ===========================================================================
# Magic Link Session API
# ===========================================================================

# ---------------------------------------------------------------------------
# Pydantic request/response models for magic-link endpoints
# ---------------------------------------------------------------------------

class CreateLinkRequest(BaseModel):
    buyer_po: m.BuyerPO
    exporter_name: str
    shipment_id: Optional[str] = None
    corridor: Optional[str] = "IN-MUN → US-LAX"


class MagicLinkSessionOut(BaseModel):
    token: str
    shipment_id: str
    corridor: str
    exporter_name: str
    buyer_po: m.BuyerPO
    latest_audit: Optional[m.AuditResponse] = None
    created_at: str
    submission_status: str


class CreateLinkResponse(BaseModel):
    token: str
    magic_url: str
    session: MagicLinkSessionOut


class DiscrepancyPatchRequest(BaseModel):
    resolution_state: m.ResolutionState
    resolution_reason_chip: Optional[str] = None
    resolution_note: Optional[str] = None
    actor_role: Literal["SUPPLIER_PORTAL", "BUYER_WORKBENCH"] = "BUYER_WORKBENCH"


def _session_to_out(session: dict) -> MagicLinkSessionOut:
    """Convert an internal session dict to the Pydantic response model."""
    return MagicLinkSessionOut(
        token=session["token"],
        shipment_id=session["shipment_id"],
        corridor=session["corridor"],
        exporter_name=session["exporter_name"],
        buyer_po=session["buyer_po"],
        latest_audit=session.get("latest_audit"),
        created_at=session["created_at"],
        submission_status=session["submission_status"],
    )


# ---------------------------------------------------------------------------
# POST /api/links/create
# ---------------------------------------------------------------------------

@app.post(
    "/api/links/create",
    response_model=CreateLinkResponse,
    summary="Create a magic-link upload session from a Buyer PO",
    tags=["magic-links"],
)
async def create_magic_link(req: CreateLinkRequest) -> CreateLinkResponse:
    """
    Generate a short URL-safe token, store the Buyer PO in the session,
    and return the supplier upload URL.
    """
    raw_token = secrets.token_urlsafe(6)  # ~8 chars
    # Prefix with exporter slug for readability
    slug = req.exporter_name.split()[0].lower()[:6]
    token = f"{slug}-{raw_token}"
    shipment_id = req.shipment_id or f"SHP-{datetime.now(timezone.utc).strftime('%Y-%m%d')}-{raw_token[:4].upper()}"

    session: dict = {
        "token": token,
        "shipment_id": shipment_id,
        "corridor": req.corridor or "IN-MUN → US-LAX",
        "exporter_name": req.exporter_name,
        "buyer_po": req.buyer_po,
        "latest_audit": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "submission_status": "AWAITING_SUPPLIER_UPLOAD",
    }
    MAGIC_LINK_SESSIONS[token] = session

    logger.info("Magic link created: token=%s shipment=%s", token, shipment_id)
    return CreateLinkResponse(
        token=token,
        magic_url=f"/upload/{token}",
        session=_session_to_out(session),
    )


# ---------------------------------------------------------------------------
# GET /api/links/{token}
# ---------------------------------------------------------------------------

@app.get(
    "/api/links/{token}",
    response_model=MagicLinkSessionOut,
    summary="Fetch session state for a magic-link token (public, zero-auth)",
    tags=["magic-links"],
)
async def get_magic_link(token: str) -> MagicLinkSessionOut:
    """
    Returns session metadata including `buyer_po`, `submission_status`,
    and `latest_audit`. Used by both Supplier Portal (on mount) and
    Buyer Workbench (polling / refresh).
    """
    session = MAGIC_LINK_SESSIONS.get(token)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Magic-link token '{token}' not found or expired.",
        )
    return _session_to_out(session)


# ---------------------------------------------------------------------------
# POST /api/links/{token}/audit-files
# ---------------------------------------------------------------------------

@app.post(
    "/api/links/{token}/audit-files",
    response_model=m.AuditResponse,
    summary="Supplier uploads Invoice + Packing List PDFs against a magic-link token",
    tags=["magic-links"],
)
async def audit_files_for_token(
    token: str,
    invoice_file: Annotated[UploadFile, File(description="Tally Commercial Invoice PDF")],
    packing_list_file: Annotated[UploadFile, File(description="Packing List PDF")],
) -> m.AuditResponse:
    """
    Accepts Invoice and Packing List PDFs (the Buyer PO is pre-loaded in the session).
    Extracts the packet, attaches the session's BuyerPO, runs the deterministic audit,
    saves the result, and returns the AuditResponse.
    """
    session = MAGIC_LINK_SESSIONS.get(token)
    if not session:
        raise HTTPException(status_code=404, detail=f"Token '{token}' not found.")

    inv_bytes = await _read_upload(invoice_file, "Invoice PDF")
    pl_bytes = await _read_upload(packing_list_file, "Packing List PDF")

    # Build a minimal dummy PO PDF bytes (the PO is already in the session)
    dummy_po_bytes = (
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n"
        b"xref\n0 4\n0000000000 65535 f \ntrailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n190\n%%EOF"
    )

    try:
        extracted = extract_shipment_packet(dummy_po_bytes, inv_bytes, pl_bytes)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"PDF extraction failed: {exc}. Check GOOGLE_API_KEY.",
        ) from exc

    # Build ShipmentPacket using the session's pre-loaded BuyerPO
    buyer_po: m.BuyerPO = session["buyer_po"]

    # Map extracted lines → models.CommercialInvoice
    inv_line_items = [
        m.InvoiceLineItem(
            line_no=idx + 1,
            description=line.desc,
            hsn_code=line.hsn,
            quantity=float(line.qty),
            uom="PCS",
            unit_price_usd=line.rate,
            total_usd=line.line_total,
        )
        for idx, line in enumerate(extracted.inv_lines)
    ]

    pl_items = [
        m.PackingListCartonGroup(
            line_no=idx + 1,
            description=line.desc,
            # ExtractedPLLine has no ctns field; use the matching inv_line's ctns count
            carton_count=extracted.inv_lines[idx].ctns if idx < len(extracted.inv_lines) else 0,
            total_quantity=float(extracted.inv_lines[idx].qty) if idx < len(extracted.inv_lines) else 0,
            uom="PCS",
            net_weight_kg=line.net_wt_kg,
            gross_weight_kg=line.gross_wt_kg,
        )
        for idx, line in enumerate(extracted.pl_lines)
    ]

    meta = extracted.metadata
    commercial_invoice = m.CommercialInvoice(
        invoice_number=meta.invoice_no,
        invoice_date=meta.invoice_date,
        exporter_name=meta.exporter_name,
        exporter_gstin=meta.exporter_gstin or None,
        export_tax_route="LUT_BOND" if meta.exporter_lut_arn else "UNKNOWN",
        lut_arn_number=meta.exporter_lut_arn or None,
        buyer_po_ref=meta.po_number,
        total_cartons_declared=sum(line.ctns for line in extracted.inv_lines),
        total_net_weight_kg=sum(line.net_wt_kg for line in extracted.inv_lines),
        total_gross_weight_kg=sum(line.gross_wt_kg for line in extracted.inv_lines),
        total_invoice_value_usd=sum(line.line_total for line in extracted.inv_lines),
        line_items=inv_line_items,
    )

    packing_list = m.PackingList(
        pl_number=meta.packing_list_no,
        invoice_ref=meta.invoice_no,
        total_cartons=sum(line.ctns for line in extracted.inv_lines),  # PL ctns from invoice (PL lines have no ctns field)
        total_quantity=sum(float(inv_line_items[i].quantity) for i in range(len(pl_items))),
        total_net_weight_kg=sum(line.net_wt_kg for line in extracted.pl_lines),
        total_gross_weight_kg=sum(line.gross_wt_kg for line in extracted.pl_lines),
        items=pl_items,
    )

    packet = m.ShipmentPacket(
        shipment_id=session["shipment_id"],
        corridor=session["corridor"],
        buyer_po=buyer_po,
        commercial_invoice=commercial_invoice,
        packing_list=packing_list,
    )

    audit_result = run_deterministic_audit(packet)

    # Persist result in session
    session["latest_audit"] = audit_result
    session["submission_status"] = (
        "DISCREPANCIES_FLAGGED"
        if audit_result.summary.unresolved_count > 0
        else "SUBMITTED_FOR_BUYER_REVIEW"
    )

    logger.info(
        "Magic-link audit complete: token=%s status=%s findings=%d",
        token,
        audit_result.summary.status,
        audit_result.summary.total_findings,
    )
    return audit_result


# ---------------------------------------------------------------------------
# POST /api/links/{token}/simulate-upload
# ---------------------------------------------------------------------------

@app.post(
    "/api/links/{token}/simulate-upload",
    response_model=m.AuditResponse,
    summary="Simulate a supplier upload using built-in Tally packet fixtures",
    tags=["magic-links"],
)
async def simulate_upload(
    token: str,
    version: Annotated[
        Literal["v1_broken", "v2_revised"],
        Form(description="v1_broken = sabotaged packet with all errors; v2_revised = corrected packet"),
    ] = "v1_broken",
) -> m.AuditResponse:
    """
    Runs the deterministic audit engine against the session's pre-loaded BuyerPO
    and either the v1 sabotaged or v2 corrected Tally Invoice/PL fixture.
    No PDF upload required — ideal for live demos in incognito.
    """
    session = MAGIC_LINK_SESSIONS.get(token)
    if not session:
        raise HTTPException(status_code=404, detail=f"Token '{token}' not found.")

    buyer_po: m.BuyerPO = session["buyer_po"]

    if version == "v1_broken":
        packet = _build_tally_mock_v1(buyer_po, session["shipment_id"], session["corridor"])
    else:
        packet = _build_tally_mock_v2(buyer_po, session["shipment_id"], session["corridor"])

    audit_result = run_deterministic_audit(packet)

    session["latest_audit"] = audit_result
    session["submission_status"] = (
        "DISCREPANCIES_FLAGGED"
        if audit_result.summary.unresolved_count > 0
        else "SUBMITTED_FOR_BUYER_REVIEW"
    )

    logger.info(
        "Simulate upload: token=%s version=%s status=%s findings=%d",
        token,
        version,
        audit_result.summary.status,
        audit_result.summary.total_findings,
    )
    return audit_result


def _build_tally_mock_v1(
    buyer_po: m.BuyerPO,
    shipment_id: str,
    corridor: str,
) -> m.ShipmentPacket:
    """
    Sabotaged Tally Invoice + PL against the buyer_po:
      RULE-D01: carton mismatch (inv 98 vs pl 108)
      RULE-D02: weight inversion on PL (net > gross)
      RULE-D04: UOM conflict on Line 3 (Sets vs PCS)
      RULE-D05: line-total arithmetic error on Line 4 ($4,850 printed vs 500×$9.50=$4,750)
      RULE-V01: unit price overcharge on Line 2 (+$0.40/pc)
      RULE-C01: HSN root divergence on Line 1
    """
    inv_lines = [
        m.InvoiceLineItem(
            line_no=1,
            po_line_ref=1,
            description="100% COTTON MENS ROUND NECK T-SHIRT (KNITTED) BATCH #44A",
            hsn_code="6205.20.00",          # ← RULE-C01: should be 6109.10
            quantity=2000.0,
            uom="PCS",
            unit_price_usd=4.50,
            total_usd=9000.00,
        ),
        m.InvoiceLineItem(
            line_no=2,
            po_line_ref=2,
            description="LADIES COTTON PIQUE COLLAR POLO SHIRT WHITE",
            hsn_code="6106.10.00",
            quantity=2000.0,
            uom="PCS",
            unit_price_usd=6.40,            # ← RULE-V01: PO says 6.00
            total_usd=12800.00,
        ),
        m.InvoiceLineItem(
            line_no=3,
            po_line_ref=3,
            description="FLEECE PULLOVER HOODIE HEAVY GREY XL",
            hsn_code="6110.20.00",
            quantity=1000.0,
            uom="SETS",                     # ← RULE-D04: PL uses PCS
            unit_price_usd=11.00,
            total_usd=11000.00,
        ),
        m.InvoiceLineItem(
            line_no=4,
            po_line_ref=4,
            description="KIDS COTTON PIQUE POLO SHIRT BLUE",
            hsn_code="6112.11.00",
            quantity=500.0,
            uom="PCS",
            unit_price_usd=9.50,
            total_usd=4850.00,              # ← RULE-D05: 500×9.50=4750 but printed 4850
        ),
    ]

    pl_items = [
        m.PackingListCartonGroup(
            line_no=1, description="MENS ROUND NECK T-SHIRT",
            carton_count=40, total_quantity=2000.0, uom="PCS",
            net_weight_kg=380.0, gross_weight_kg=420.0,
        ),
        m.PackingListCartonGroup(
            line_no=2, description="LADIES PIQUE POLO SHIRT WHITE",
            carton_count=30, total_quantity=2000.0, uom="PCS",
            net_weight_kg=410.0, gross_weight_kg=450.0,
        ),
        m.PackingListCartonGroup(
            line_no=3, description="FLEECE PULLOVER HOODIE GREY XL",
            carton_count=38, total_quantity=1000.0, uom="PCS",
            net_weight_kg=580.0, gross_weight_kg=620.0,
        ),
        m.PackingListCartonGroup(
            line_no=4, description="KIDS POLO SHIRT BLUE",
            carton_count=10, total_quantity=500.0, uom="PCS",
            net_weight_kg=85.0, gross_weight_kg=90.0,
        ),
    ]

    commercial_invoice = m.CommercialInvoice(
        invoice_number="SHR/EXP/26-27/0412",
        invoice_date="14-Oct-2026",
        exporter_name="Shreeji Textiles & Apparel Pvt. Ltd.",
        exporter_gstin="24AABCS1429B1Z5",
        export_tax_route="LUT_BOND",
        lut_arn_number="AD240426009182Z",
        buyer_po_ref=buyer_po.po_number,
        total_cartons_declared=98,           # ← RULE-D01: PL totals 118
        total_net_weight_kg=1455.0,
        total_gross_weight_kg=1580.0,
        total_invoice_value_usd=sum(l.total_usd for l in inv_lines),
        line_items=inv_lines,
    )

    packing_list = m.PackingList(
        pl_number="SHR/PL/0412",
        invoice_ref="SHR/EXP/26-27/0412",
        total_cartons=118,                   # ← RULE-D01: Invoice says 98
        total_quantity=5500.0,
        total_net_weight_kg=820.0,           # ← RULE-D02: net > gross
        total_gross_weight_kg=780.0,
        items=pl_items,
    )

    return m.ShipmentPacket(
        shipment_id=shipment_id,
        corridor=corridor,
        buyer_po=buyer_po,
        commercial_invoice=commercial_invoice,
        packing_list=packing_list,
    )


def _build_tally_mock_v2(
    buyer_po: m.BuyerPO,
    shipment_id: str,
    corridor: str,
) -> m.ShipmentPacket:
    """
    Corrected Tally Invoice + PL — all errors from v1 fixed.
    Should produce zero deterministic conflicts and zero commercial variances.
    """
    inv_lines = [
        m.InvoiceLineItem(
            line_no=1, po_line_ref=1,
            description="100% COTTON MENS ROUND NECK T-SHIRT (KNITTED) BATCH #44A",
            hsn_code="6109.10.00",           # ← corrected
            quantity=2000.0, uom="PCS",
            unit_price_usd=4.50, total_usd=9000.00,
        ),
        m.InvoiceLineItem(
            line_no=2, po_line_ref=2,
            description="LADIES COTTON PIQUE COLLAR POLO SHIRT WHITE",
            hsn_code="6106.10.00",
            quantity=2000.0, uom="PCS",
            unit_price_usd=6.00, total_usd=12000.00,  # ← corrected to PO price
        ),
        m.InvoiceLineItem(
            line_no=3, po_line_ref=3,
            description="FLEECE PULLOVER HOODIE HEAVY GREY XL",
            hsn_code="6110.20.00",
            quantity=1000.0, uom="PCS",      # ← corrected UOM
            unit_price_usd=11.00, total_usd=11000.00,
        ),
        m.InvoiceLineItem(
            line_no=4, po_line_ref=4,
            description="KIDS COTTON PIQUE POLO SHIRT BLUE",
            hsn_code="6112.11.00",
            quantity=500.0, uom="PCS",
            unit_price_usd=8.50, total_usd=4250.00,   # ← corrected: 500×8.50
        ),
    ]

    pl_items = [
        m.PackingListCartonGroup(
            line_no=1, description="MENS ROUND NECK T-SHIRT",
            carton_count=40, total_quantity=2000.0, uom="PCS",
            net_weight_kg=380.0, gross_weight_kg=420.0,
        ),
        m.PackingListCartonGroup(
            line_no=2, description="LADIES PIQUE POLO SHIRT WHITE",
            carton_count=30, total_quantity=2000.0, uom="PCS",
            net_weight_kg=410.0, gross_weight_kg=450.0,
        ),
        m.PackingListCartonGroup(
            line_no=3, description="FLEECE PULLOVER HOODIE GREY XL",
            carton_count=38, total_quantity=1000.0, uom="PCS",
            net_weight_kg=580.0, gross_weight_kg=620.0,
        ),
        m.PackingListCartonGroup(
            line_no=4, description="KIDS POLO SHIRT BLUE",
            carton_count=10, total_quantity=500.0, uom="PCS",
            net_weight_kg=85.0, gross_weight_kg=90.0,
        ),
    ]

    commercial_invoice = m.CommercialInvoice(
        invoice_number="SHR/EXP/26-27/0412-R",
        invoice_date="16-Oct-2026",
        exporter_name="Shreeji Textiles & Apparel Pvt. Ltd.",
        exporter_gstin="24AABCS1429B1Z5",
        export_tax_route="LUT_BOND",
        lut_arn_number="AD240426009182Z",
        buyer_po_ref=buyer_po.po_number,
        total_cartons_declared=118,          # ← matches PL
        total_net_weight_kg=1455.0,
        total_gross_weight_kg=1580.0,
        total_invoice_value_usd=sum(l.total_usd for l in inv_lines),
        line_items=inv_lines,
    )

    packing_list = m.PackingList(
        pl_number="SHR/PL/0412-R",
        invoice_ref="SHR/EXP/26-27/0412-R",
        total_cartons=118,                   # ← matches Invoice
        total_quantity=5500.0,
        total_net_weight_kg=1455.0,          # ← corrected: net < gross
        total_gross_weight_kg=1580.0,
        items=pl_items,
    )

    return m.ShipmentPacket(
        shipment_id=shipment_id,
        corridor=corridor,
        buyer_po=buyer_po,
        commercial_invoice=commercial_invoice,
        packing_list=packing_list,
    )


# ---------------------------------------------------------------------------
# PATCH /api/links/{token}/discrepancies/{discrepancy_id}
# ---------------------------------------------------------------------------

@app.patch(
    "/api/links/{token}/discrepancies/{discrepancy_id}",
    response_model=m.AuditResponse,
    summary="Resolve a single discrepancy in a magic-link session",
    tags=["magic-links"],
)
async def patch_discrepancy(
    token: str,
    discrepancy_id: str,
    req: DiscrepancyPatchRequest,
) -> m.AuditResponse:
    """
    Updates the `resolution_state`, `resolution_reason_chip`, and
    `resolution_note` for the specified discrepancy.  Recomputes
    `summary.status` and `summary.unresolved_count` and persists the
    updated AuditResponse back to the session.
    """
    session = MAGIC_LINK_SESSIONS.get(token)
    if not session:
        raise HTTPException(status_code=404, detail=f"Token '{token}' not found.")

    audit: Optional[m.AuditResponse] = session.get("latest_audit")
    if not audit:
        raise HTTPException(
            status_code=409,
            detail="No audit has been run for this session yet. Call /simulate-upload or /audit-files first.",
        )

    # Find and update the discrepancy
    updated_discrepancies = []
    found = False
    actor_name = (
        "US Buyer Import Specialist"
        if req.actor_role == "BUYER_WORKBENCH"
        else "Shreeji Logistics Team"
    )
    resolved_at = datetime.now(timezone.utc).strftime("%H:%M:%S")

    for disc in audit.discrepancies:
        if disc.id == discrepancy_id:
            found = True
            updated = disc.model_copy(
                update={
                    "resolution_state": req.resolution_state,
                    "resolution_reason_chip": req.resolution_reason_chip,
                    "resolution_note": req.resolution_note,
                    "resolved_by": actor_name,
                    "resolved_at": resolved_at,
                }
            )
            updated_discrepancies.append(updated)
        else:
            updated_discrepancies.append(disc)

    if not found:
        raise HTTPException(
            status_code=404,
            detail=f"Discrepancy '{discrepancy_id}' not found in this session's audit.",
        )

    # Recompute summary
    unresolved = sum(
        1 for d in updated_discrepancies if d.resolution_state == "UNRESOLVED"
    )
    new_status: m.AuditOverallStatus = (
        "ACTION_REQUIRED"
        if unresolved > 0
        else ("CLEARED_WITH_EXCEPTIONS" if updated_discrepancies else "VERIFIED_CLEAN")
    )

    updated_summary = audit.summary.model_copy(
        update={
            "status": new_status,
            "unresolved_count": unresolved,
        }
    )

    updated_audit = audit.model_copy(
        update={
            "discrepancies": updated_discrepancies,
            "summary": updated_summary,
        }
    )

    session["latest_audit"] = updated_audit
    session["submission_status"] = (
        "SUBMITTED_FOR_BUYER_REVIEW"
        if unresolved == 0
        else "DISCREPANCIES_FLAGGED"
    )

    logger.info(
        "Discrepancy patched: token=%s disc=%s state=%s unresolved=%d",
        token,
        discrepancy_id,
        req.resolution_state,
        unresolved,
    )
    return updated_audit


# ---------------------------------------------------------------------------
# Global exception handler (convert unhandled errors to JSON)
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": f"Internal server error: {type(exc).__name__}"},
    )


# ---------------------------------------------------------------------------
# Dev entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
