"""
tests/test_session_store.py — Direct Multi-Tenant Session Store Lifecycle & Transition Tests.

Tests:
1. Per-Workspace Sandbox Isolation (owner_id).
2. Upsert Deduplication by (owner_id, po_invoice_key).
3. Granular Queue Status Derivation (ACTION_REQUIRED, AWAITING_SUPPLIER_REVISION, CLEARED_WITH_EXCEPTIONS, VERIFIED_CLEAN).
4. Re-Audit State Preservation & Diff Tracking (update_session_reaudit).
5. Cleared Shipment Lockout Guardrail.
"""

from __future__ import annotations

import copy
import pytest

from app.schemas.api import AuditResponse, PatchResolutionRequest
from app.schemas.domain import ShipmentPacket
from app.services.linter import run_deterministic_audit
from app.services.session_store import session_store


# ---------------------------------------------------------------------------
# 1. Per-Workspace Sandbox Isolation (owner_id)
# ---------------------------------------------------------------------------
def test_workspace_sandbox_isolation(dirty_packet_v1: ShipmentPacket):
    """
    Validates per-browser sandbox isolation via owner_id:
    - User A and User B receive distinct lazy-seeded demo shipments.
    - Modifying or deleting a shipment in User A has zero effect on User B.
    - Attempting to delete User A's shipment using User B's owner_id is rejected.
    """
    # 1. Lazy-seed separate workspaces
    shipments_a = session_store.list_shipments("sandbox_user_A")
    shipments_b = session_store.list_shipments("sandbox_user_B")

    assert len(shipments_a) == 1, "User A should have 1 seeded shipment"
    assert len(shipments_b) == 1, "User B should have 1 seeded shipment"

    token_a = shipments_a[0].session_token
    token_b = shipments_b[0].session_token
    assert token_a != token_b, "Tokens across workspaces must be distinct"

    # 2. Modify User A's shipment by resolving a discrepancy
    session_a = session_store.get_session(token_a)
    assert session_a is not None
    first_disc_id = session_a.discrepancies[0].id

    session_store.resolve_discrepancy(
        token=token_a,
        discrepancy_id=first_disc_id,
        patch_data=PatchResolutionRequest(
            resolution_state="ACCEPTED_EXCEPTION",
            resolution_reason_chip="Commercial Approval",
            resolution_note="Isolated to user A",
            actor_role="BUYER_WORKBENCH",
        ),
    )

    # Verify User A's item is updated
    updated_a = session_store.get_session(token_a)
    assert updated_a is not None
    disc_a = next(d for d in updated_a.discrepancies if d.id == first_disc_id)
    assert disc_a.resolution_state == "ACCEPTED_EXCEPTION"

    # Verify User B's shipment is completely unaffected
    session_b = session_store.get_session(token_b)
    assert session_b is not None
    disc_b = next(d for d in session_b.discrepancies if d.id == first_disc_id)
    assert disc_b.resolution_state == "UNRESOLVED", "User B's discrepancy must remain UNRESOLVED"

    # 3. Cross-tenant deletion attempt: User B tries to delete User A's session
    del_unauthorized = session_store.delete_session(token=token_a, owner_id="sandbox_user_B")
    assert del_unauthorized is False, "Cross-tenant deletion must return False"
    assert session_store.get_session(token_a) is not None, "User A session must not be deleted"

    # 4. Authorized deletion by User A
    del_authorized = session_store.delete_session(token=token_a, owner_id="sandbox_user_A")
    assert del_authorized is True, "Authorized deletion must return True"
    assert session_store.get_session(token_a) is None, "User A session should be removed"

    # Verify User B's session remains untouched
    assert session_store.get_session(token_b) is not None, "User B session must still exist"


