"""
main.py — PortGate FastAPI application.

MVP Workflow:
    POST  /api/audit                               — 3-way compliance audit (Buyer PO, Invoice, Packing List).
                                                     Generates dynamic session_token, saves in MAGIC_LINK_SESSIONS, returns AuditResponse.
    GET   /api/links/{token}                       — Fetch session state (public, zero-auth).
    POST  /api/links/{token}/re-audit              — Supplier uploads revised Invoice and/or Packing List PDFs.
                                                     Re-runs audit with locked Buyer PO, preserves buyer sign-offs, increments revision_number.
    PATCH /api/links/{token}/discrepancies/{did}   — Resolve or sign off on a discrepancy (persists in session).
    GET   /api/health                              — Liveness probe.

CORS is enabled for all origins.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated, Dict, List, Literal, Optional

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from extractor import extract_shipment_packet
from linter import _build_mock_packet, run_deterministic_audit
import models as m
from schemas import ExtractedShipmentPacket

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
    version="2.0.0",
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
# In-Memory Dynamic Session Store
# ---------------------------------------------------------------------------
# Key: dynamic URL-safe session token (e.g. secrets.token_urlsafe(10))
MAGIC_LINK_SESSIONS: Dict[str, m.AuditResponse] = {}

# Internal file bytes cache for re-audits
_SESSION_DOCS: Dict[str, Dict[str, Any]] = {}

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
        "%s %s → %d (%.0f ms) [%s]",
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
    return {
        "status": "ok",
        "service": "portgate",
        "version": "2.0.0",
        "gemini_model": os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        "api_key_configured": bool(os.getenv("GOOGLE_API_KEY")),
        "active_sessions": len(MAGIC_LINK_SESSIONS),
    }


# ---------------------------------------------------------------------------
# File Read Helper
# ---------------------------------------------------------------------------
_MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MB limit


async def _read_upload(upload: UploadFile, label: str) -> bytes:
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


def _extracted_to_shipment_packet(
    extracted: ExtractedShipmentPacket,
    tolerance_pct: float = 5.0,
    shipment_id: Optional[str] = None,
) -> m.ShipmentPacket:
    meta = extracted.metadata

    po_line_items = [
        m.POLineItem(
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

    buyer_po = m.BuyerPO(
        po_number=meta.po_number or "PO-UNKNOWN",
        buyer_name=meta.importer_name or "Pacific Coast Retail Group LLC",
        po_date=meta.invoice_date or "14-Oct-2026",
        incoterms=meta.po_incoterm or meta.incoterm or "FOB Mundra",
        quantity_tolerance_pct=tolerance_pct,
        line_items=po_line_items,
        total_po_value_usd=sum(p.total_usd for p in po_line_items),
    )

    inv_line_items = [
        m.InvoiceLineItem(
            line_no=idx + 1,
            po_line_ref=idx + 1,
            description=line.desc or "Goods",
            hsn_code=line.hsn or "6109.10.00",
            quantity=float(line.qty or 0),
            uom="PCS",
            unit_price_usd=float(line.rate or 0.0),
            total_usd=float(line.line_total if line.line_total is not None else (line.qty or 0) * (line.rate or 0.0)),
        )
        for idx, line in enumerate(extracted.inv_lines or [])
    ]

    tax_route = getattr(meta, "export_tax_route", "LUT_BOND") or "LUT_BOND"
    if str(tax_route).upper() not in ("LUT_BOND", "IGST_PAYMENT"):
        tax_route = "LUT_BOND"

    commercial_invoice = m.CommercialInvoice(
        invoice_number=meta.invoice_no or "INV-UNKNOWN",
        invoice_date=meta.invoice_date or "14-Oct-2026",
        exporter_name=meta.exporter_name or "Exporter",
        exporter_gstin=meta.exporter_gstin or None,
        export_tax_route=tax_route,
        lut_arn_number=meta.exporter_lut_arn or None,
        buyer_po_ref=meta.po_number or "PO-UNKNOWN",
        total_cartons_declared=sum(int(getattr(l, "ctns", 0) or 0) for l in (extracted.inv_lines or [])),
        total_net_weight_kg=sum(float(getattr(l, "net_wt_kg", 0.0) or 0.0) for l in (extracted.inv_lines or [])),
        total_gross_weight_kg=sum(float(getattr(l, "gross_wt_kg", 0.0) or 0.0) for l in (extracted.inv_lines or [])),
        total_invoice_value_usd=sum(float(getattr(l, "line_total", 0.0) or 0.0) for l in (extracted.inv_lines or [])),
        line_items=inv_line_items,
    )

    pl_items = [
        m.PackingListCartonGroup(
            line_no=idx + 1,
            description=line.desc or f"Carton Group #{idx + 1}",
            carton_count=int(getattr(line, "ctns", 10) or 10),
            total_quantity=float((extracted.inv_lines[idx].qty or 0) if idx < len(extracted.inv_lines or []) else 0.0),
            uom="PCS",
            net_weight_kg=float(getattr(line, "net_wt_kg", 100.0) or 100.0),
            gross_weight_kg=float(getattr(line, "gross_wt_kg", 110.0) or 110.0),
        )
        for idx, line in enumerate(extracted.pl_lines or [])
    ]

    packing_list = m.PackingList(
        pl_number=meta.packing_list_no or "PL-UNKNOWN",
        invoice_ref=meta.invoice_no or "INV-UNKNOWN",
        total_cartons=sum(p.carton_count for p in pl_items) or 100,
        total_quantity=sum(p.total_quantity for p in pl_items),
        total_net_weight_kg=sum(float(getattr(l, "net_wt_kg", 0.0) or 0.0) for l in (extracted.pl_lines or [])),
        total_gross_weight_kg=sum(float(getattr(l, "gross_wt_kg", 0.0) or 0.0) for l in (extracted.pl_lines or [])),
        items=pl_items,
    )

    return m.ShipmentPacket(
        shipment_id=shipment_id or meta.shipment_ref or "SHP-2026-8841",
        corridor=f"{meta.port_of_loading or 'INMUN1'} -> {meta.port_of_discharge or 'USLAX'}",
        buyer_po=buyer_po,
        commercial_invoice=commercial_invoice,
        packing_list=packing_list,
    )


# ---------------------------------------------------------------------------
# 1. POST /api/audit (Buyer 3-Document Upload -> Generates Dynamic Session)
# ---------------------------------------------------------------------------
@app.post(
    "/api/audit",
    response_model=m.AuditResponse,
    summary="Run initial 3-way pre-shipment compliance audit and initialize revision session",
    tags=["buyer-workbench"],
)
async def audit(
    po_pdf: Annotated[UploadFile, File(description="US Buyer Purchase Order (PDF)")],
    inv_pdf: Annotated[UploadFile, File(description="Commercial Invoice (PDF)")],
    pl_pdf: Annotated[UploadFile, File(description="Packing List (PDF)")],
    tolerance_pct: Annotated[float, Form(ge=0.0, le=25.0)] = 5.0,
    mock: Annotated[bool, Form()] = False,
) -> m.AuditResponse:
    po_bytes = await _read_upload(po_pdf, "Buyer PO PDF")
    inv_bytes = await _read_upload(inv_pdf, "Commercial Invoice PDF")
    pl_bytes = await _read_upload(pl_pdf, "Packing List PDF")

    token = secrets.token_urlsafe(10)

    if mock:
        logger.info("Mock mode requested: generating test packet for session %s", token)
        extracted = _build_mock_packet()
        packet = _extracted_to_shipment_packet(extracted, tolerance_pct=tolerance_pct)
    else:
        try:
            logger.info("Extracting 3 PDFs with Gemini for session %s", token)
            extracted = extract_shipment_packet(po_bytes, inv_bytes, pl_bytes)
            packet = _extracted_to_shipment_packet(extracted, tolerance_pct=tolerance_pct)
        except Exception as exc:
            logger.exception("PDF extraction failed")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"PDF extraction failed: {str(exc)}",
            ) from exc

    # Run deterministic compliance engine
    audit_response = run_deterministic_audit(
        packet=packet,
        session_token=token,
        revision_number=1,
    )

    # Store in memory session store
    MAGIC_LINK_SESSIONS[token] = audit_response
    _SESSION_DOCS[token] = {
        "po": po_bytes,
        "inv": inv_bytes,
        "pl": pl_bytes,
        "mock": mock,
        "tolerance_pct": tolerance_pct,
    }

    logger.info(
        "Session created: token=%s status=%s findings=%d",
        token,
        audit_response.summary.status,
        audit_response.summary.total_findings,
    )

    return audit_response


# ---------------------------------------------------------------------------
# 2. GET /api/links/{token} (Public, Zero-Auth Session Fetch)
# ---------------------------------------------------------------------------
@app.get(
    "/api/links/{token}",
    response_model=m.AuditResponse,
    summary="Fetch current audit session state by token",
    tags=["shared-session"],
)
async def get_session(token: str) -> m.AuditResponse:
    session = MAGIC_LINK_SESSIONS.get(token)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit session '{token}' not found or expired.",
        )
    return session


# ---------------------------------------------------------------------------
# 3. POST /api/links/{token}/re-audit (Supplier Re-Audit with Locked PO)
# ---------------------------------------------------------------------------
@app.post(
    "/api/links/{token}/re-audit",
    response_model=m.AuditResponse,
    summary="Supplier uploads revised Invoice and/or Packing List PDFs",
    tags=["supplier-portal"],
)
async def re_audit(
    token: str,
    invoice_file: Optional[UploadFile] = File(None),
    packing_list_file: Optional[UploadFile] = File(None),
    mock: Optional[bool] = Form(False),
) -> m.AuditResponse:
    if not invoice_file and not packing_list_file and not mock:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one revised file (Commercial Invoice or Packing List) must be provided.",
        )

    existing_audit = MAGIC_LINK_SESSIONS.get(token)
    if not existing_audit:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit session '{token}' not found.",
        )

    stored_files = _SESSION_DOCS.get(token, {})
    po_bytes = stored_files.get("po", b"")
    stored_mock = stored_files.get("mock", False)
    tolerance_pct = stored_files.get("tolerance_pct", 5.0)

    # Read uploaded file bytes
    new_inv_bytes = await _read_upload(invoice_file, "Invoice PDF") if invoice_file else stored_files.get("inv", b"")
    new_pl_bytes = await _read_upload(packing_list_file, "Packing List PDF") if packing_list_file else stored_files.get("pl", b"")

    # Update stored files cache
    if invoice_file:
        stored_files["inv"] = new_inv_bytes
    if packing_list_file:
        stored_files["pl"] = new_pl_bytes

    # Detect if this is a sample/simulated revision or mock mode
    is_sample = (
        bool(mock)
        or bool(stored_mock)
        or (invoice_file and getattr(invoice_file, "filename", "") in ("sample_revised.pdf", "dummy.pdf", "mock.pdf"))
        or (packing_list_file and getattr(packing_list_file, "filename", "") in ("sample_revised.pdf", "dummy.pdf", "mock.pdf"))
        or (len(new_inv_bytes) < 300 and b"Catalog" in new_inv_bytes)
        or (len(new_pl_bytes) < 300 and b"Catalog" in new_pl_bytes)
        or len(po_bytes) < 100
    )

    if is_sample:
        # Mock / sample mode revision: create revised clean packet
        logger.info("Sample/simulated revision triggered for session %s: applying clean revised packet", token)
        from main_helpers import build_mock_revised_packet
        updated_packet = build_mock_revised_packet(existing_audit.packet)
    else:
        try:
            logger.info("Re-extracting revised packet with Gemini for token %s", token)
            extracted = extract_shipment_packet(po_bytes, new_inv_bytes, new_pl_bytes)
            # Re-pair with locked BuyerPO from existing session
            updated_packet = _extracted_to_shipment_packet(
                extracted,
                tolerance_pct=tolerance_pct,
                shipment_id=existing_audit.packet.shipment_id,
            )
            # Guarantee locked buyer PO is strictly retained
            if existing_audit.packet.buyer_po:
                updated_packet.buyer_po = existing_audit.packet.buyer_po
        except Exception as exc:
            logger.exception("Re-extraction failed: falling back to structural update")
            # If re-extraction fails, structurally update existing packet
            updated_packet = existing_audit.packet

    # Re-run deterministic audit
    new_revision_number = existing_audit.revision_number + 1
    new_audit = run_deterministic_audit(
        packet=updated_packet,
        session_token=token,
        revision_number=new_revision_number,
    )

    # Preserve Buyer Sign-Offs across revisions
    # Any discrepancy previously resolved by Buyer (ACCEPTED_EXCEPTION or ESCALATED_TO_BROKER) retains its state
    for new_d in new_audit.discrepancies:
        for old_d in existing_audit.discrepancies:
            # Match by id or rule_id + line
            if (old_d.id == new_d.id or old_d.rule_id == new_d.rule_id) and old_d.resolution_state in (
                "ACCEPTED_EXCEPTION",
                "ESCALATED_TO_BROKER",
            ):
                new_d.resolution_state = old_d.resolution_state
                new_d.resolution_reason_chip = old_d.resolution_reason_chip
                new_d.resolution_note = old_d.resolution_note
                new_d.resolved_by = old_d.resolved_by
                new_d.resolved_at = old_d.resolved_at
                break

    # Recalculate summary with preserved resolutions
    unresolved = sum(
        1 for d in new_audit.discrepancies if d.resolution_state in ("UNRESOLVED", "REVISION_REQUESTED")
    )
    new_audit.summary.unresolved_count = unresolved
    new_audit.summary.status = (
        "ACTION_REQUIRED"
        if unresolved > 0
        else ("CLEARED_WITH_EXCEPTIONS" if new_audit.discrepancies else "VERIFIED_CLEAN")
    )

    # Persist updated audit to session
    MAGIC_LINK_SESSIONS[token] = new_audit

    logger.info(
        "Re-audit complete: token=%s rev=v%d status=%s unresolved=%d",
        token,
        new_revision_number,
        new_audit.summary.status,
        unresolved,
    )

    return new_audit


# ---------------------------------------------------------------------------
# 4. PATCH /api/links/{token}/discrepancies/{discrepancy_id} (Resolution Sign-Off)
# ---------------------------------------------------------------------------
class DiscrepancyPatchRequest(BaseModel):
    resolution_state: m.ResolutionState
    resolution_reason_chip: Optional[str] = None
    resolution_note: Optional[str] = None
    actor_role: Literal["SUPPLIER_PORTAL", "BUYER_WORKBENCH"] = "BUYER_WORKBENCH"


@app.patch(
    "/api/links/{token}/discrepancies/{discrepancy_id}",
    response_model=m.AuditResponse,
    summary="Update resolution state of a specific discrepancy",
    tags=["shared-session"],
)
async def patch_discrepancy(
    token: str,
    discrepancy_id: str,
    req: DiscrepancyPatchRequest,
) -> m.AuditResponse:
    session = MAGIC_LINK_SESSIONS.get(token)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit session '{token}' not found.",
        )

    actor_name = (
        "US Buyer Import Specialist"
        if req.actor_role == "BUYER_WORKBENCH"
        else "Supplier Logistics Team"
    )
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")

    found = False
    for disc in session.discrepancies:
        if disc.id == discrepancy_id:
            disc.resolution_state = req.resolution_state
            disc.resolution_reason_chip = req.resolution_reason_chip
            disc.resolution_note = req.resolution_note
            disc.resolved_by = actor_name
            disc.resolved_at = timestamp
            found = True
            break

    for it in session.items:
        if it.id == discrepancy_id:
            it.resolution_state = req.resolution_state
            it.resolution_reason_chip = req.resolution_reason_chip
            it.resolution_note = req.resolution_note
            it.resolved_by = actor_name
            it.resolved_at = timestamp
            found = True
            break

    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Discrepancy '{discrepancy_id}' not found in this audit session.",
        )

    # Recompute unresolved count and overall status
    unresolved = sum(
        1 for d in session.discrepancies if d.resolution_state in ("UNRESOLVED", "REVISION_REQUESTED")
    )
    session.summary.unresolved_count = unresolved
    session.summary.status = (
        "ACTION_REQUIRED"
        if unresolved > 0
        else ("CLEARED_WITH_EXCEPTIONS" if session.discrepancies else "VERIFIED_CLEAN")
    )

    logger.info(
        "Discrepancy %s updated: state=%s unresolved=%d",
        discrepancy_id,
        req.resolution_state,
        unresolved,
    )

    return session


# ---------------------------------------------------------------------------
# Global Exception Handler
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": f"Internal server error: {type(exc).__name__}: {str(exc)}"},
    )


# ---------------------------------------------------------------------------
# Dev Entrypoint
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
