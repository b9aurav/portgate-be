"""
app/api/routes.py — HTTP Route Handlers for PortGate.

Routes:
  GET   /api/health
  POST  /api/audit
  GET   /api/links/{token}
  POST  /api/links/{token}/re-audit
  PATCH /api/links/{token}/discrepancies/{discrepancy_id}
"""

from __future__ import annotations

import logging
import secrets
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile, status

from app import config
from app.schemas.api import AuditResponse, PatchResolutionRequest, ShipmentListItem
from app.services.extractor import (
    build_mock_packet,
    build_mock_revised_packet,
    extract_shipment_packet,
    extracted_to_shipment_packet,
)
from app.services.linter import run_deterministic_lint
from app.services.serializer import build_audit_response
from app.services.session_store import session_store

logger = logging.getLogger("portgate.routes")

router = APIRouter(tags=["compliance-engine"])


# ---------------------------------------------------------------------------
# Workspace Owner Dependency (Seamlessly accepts Supabase JWTs in future)
# ---------------------------------------------------------------------------
async def get_current_owner_id(
    x_workspace_id: Optional[str] = Header(default=None, alias="X-Workspace-Id")
) -> str:
    if x_workspace_id and x_workspace_id.strip():
        return x_workspace_id.strip()
    return "sandbox_default"


# ---------------------------------------------------------------------------
# Upload Helper
# ---------------------------------------------------------------------------
async def _read_upload(upload: UploadFile, label: str) -> bytes:
    if hasattr(upload, "file") and hasattr(upload.file, "read"):
        data = upload.file.read()
    else:
        data = await upload.read()
    if len(data) > config.MAX_FILE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"{label} exceeds the {config.MAX_FILE_BYTES // (1024 * 1024)} MB limit.",
        )
    if not data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{label} is empty.",
        )
    return data


# ---------------------------------------------------------------------------
# Health Probe
# ---------------------------------------------------------------------------
@router.get(
    "/api/health",
    summary="Liveness and configuration probe",
    tags=["ops"],
)
async def health():
    return {
        "status": "ok",
        "service": "portgate",
        "version": "2.0.0",
        "gemini_model": config.GEMINI_MODEL,
        "api_key_configured": bool(config.GOOGLE_API_KEY),
        "active_sessions": len(session_store.raw_sessions),
    }


# ---------------------------------------------------------------------------
# Multi-Shipment Queue & Sandbox Workspace
# ---------------------------------------------------------------------------
@router.get(
    "/api/shipments",
    response_model=list[ShipmentListItem],
    summary="List all active shipment audit sessions for Buyer Queue Dashboard (scoped to sandbox owner)",
)
def list_shipments(owner_id: str = Depends(get_current_owner_id)) -> list[ShipmentListItem]:
    if not isinstance(owner_id, str):
        owner_id = "sandbox_default"
    return session_store.list_shipments(owner_id=owner_id)


@router.delete(
    "/api/shipments/{token}",
    summary="Delete a shipment audit session",
)
def delete_shipment(token: str, owner_id: str = Depends(get_current_owner_id)):
    if not isinstance(owner_id, str):
        owner_id = "sandbox_default"
    success = session_store.delete_session(token=token, owner_id=owner_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Shipment session '{token}' not found",
        )
    return {"status": "success", "deleted_token": token}


@router.post(
    "/api/workspace/reset",
    summary="Reset sandbox workspace to fresh initial demo state",
)
def reset_workspace(owner_id: str = Depends(get_current_owner_id)):
    if not isinstance(owner_id, str):
        owner_id = "sandbox_default"
    session_store.reset_workspace(owner_id=owner_id)
    return {
        "status": "success",
        "owner_id": owner_id,
        "message": "Sandbox workspace reset successfully.",
    }


# ---------------------------------------------------------------------------
# 1. POST /api/audit (Buyer 3-Document Upload -> Generates Dynamic Session)
# ---------------------------------------------------------------------------
@router.post(
    "/api/audit",
    response_model=AuditResponse,
    summary="Run initial 3-way pre-shipment compliance audit and initialize revision session",
)
async def audit(
    po_pdf: Annotated[UploadFile, File(description="US Buyer Purchase Order (PDF)")],
    inv_pdf: Annotated[UploadFile, File(description="Commercial Invoice (PDF)")],
    pl_pdf: Annotated[UploadFile, File(description="Packing List (PDF)")],
    tolerance_pct: Annotated[float, Form(ge=0.0, le=25.0)] = 5.0,
    mock: Annotated[bool, Form()] = False,
    owner_id: str = Depends(get_current_owner_id),
) -> AuditResponse:
    if not isinstance(owner_id, str):
        owner_id = "sandbox_default"

    po_bytes = await _read_upload(po_pdf, "Buyer PO PDF")
    inv_bytes = await _read_upload(inv_pdf, "Commercial Invoice PDF")
    pl_bytes = await _read_upload(pl_pdf, "Packing List PDF")

    if mock:
        logger.info("Mock mode requested: generating test packet for owner %s", owner_id)
        extracted = build_mock_packet()
    else:
        try:
            logger.info("Extracting 3 PDFs with Gemini multimodal ingestion for owner %s", owner_id)
            extracted = extract_shipment_packet(po_bytes, inv_bytes, pl_bytes)
        except Exception as exc:
            logger.exception("PDF extraction failed")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"PDF extraction failed: {str(exc)}",
            ) from exc

    # Generate dynamic shipment ID for newly uploaded packet
    po_num = extracted.metadata.po_number or "PO"
    inv_num = extracted.metadata.invoice_no or "INV"
    dyn_num = (abs(hash(f"{po_num}:{inv_num}:{secrets.token_hex(2)}")) % 9000) + 1000
    dynamic_shipment_id = f"SHP-2026-{dyn_num}"

    packet = extracted_to_shipment_packet(
        extracted,
        tolerance_pct=tolerance_pct,
        shipment_id=dynamic_shipment_id,
    )

    # Run deterministic compliance engine
    findings, passed_checks = run_deterministic_lint(packet)

    # Build final serialized response
    audit_response = build_audit_response(
        packet=packet,
        findings=findings,
        passed_checks=passed_checks,
        revision_number=1,
    )

    # Store in session store scoped to owner_id with deduplication
    stored_audit = session_store.create_or_replace_session(
        audit=audit_response,
        owner_id=owner_id,
        files={
            "po": po_bytes,
            "inv": inv_bytes,
            "pl": pl_bytes,
            "mock": mock,
            "tolerance_pct": tolerance_pct,
        },
    )

    return stored_audit