# ---------------------------------------------------------------------------
# 2. Upsert Deduplication by (owner_id, po_invoice_key)
# ---------------------------------------------------------------------------
def test_upsert_deduplication_by_owner_and_po_invoice_key(dirty_packet_v1: ShipmentPacket):
    """
    Uploading a packet with PO + Invoice twice under the same owner_id:
    - Reuses the existing session_token.
    - Overwrites the row in place (prevents duplicate rows in queue).
    - Resets revision_number to 1.
    Uploading that same PO + Invoice under a *different* owner_id:
    - Creates a separate row and token for that second owner.
    """
    audit1 = run_deterministic_audit(dirty_packet_v1)
    owner_1 = "sandbox_upsert_user_1"
    owner_2 = "sandbox_upsert_user_2"

    # 1. First upload under owner_1
    res1 = session_store.create_or_replace_session(audit=audit1, owner_id=owner_1)
    tok1 = res1.session_token
    assert tok1.startswith("shp_")
    assert res1.revision_number == 1

    # 2. Re-upload identical PO + Invoice under same owner_1 (simulating re-upload)
    audit2 = run_deterministic_audit(dirty_packet_v1)
    audit2.revision_number = 5  # Arbitrary higher revision to test reset
    res2 = session_store.create_or_replace_session(audit=audit2, owner_id=owner_1)

    assert res2.session_token == tok1, "Must reuse existing session_token"
    assert res2.revision_number == 1, "Must reset revision_number to 1 upon fresh upload"

    # Verify queue count for owner_1 has no duplicates
    owner1_items = session_store.list_shipments(owner_1)
    matching_owner1 = [it for it in owner1_items if it.session_token == tok1]
    assert len(matching_owner1) == 1
    assert len(owner1_items) == 1, "Queue must contain exactly 1 row (no duplicates)"

    # 3. Upload same PO + Invoice under owner_2
    audit3 = run_deterministic_audit(dirty_packet_v1)
    res3 = session_store.create_or_replace_session(audit=audit3, owner_id=owner_2)
    tok2 = res3.session_token

    assert tok2 != tok1, "Different owner_id must receive a distinct token"
    assert len(session_store.list_shipments(owner_1)) == 1
    assert len(session_store.list_shipments(owner_2)) == 1


# ---------------------------------------------------------------------------
# 3. Granular Queue Status Derivation
# ---------------------------------------------------------------------------
def test_granular_queue_status_derivation(clean_packet: ShipmentPacket, dirty_packet_v1: ShipmentPacket):
    """
    Verifies status transitions accurately across:
    - 'VERIFIED_CLEAN': originally 0 discrepancies.
    - 'ACTION_REQUIRED': unresolved hard conflicts or variances exist.
    - 'AWAITING_SUPPLIER_REVISION': remaining unresolved items are flagged REVISION_REQUESTED.
    - 'CLEARED_WITH_EXCEPTIONS': unresolved_count == 0 after buyer accepts variances / exceptions.
    """
    owner = "sandbox_status_test"

    # 1. Clean packet -> VERIFIED_CLEAN
    clean_audit = run_deterministic_audit(clean_packet)
    clean_res = session_store.create_or_replace_session(clean_audit, owner_id=owner)
    clean_items = session_store.list_shipments(owner)
    clean_item = next(it for it in clean_items if it.session_token == clean_res.session_token)
    assert clean_item.status == "VERIFIED_CLEAN"
    assert clean_item.unresolved_count == 0

    # 2. Dirty packet -> ACTION_REQUIRED
    dirty_audit = run_deterministic_audit(dirty_packet_v1)
    dirty_res = session_store.create_or_replace_session(dirty_audit, owner_id=owner)
    dirty_tok = dirty_res.session_token

    dirty_items = session_store.list_shipments(owner)
    dirty_item = next(it for it in dirty_items if it.session_token == dirty_tok)
    assert dirty_item.status == "ACTION_REQUIRED"
    assert dirty_item.unresolved_count > 0

    # 3. Flag an item as REVISION_REQUESTED -> AWAITING_SUPPLIER_REVISION
    disc_to_req = dirty_audit.discrepancies[0].id
    session_store.resolve_discrepancy(
        token=dirty_tok,
        discrepancy_id=disc_to_req,
        patch_data=PatchResolutionRequest(
            resolution_state="REVISION_REQUESTED",
            resolution_reason_chip="Supplier Fix Requested",
            resolution_note="Please rectify carton count",
            actor_role="BUYER_WORKBENCH",
        ),
    )
    req_items = session_store.list_shipments(owner)
    req_item = next(it for it in req_items if it.session_token == dirty_tok)
    assert req_item.status == "AWAITING_SUPPLIER_REVISION"

    # 4. Resolve ALL discrepancies -> CLEARED_WITH_EXCEPTIONS
    session_dirty = session_store.get_session(dirty_tok)
    assert session_dirty is not None
    for d in session_dirty.discrepancies:
        session_store.resolve_discrepancy(
            token=dirty_tok,
            discrepancy_id=d.id,
            patch_data=PatchResolutionRequest(
                resolution_state="ACCEPTED_EXCEPTION",
                resolution_reason_chip="Waiver Granted",
                resolution_note="Accepting for test",
                actor_role="BUYER_WORKBENCH",
            ),
        )

    cleared_items = session_store.list_shipments(owner)
    cleared_item = next(it for it in cleared_items if it.session_token == dirty_tok)
    assert cleared_item.unresolved_count == 0
    assert cleared_item.status == "CLEARED_WITH_EXCEPTIONS"


