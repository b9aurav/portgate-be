"""
tests/test_api_routes.py — Integration Tests for PortGate FastAPI HTTP Endpoints.

Tests:
1. GET /api/health probe.
2. GET /api/shipments with and without X-Workspace-Id header.
3. POST /api/audit multipart 3-PDF upload, tolerance validation, and error handling.
4. GET /api/links/{token} public zero-auth fetch and 404 handling.
5. PATCH /api/links/{token}/discrepancies/{disc_id} state transitions and decision log.
6. POST /api/links/{token}/re-audit revisions and cleared-shipment lockout guardrail.
7. DELETE /api/shipments/{token} workspace isolation and deletion validation.
8. POST /api/workspace/reset state wiping and re-seeding.
"""

from __future__ import annotations

import io
import pytest
from fastapi.testclient import TestClient

from app.schemas.api import AuditResponse, PatchResolutionRequest
from app.schemas.domain import ShipmentPacket
from app.services.linter import run_deterministic_audit
from app.services.session_store import session_store


# ---------------------------------------------------------------------------
# Helper to create a dummy PDF file tuple for TestClient multipart upload
# ---------------------------------------------------------------------------
def _make_dummy_pdf(filename: str = "doc.pdf", content: bytes = b"%PDF-1.4 test dummy content\n%%EOF"):
    return (filename, io.BytesIO(content), "application/pdf")


# ---------------------------------------------------------------------------
# 1. Health Probe
# ---------------------------------------------------------------------------
def test_health_probe(client: TestClient):
    """GET /api/health returns 200 OK with configuration and service health."""
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "portgate"
    assert "version" in data
    assert "active_sessions" in data


# ---------------------------------------------------------------------------
# 2. GET /api/shipments (Workspace Scoped)
# ---------------------------------------------------------------------------
def test_get_shipments_with_and_without_workspace_header(client: TestClient):
    """
    GET /api/shipments:
    - With X-Workspace-Id: sandbox_test_1 lazy-seeds and returns list for sandbox_test_1.
    - Without header falls back cleanly to sandbox_default.
    """
    # 1. Request with custom workspace header
    res_custom = client.get("/api/shipments", headers={"X-Workspace-Id": "sandbox_test_1"})
    assert res_custom.status_code == 200
    items_custom = res_custom.json()
    assert isinstance(items_custom, list)
    assert len(items_custom) >= 1
    token_custom = items_custom[0]["session_token"]
    assert token_custom.startswith("shp_")

    # 2. Request without header (falls back to sandbox_default)
    res_default = client.get("/api/shipments")
    assert res_default.status_code == 200
    items_default = res_default.json()
    assert isinstance(items_default, list)
    assert len(items_default) >= 1
    token_default = items_default[0]["session_token"]
    assert token_default.startswith("shp_")

    # Distinct tokens between sandbox_test_1 and sandbox_default
    assert token_custom != token_default


# ---------------------------------------------------------------------------
# 3. POST /api/audit (Multipart 3-PDF Upload)
# ---------------------------------------------------------------------------
def test_post_audit_upload_creates_session(client: TestClient):
    """
    POST /api/audit (multipart 3-PDF upload + tolerance_pct):
    - Ingests PO, Invoice, Packing List PDFs.
    - Creates/upserts session scoped to X-Workspace-Id.
    - Returns 200 OK with session_token, packet, summary, and UI view models.
    """
    workspace = "sandbox_api_audit_user"
    files = {
        "po_pdf": _make_dummy_pdf("po.pdf"),
        "inv_pdf": _make_dummy_pdf("inv.pdf"),
        "pl_pdf": _make_dummy_pdf("pl.pdf"),
    }
    data = {
        "tolerance_pct": "5.0",
        "mock": "false",
    }
    headers = {"X-Workspace-Id": workspace}

    res = client.post("/api/audit", files=files, data=data, headers=headers)
    assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"

    audit_data = res.json()
    token = audit_data.get("session_token")
    assert token and token.startswith("shp_")
    assert audit_data["revision_number"] == 1
    assert "packet" in audit_data
    assert "summary" in audit_data
    assert "discrepancies" in audit_data
    assert audit_data.get("mid_code") == "INSHRTEX2301SUR"

    # Verify session is retrievable by owner
    shipments = session_store.list_shipments(workspace)
    assert any(s.session_token == token for s in shipments)


