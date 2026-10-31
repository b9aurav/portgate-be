from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

from app.schemas.domain import (
    AuditOverallStatus,
    FindingCategory,
    ResolutionState,
    SeverityLevel,
    ShipmentPacket,
)


class DiscrepancyItem(BaseModel):
    """
    Unified discrepancy model holding core rule violation data,
    buyer/supplier resolution tracking, and UI DocumentViewer compatibility attributes.
    """

    id: str
    rule_id: str
    category: FindingCategory
    severity: SeverityLevel
    title: str
    description: str
    source_doc_a: str
    source_field_a: str
    source_val_a: str
    source_doc_b: str
    source_field_b: str
    source_val_b: str
    highlight_cells: List[str] = Field(default_factory=list)
    financial_impact_note: Optional[str] = None
    recommended_actions: List[str] = Field(default_factory=list)
    resolution_state: ResolutionState = "UNRESOLVED"
    resolution_reason_chip: Optional[str] = None
    resolution_note: Optional[str] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[str] = None

    # UI / DocumentViewer compatibility fields
    sev: Optional[str] = None
    cells: Optional[List[str]] = None
    locs: Optional[List[List[str]]] = None
    body: Optional[str] = None
    impact: Optional[str] = None
    fix: Optional[str] = None


class AuditSummary(BaseModel):
    status: AuditOverallStatus
    audit_mode: Literal["3_WAY_FULL", "2_WAY_INVOICE_PL"] = "3_WAY_FULL"
    ruleset_version: str = "IN-US-2026.4"
    total_findings: int = 0
    deterministic_conflicts: int = 0
    commercial_variances: int = 0
    compliance_advisories: int = 0
    unresolved_count: int = 0
    critical_count: Optional[int] = 0
    warning_count: Optional[int] = 0
    passed_count: Optional[int] = 0


class AuditResponse(BaseModel):
    """
    Root response model matching both backend domain specifications
    and the Next.js frontend contract.
    """

    # Core Structured Fields
    session_token: str = ""
    revision_number: int = 1
    packet: ShipmentPacket
    summary: AuditSummary
    discrepancies: List[DiscrepancyItem] = Field(default_factory=list)

    # Flattened UI Compatibility Fields (consumed directly by DocumentViewer & AuditSidebar)
    shipment_ref: str = "SHP-2026-8841"
    mid_code: str = ""
    metadata: Optional[Dict[str, Any]] = None
    po_lines: List[Dict[str, Any]] = Field(default_factory=list)
    inv_lines: List[Dict[str, Any]] = Field(default_factory=list)
    weights: Dict[str, Any] = Field(default_factory=dict)
    cartons: Dict[str, Any] = Field(default_factory=dict)
    items: List[DiscrepancyItem] = Field(default_factory=list)
    passed: List[str] = Field(default_factory=list)


class PatchResolutionRequest(BaseModel):
    """Payload for PATCH /api/links/{token}/discrepancies/{discrepancy_id}."""

    resolution_state: ResolutionState
    resolution_reason_chip: Optional[str] = None
    resolution_note: Optional[str] = None
    actor_role: Literal["SUPPLIER_PORTAL", "BUYER_WORKBENCH"] = "BUYER_WORKBENCH"