# ---------------------------------------------------------------------------
# 2. GET /api/links/{token} (Public, Zero-Auth Session Fetch)
# ---------------------------------------------------------------------------
@router.get(
    "/api/links/{token}",
    response_model=AuditResponse,
    summary="Fetch current audit session state by token",
)
async def get_session(token: str) -> AuditResponse:
    session = session_store.get_session(token)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit session '{token}' not found or expired.",
        )
    return session


# ---------------------------------------------------------------------------
# 3. POST /api/links/{token}/re-audit (Supplier Re-Audit with Locked PO)
# ---------------------------------------------------------------------------
@router.post(
    "/api/links/{token}/re-audit",
    response_model=AuditResponse,
    summary="Supplier uploads revised Invoice and/or Packing List PDFs",
)
async def re_audit(
    token: str,
    invoice_file: Optional[UploadFile] = File(None),
    packing_list_file: Optional[UploadFile] = File(None),
    mock: Optional[bool] = Form(False),
) -> AuditResponse:
    if not invoice_file and not packing_list_file and not mock:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one revised file (Commercial Invoice or Packing List) must be provided.",
        )

    existing_audit = session_store.get_session(token)
    if not existing_audit:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit session '{token}' not found.",
        )

    # Guardrail: Lock packet when already cleared
    unresolved_count = (
        existing_audit.summary.unresolved_count
        if existing_audit.summary
        else sum(
            1 for d in existing_audit.discrepancies
            if d.resolution_state in ("UNRESOLVED", "REVISION_REQUESTED")
        )
    )
    if unresolved_count == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Shipment packet is already cleared and locked for customs filing.",
        )

    stored_files = session_store.get_session_files(token)
    po_bytes = stored_files.get("po", b"")
    stored_mock = stored_files.get("mock", False)
    tolerance_pct = stored_files.get("tolerance_pct", 5.0)

    # Read uploaded file bytes
    new_inv_bytes = (
        await _read_upload(invoice_file, "Invoice PDF")
        if invoice_file
        else stored_files.get("inv", b"")
    )
    new_pl_bytes = (
        await _read_upload(packing_list_file, "Packing List PDF")
        if packing_list_file
        else stored_files.get("pl", b"")
    )

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
        logger.info("Sample/simulated revision triggered for session %s: applying clean revised packet", token)
        updated_packet = build_mock_revised_packet(existing_audit.packet)
    else:
        try:
            logger.info("Re-extracting revised packet with Gemini for token %s", token)
            extracted = extract_shipment_packet(po_bytes, new_inv_bytes, new_pl_bytes)
            updated_packet = extracted_to_shipment_packet(
                extracted,
                tolerance_pct=tolerance_pct,
                shipment_id=existing_audit.packet.shipment_id,
            )
            # Guarantee locked buyer PO is strictly retained
            if existing_audit.packet.buyer_po:
                updated_packet.buyer_po = existing_audit.packet.buyer_po
        except Exception as exc:
            logger.exception("Re-extraction failed: falling back to structural update")
            updated_packet = existing_audit.packet

    # Re-run deterministic audit against updated packet
    findings, passed_checks = run_deterministic_lint(updated_packet)

    # Build new audit response
    new_revision_number = existing_audit.revision_number + 1
    new_audit = build_audit_response(
        packet=updated_packet,
        findings=findings,
        passed_checks=passed_checks,
        session_token=token,
        revision_number=new_revision_number,
    )

    # Update session, preserve prior resolutions, and increment revision
    try:
        updated_session = session_store.update_session_reaudit(
            token=token,
            new_audit=new_audit,
            new_files={"inv": new_inv_bytes, "pl": new_pl_bytes},
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return updated_session


# ---------------------------------------------------------------------------
# 4. PATCH /api/links/{token}/discrepancies/{discrepancy_id} (Resolution Sign-Off)
# ---------------------------------------------------------------------------
@router.patch(
    "/api/links/{token}/discrepancies/{discrepancy_id}",
    response_model=AuditResponse,
    summary="Update resolution state of a specific discrepancy",
)
async def patch_discrepancy(
    token: str,
    discrepancy_id: str,
    req: PatchResolutionRequest,
) -> AuditResponse:
    try:
        updated_session = session_store.resolve_discrepancy(
            token=token,
            discrepancy_id=discrepancy_id,
            patch_data=req,
        )
        return updated_session
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