def test_post_audit_validation_failures(client: TestClient):
    """POST /api/audit validates file presence and rejects empty uploads with 422."""
    workspace = "sandbox_validation_test"

    # Empty file content
    files = {
        "po_pdf": _make_dummy_pdf("po.pdf", content=b""),
        "inv_pdf": _make_dummy_pdf("inv.pdf"),
        "pl_pdf": _make_dummy_pdf("pl.pdf"),
    }
    res = client.post("/api/audit", files=files, headers={"X-Workspace-Id": workspace})
    assert res.status_code == 422
    assert "empty" in res.json().get("detail", "").lower()


# ---------------------------------------------------------------------------
# 4. GET /api/links/{token} (Zero-Login Public Access)
# ---------------------------------------------------------------------------
def test_get_session_by_token_zero_auth(client: TestClient, dirty_packet_v1: ShipmentPacket):
    """
    GET /api/links/{token}:
    - Works without requiring X-Workspace-Id (supporting zero-login external suppliers).
    - Returns 404 for unknown/expired tokens.
    """
    # 1. Seed a session
    audit = run_deterministic_audit(dirty_packet_v1)
    stored = session_store.create_or_replace_session(audit, owner_id="sandbox_owner_zero_auth")
    token = stored.session_token

    # 2. Fetch WITHOUT any X-Workspace-Id header
    res = client.get(f"/api/links/{token}")
    assert res.status_code == 200
    data = res.json()
    assert data["session_token"] == token
    assert data["revision_number"] == 1
    assert data["summary"]["status"] == "ACTION_REQUIRED"

    # 3. Unknown token returns 404
    res_404 = client.get("/api/links/shp_nonexistent_token_12345")
    assert res_404.status_code == 404
    assert "not found" in res_404.json().get("detail", "").lower()


# ---------------------------------------------------------------------------
# 5. PATCH /api/links/{token}/discrepancies/{disc_id}
# ---------------------------------------------------------------------------
def test_patch_discrepancy_state_and_decision_trail(client: TestClient, dirty_packet_v1: ShipmentPacket):
    """
    PATCH /api/links/{token}/discrepancies/{disc_id}:
    - Updates resolution state (ACCEPTED_EXCEPTION, REVISION_REQUESTED, ESCALATED_TO_BROKER).
    - Records decision log entry in decision_trail.
    - Recalculates unresolved_count and overall status.
    """
    audit = run_deterministic_audit(dirty_packet_v1)
    stored = session_store.create_or_replace_session(audit, owner_id="sandbox_patch_test")
    token = stored.session_token
    initial_unresolved = stored.summary.unresolved_count

    # 1. Patch to ACCEPTED_EXCEPTION
    target_id = "DISC-CARTONS"
    patch_payload = {
        "resolution_state": "ACCEPTED_EXCEPTION",
        "resolution_reason_chip": "Commercial Agreement",
        "resolution_note": "Buyer accepts carton adjustment",
        "actor_role": "BUYER_WORKBENCH",
    }
    res = client.patch(f"/api/links/{token}/discrepancies/{target_id}", json=patch_payload)
    assert res.status_code == 200
    data = res.json()

    # Verify updated item
    patched_disc = next(d for d in data["discrepancies"] if d["id"] == target_id)
    assert patched_disc["resolution_state"] == "ACCEPTED_EXCEPTION"
    assert patched_disc["resolution_note"] == "Buyer accepts carton adjustment"
    assert patched_disc["resolved_by"] == "US Buyer Import Specialist"

    # Verify unresolved count reduced
    assert data["summary"]["unresolved_count"] == initial_unresolved - 1

    # Verify decision trail log entry recorded at top
    assert len(data["decision_trail"]) > 0
    top_decision = data["decision_trail"][0]
    assert top_decision["discrepancy_id"] == target_id
    assert top_decision["action"] == "ACCEPT_VARIANCE"
    assert top_decision["new_state"] == "ACCEPTED_EXCEPTION"
    assert top_decision["note"] == "Buyer accepts carton adjustment"

    # 2. Patch another to REVISION_REQUESTED
    target_id_2 = "DISC-PRICE-2"
    patch_req_revision = {
        "resolution_state": "REVISION_REQUESTED",
        "resolution_reason_chip": "Price Discrepancy",
        "resolution_note": "Supplier must invoice at PO rate $6.00",
        "actor_role": "BUYER_WORKBENCH",
    }
    res2 = client.patch(f"/api/links/{token}/discrepancies/{target_id_2}", json=patch_req_revision)
    assert res2.status_code == 200

    # 3. Patch non-existent discrepancy returns 404
    res_fake = client.patch(
        f"/api/links/{token}/discrepancies/DISC-DOESNT-EXIST",
        json=patch_payload,
    )
    assert res_fake.status_code == 404

    # 4. Patch on non-existent token returns 404
    res_fake_tok = client.patch(
        "/api/links/shp_fake_token/discrepancies/DISC-CARTONS",
        json=patch_payload,
    )
    assert res_fake_tok.status_code == 404