# ---------------------------------------------------------------------------
# 4. Re-Audit State Preservation & Diff Tracking (update_session_reaudit)
# ---------------------------------------------------------------------------
def test_reaudit_state_preservation_and_diff_tracking(
    dirty_packet_v1: ShipmentPacket,
    corrected_packet_v2: ShipmentPacket,
):
    """
    If buyer already marked DISC-QTY-2 as ACCEPTED_EXCEPTION with sign-off note
    'Approved PO Amendment #2' in v1, and supplier uploads v2:
    - revision_number increments from 1 to 2.
    - DISC-QTY-2 retains ACCEPTED_EXCEPTION state and sign-off note.
    - Hard conflicts from v1 that were fixed in v2 (DISC-CARTONS, DISC-MATH-3) are retained
      in the history with resolution_state == 'CLEARED_BY_REVISION'.
    - auto_resolved_in_latest_rev is populated.
    - decision_trail records the revision cleared actions.
    """
    owner = "sandbox_reaudit_test"
    v1_audit = run_deterministic_audit(dirty_packet_v1)
    session = session_store.create_or_replace_session(v1_audit, owner_id=owner)
    token = session.session_token

    # 1. Buyer signs off on DISC-QTY-2
    session_store.resolve_discrepancy(
        token=token,
        discrepancy_id="DISC-QTY-2",
        patch_data=PatchResolutionRequest(
            resolution_state="ACCEPTED_EXCEPTION",
            resolution_reason_chip="Authorized Split Shipment",
            resolution_note="Approved PO Amendment #2",
            actor_role="BUYER_WORKBENCH",
        ),
    )

    # Verify v1 sign-off is recorded
    signed_off_session = session_store.get_session(token)
    assert signed_off_session is not None
    signed_off_item = next(d for d in signed_off_session.discrepancies if d.id == "DISC-QTY-2")
    assert signed_off_item.resolution_state == "ACCEPTED_EXCEPTION"
    assert signed_off_item.resolution_note == "Approved PO Amendment #2"

    # 2. Supplier uploads corrected packet v2 (hard conflicts fixed, DISC-QTY-2 still present)
    v2_audit = run_deterministic_audit(corrected_packet_v2)
    reaudit_res = session_store.update_session_reaudit(
        token=token,
        new_audit=v2_audit,
    )

    # 3. Assertions on v2 session
    # A. Revision number incremented
    assert reaudit_res.revision_number == 2, f"Expected revision 2, got {reaudit_res.revision_number}"

    # B. Buyer sign-off on DISC-QTY-2 is preserved
    v2_qty_disc = next((d for d in reaudit_res.discrepancies if d.id == "DISC-QTY-2"), None)
    assert v2_qty_disc is not None, "DISC-QTY-2 must be present in v2"
    assert v2_qty_disc.resolution_state == "ACCEPTED_EXCEPTION"
    assert v2_qty_disc.resolution_note == "Approved PO Amendment #2"

    # C. Fixed hard conflicts are retained as CLEARED_BY_REVISION (or CLEARED_IN_REVISION)
    fixed_cartons = next((d for d in reaudit_res.discrepancies if d.id == "DISC-CARTONS"), None)
    assert fixed_cartons is not None, "DISC-CARTONS should be retained in history"
    assert fixed_cartons.resolution_state in ("CLEARED_BY_REVISION", "CLEARED_IN_REVISION")
    assert "Rev v2" in (fixed_cartons.resolution_note or "")

    fixed_math = next((d for d in reaudit_res.discrepancies if d.id == "DISC-MATH-3"), None)
    assert fixed_math is not None, "DISC-MATH-3 should be retained in history"
    assert fixed_math.resolution_state in ("CLEARED_BY_REVISION", "CLEARED_IN_REVISION")

    # D. auto_resolved_in_latest_rev contains the fixed discrepancy IDs
    assert "DISC-CARTONS" in reaudit_res.auto_resolved_in_latest_rev
    assert "DISC-MATH-3" in reaudit_res.auto_resolved_in_latest_rev
    assert "DISC-WEIGHT-INV" in reaudit_res.auto_resolved_in_latest_rev

    # E. Decision trail contains auto-resolved events
    assert hasattr(reaudit_res, "decision_trail")
    carton_trail_entry = next(
        (t for t in reaudit_res.decision_trail if t.get("discrepancy_id") == "DISC-CARTONS"),
        None,
    )
    assert carton_trail_entry is not None
    assert carton_trail_entry.get("action") in ("CLEARED_BY_REVISION", "CLEARED_IN_REVISION")


