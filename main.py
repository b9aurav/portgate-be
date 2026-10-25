"""
main.py — PortGate FastAPI application.

Endpoints:
    GET  /api/health          — Liveness probe
    POST /api/audit           — 3-way compliance audit (3 PDF uploads)

CORS is enabled for all origins (configure ALLOWED_ORIGINS in production).

Environment variables (.env):
    GOOGLE_API_KEY    — Required for live PDF extraction via Gemini
    GEMINI_MODEL      — Optional, defaults to gemini-2.5-flash-preview-05-20
    ALLOWED_ORIGINS   — Comma-separated list of allowed CORS origins
                        (defaults to "*" for development)
    LOG_LEVEL         — Uvicorn log level (default: info)

Run:
    uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Annotated

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from extractor import extract_shipment_packet
from linter import _build_mock_packet, run_audit
from schemas import AuditResponse

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
        "gemini_model": os.getenv("GEMINI_MODEL", "gemini-2.5-flash-preview-05-20"),
        "api_key_configured": bool(os.getenv("GOOGLE_API_KEY")),
    }


# ---------------------------------------------------------------------------
# Audit endpoint
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
    request_id = "n/a"  # populated by middleware after function returns

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