# ---------------------------------------------------------------------------
# 6. POST /api/links/{token}/re-audit & Lockout Guardrail
# ---------------------------------------------------------------------------
def test_post_reaudit_flow_and_cleared_lockout(
    client: TestClient,
    dirty_packet_v1: ShipmentPacket,
    clean_packet: ShipmentPacket,
):
    """
    POST /api/links/{token}/re-audit:
    - Succeeds when unresolved_count > 0, increments revision_number.
    - Requires at least one revised file when mock=False (400 if none provided).
    - Returns 400 Bad Request when the shipment packet is already cleared and locked.
    """
    # 1. Create dirty session with unresolved items
    audit = run_deterministic_audit(dirty_packet_v1)
    stored = session_store.create_or_replace_session(
        audit,
        owner_id="sandbox_reaudit_route_test",
        files={"po": b"%PDF dummy", "mock": True, "tolerance_pct": 5.0},
    )
    token = stored.session_token

    # 2. Re-audit with no files and mock=False returns 400
    res_empty = client.post(f"/api/links/{token}/re-audit", data={"mock": "false"})
    assert res_empty.status_code == 400
    assert "at least one revised file" in res_empty.json().get("detail", "").lower()

    # 3. Re-audit with revised files succeeds and increments revision
    files = {
        "invoice_file": _make_dummy_pdf("revised_inv.pdf"),
        "packing_list_file": _make_dummy_pdf("revised_pl.pdf"),
    }
    res_reaudit = client.post(
        f"/api/links/{token}/re-audit",
        files=files,
        data={"mock": "true"},
    )
    assert res_reaudit.status_code == 200
    data = res_reaudit.json()
    assert data["revision_number"] == 2
    assert data["session_token"] == token

    # 4. Cleared shipment lockout guardrail:
    # Resolve all remaining discrepancies so unresolved_count == 0
    current_session = session_store.get_session(token)
    assert current_session is not None
    for d in current_session.discrepancies:
        session_store.resolve_discrepancy(
            token=token,
            discrepancy_id=d.id,
            patch_data=PatchResolutionRequest(
                resolution_state="ACCEPTED_EXCEPTION",
                resolution_reason_chip="Waiver",
                resolution_note="Approved all items",
                actor_role="BUYER_WORKBENCH",
            ),
        )

    # Attempt re-audit on fully cleared shipment -> 400 Bad Request
    files_late = {"invoice_file": _make_dummy_pdf("late_invoice.pdf")}
    res_locked = client.post(
        f"/api/links/{token}/re-audit",
        files=files_late,
        data={"mock": "true"},
    )
    assert res_locked.status_code == 400
    assert "already cleared and locked for customs filing" in res_locked.json().get("detail", "")


# ---------------------------------------------------------------------------
# 7. DELETE /api/shipments/{token} (Workspace Isolation)
# ---------------------------------------------------------------------------
def test_delete_shipment_workspace_isolation(client: TestClient, clean_packet: ShipmentPacket):
    """
    DELETE /api/shipments/{token}:
    - Deletes only the shipment belonging to caller's X-Workspace-Id.
    - Attempting deletion from another workspace returns 404.
    """
    owner_a = "sandbox_owner_del_A"
    owner_b = "sandbox_owner_del_B"

    audit = run_deterministic_audit(clean_packet)
    stored = session_store.create_or_replace_session(audit, owner_id=owner_a)
    token = stored.session_token

    # 1. Attempt delete from workspace B -> 404
    res_b = client.delete(f"/api/shipments/{token}", headers={"X-Workspace-Id": owner_b})
    assert res_b.status_code == 404
    assert session_store.get_session(token) is not None, "Shipment must not be deleted by workspace B"

    # 2. Delete from workspace A -> 200 OK
    res_a = client.delete(f"/api/shipments/{token}", headers={"X-Workspace-Id": owner_a})
    assert res_a.status_code == 200
    assert res_a.json() == {"status": "success", "deleted_token": token}
    assert session_store.get_session(token) is None

    # 3. Subsequent delete -> 404
    res_a_again = client.delete(f"/api/shipments/{token}", headers={"X-Workspace-Id": owner_a})
    assert res_a_again.status_code == 404