# ---------------------------------------------------------------------------
# 5. Cleared Shipment Lockout Guardrail
# ---------------------------------------------------------------------------
def test_cleared_shipment_lockout_guardrail(clean_packet: ShipmentPacket, dirty_packet_v1: ShipmentPacket):
    """
    Once unresolved_count == 0, calling update_session_reaudit must raise an exception
    ('Shipment packet is already cleared and locked for customs filing').
    """
    owner = "sandbox_lockout_test"

    # Case A: Clean packet has unresolved_count == 0 originally
    clean_audit = run_deterministic_audit(clean_packet)
    clean_session = session_store.create_or_replace_session(clean_audit, owner_id=owner)
    token_clean = clean_session.session_token

    new_audit_attempt = run_deterministic_audit(clean_packet)
    with pytest.raises(Exception) as exc_info:
        session_store.update_session_reaudit(token=token_clean, new_audit=new_audit_attempt)

    assert "already cleared and locked for customs filing" in str(exc_info.value)

    # Case B: Dirty packet where all items get resolved
    dirty_audit = run_deterministic_audit(dirty_packet_v1)
    dirty_session = session_store.create_or_replace_session(dirty_audit, owner_id=owner)
    token_dirty = dirty_session.session_token

    for d in dirty_session.discrepancies:
        session_store.resolve_discrepancy(
            token=token_dirty,
            discrepancy_id=d.id,
            patch_data=PatchResolutionRequest(
                resolution_state="ACCEPTED_EXCEPTION",
                resolution_reason_chip="Waiver",
                resolution_note="Resolved all",
                actor_role="BUYER_WORKBENCH",
            ),
        )

    re_attempt = run_deterministic_audit(dirty_packet_v1)
    with pytest.raises(Exception) as exc_info2:
        session_store.update_session_reaudit(token=token_dirty, new_audit=re_attempt)

    assert "already cleared and locked for customs filing" in str(exc_info2.value)