# ---------------------------------------------------------------------------
# 8. POST /api/workspace/reset
# ---------------------------------------------------------------------------
def test_post_workspace_reset(client: TestClient, dirty_packet_v1: ShipmentPacket):
    """
    POST /api/workspace/reset:
    - Wipes custom uploads for X-Workspace-Id.
    - Restores fresh seed shipments for the workspace.
    """
    owner = "sandbox_reset_user"

    # Add a custom uploaded session
    audit = run_deterministic_audit(dirty_packet_v1)
    stored = session_store.create_or_replace_session(audit, owner_id=owner)
    custom_token = stored.session_token

    # Verify custom session exists
    shipments_before = session_store.list_shipments(owner)
    assert any(s.session_token == custom_token for s in shipments_before)

    # Call reset
    res = client.post("/api/workspace/reset", headers={"X-Workspace-Id": owner})
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["owner_id"] == owner

    # Verify workspace has a fresh initial demo shipment
    shipments_after = session_store.list_shipments(owner)
    assert len(shipments_after) >= 1
    # Custom token is gone, replaced with fresh seed
    assert not any(s.session_token == custom_token for s in shipments_after)


# ---------------------------------------------------------------------------
# 9. Supabase JWT Authentication & Token Verification
# ---------------------------------------------------------------------------
def test_get_shipments_with_supabase_jwt_auth(client: TestClient):
    """
    Validates Supabase Bearer token authentication:
    - Valid Bearer token scopes queue to user_<id>.
    - Invalid or expired Bearer token raises 401 Unauthorized.
    - Malformed Authorization header raises 401 Unauthorized.
    """
    # 1. Valid token -> 200 OK scoped to user
    res_valid = client.get(
        "/api/shipments",
        headers={"Authorization": "Bearer valid_token_buyer_alpha"},
    )
    assert res_valid.status_code == 200
    shipments = res_valid.json()
    assert isinstance(shipments, list)
    assert len(shipments) >= 1

    # 2. Invalid token -> 401 Unauthorized
    res_invalid = client.get(
        "/api/shipments",
        headers={"Authorization": "Bearer invalid_secret_token_123"},
    )
    assert res_invalid.status_code == 401
    assert "Invalid or expired authentication token" in res_invalid.json()["detail"]

    # 3. Malformed Authorization (missing Bearer prefix) -> 401 Unauthorized
    res_malformed = client.get(
        "/api/shipments",
        headers={"Authorization": "Basic some_credentials"},
    )
    assert res_malformed.status_code == 401


# ---------------------------------------------------------------------------
# 10. POST /api/workspace/claim
# ---------------------------------------------------------------------------
def test_post_workspace_claim(client: TestClient, dirty_packet_v1: ShipmentPacket):
    """
    POST /api/workspace/claim:
    - Requires valid Authorization: Bearer token (401 without auth).
    - Migrates shipments from X-Workspace-Id into the authenticated user account.
    - Subsequent claim of duplicate PO+Invoice key deduplicates cleanly.
    """
    # 1. Unauthenticated claim attempt -> 401
    res_no_auth = client.post(
        "/api/workspace/claim",
        headers={"X-Workspace-Id": "sandbox_claim_test"},
    )
    assert res_no_auth.status_code == 401
    assert "Authentication required" in res_no_auth.json()["detail"]

    # 2. Setup a sandbox session
    sandbox_id = "sandbox_anon_prospective_buyer"
    audit = run_deterministic_audit(dirty_packet_v1)
    stored_sandbox = session_store.create_or_replace_session(audit, owner_id=sandbox_id)
    sandbox_token = stored_sandbox.session_token

    # Verify shipment is in sandbox
    assert any(s.session_token == sandbox_token for s in session_store.list_shipments(sandbox_id))

    # 3. Claim sandbox into user account user_alice_corp
    auth_header = {"Authorization": "Bearer valid_token_alice_corp", "X-Workspace-Id": sandbox_id}
    res_claim = client.post("/api/workspace/claim", headers=auth_header)
    assert res_claim.status_code == 200
    claim_data = res_claim.json()
    assert claim_data["status"] == "success"
    assert claim_data["claimed_count"] == 1
    assert claim_data["owner_id"] == "user_alice_corp"

    # Verify shipment is now in user's account
    user_shipments = session_store.list_shipments("user_alice_corp")
    assert any(s.session_token == sandbox_token for s in user_shipments)

    # 4. Claim again (duplicate PO+Invoice): must handle deduplication cleanly
    res_claim_again = client.post("/api/workspace/claim", headers=auth_header)
    assert res_claim_again.status_code == 200
    assert res_claim_again.json()["claimed_count"] == 0
