import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.api.routes import audit, get_session, list_shipments, patch_discrepancy, re_audit
from app.schemas.api import PatchResolutionRequest
from fastapi import UploadFile

async def run_direct_tests():
    print("0. Testing list_shipments()...")
    initial_shipments = list_shipments()
    assert len(initial_shipments) >= 1, "Expected at least 1 seeded shipment"
    print(f"   Shipments listed: {len(initial_shipments)}, first token={initial_shipments[0].session_token}")

    print("1. Testing audit(mock=True)...")
    dummy_pdf = b"%PDF-1.4 dummy"
    po_f = UploadFile(filename="po.pdf", file=io.BytesIO(dummy_pdf))
    inv_f = UploadFile(filename="inv.pdf", file=io.BytesIO(dummy_pdf))
    pl_f = UploadFile(filename="pl.pdf", file=io.BytesIO(dummy_pdf))

    audit_res = await audit(
        po_pdf=po_f,
        inv_pdf=inv_f,
        pl_pdf=pl_f,
        tolerance_pct=5.0,
        mock=True
    )
    
    # Verify AuditResponse model fields
    assert audit_res.session_token, "Missing session_token"
    assert audit_res.revision_number == 1, f"Expected revision 1, got {audit_res.revision_number}"
    assert audit_res.packet is not None, "Missing packet"
    assert audit_res.summary is not None, "Missing summary"
    assert len(audit_res.discrepancies) > 0, "Missing discrepancies"
    
    # Verify flattened UI fields
    assert audit_res.shipment_ref, "Missing shipment_ref"
    assert audit_res.mid_code == "INSHRTEX2301SUR", f"Expected INSHRTEX2301SUR, got {audit_res.mid_code}"
    assert audit_res.metadata is not None, "Missing metadata"
    assert len(audit_res.po_lines) > 0, "Missing po_lines"
    assert len(audit_res.inv_lines) > 0, "Missing inv_lines"
    assert audit_res.weights is not None, "Missing weights"
    assert audit_res.cartons is not None, "Missing cartons"
    assert len(audit_res.items) > 0, "Missing items"
    assert len(audit_res.passed) > 0, "Missing passed"
    
    token = audit_res.session_token
    print(f"   Audit response verified! Token: {token}")
    print(f"   Items (UI): {len(audit_res.items)}, Passed (UI): {len(audit_res.passed)}")
    print(f"   Summary status: {audit_res.summary.status}")
    
    # Check first item structure
    first_item = audit_res.items[0]
    print(f"   First item: id={first_item.id}, sev={first_item.sev}, rule={first_item.rule_id}")
    assert first_item.sev in ("crit", "warn", "info")
    assert first_item.cells is not None
    assert first_item.locs is not None
    assert first_item.body
    assert first_item.fix

    print(f"\n2. Testing get_session(token='{token}')...")
    session_res = await get_session(token=token)
    assert session_res.session_token == token
    assert session_res.summary.status == audit_res.summary.status
    print("   Session retrieved successfully and matches audit response.")

    print(f"\n3. Testing patch_discrepancy for item {first_item.id}...")
    patch_req = PatchResolutionRequest(
        resolution_state="ACCEPTED_EXCEPTION",
        resolution_reason_chip="Commercial Approval",
        resolution_note="Buyer approval granted in unit test",
        actor_role="BUYER_WORKBENCH"
    )
    patched_audit = await patch_discrepancy(
        token=token,
        discrepancy_id=first_item.id,
        req=patch_req
    )
    patched_item = next(it for it in patched_audit.items if it.id == first_item.id)
    assert patched_item.resolution_state == "ACCEPTED_EXCEPTION"
    assert patched_item.resolution_note == "Buyer approval granted in unit test"
    print(f"   Patched item successfully: state={patched_item.resolution_state}")

    print(f"\n4. Testing re_audit with simulated revised documents...")
    # Create UploadFiles for sample_revised.pdf
    dummy_bytes = b"%PDF-1.4 sample revised invoice and packing list"
    new_inv = UploadFile(filename="sample_revised.pdf", file=io.BytesIO(dummy_bytes))
    new_pl = UploadFile(filename="sample_revised.pdf", file=io.BytesIO(dummy_bytes))

    reaudit_res = await re_audit(
        token=token,
        invoice_file=new_inv,
        packing_list_file=new_pl,
        mock=False
    )
    assert reaudit_res.revision_number == 2, f"Expected revision 2, got {reaudit_res.revision_number}"
    assert reaudit_res.summary.unresolved_count < audit_res.summary.unresolved_count
    assert len(reaudit_res.auto_resolved_in_latest_rev) > 0, "Expected auto_resolved_in_latest_rev to be populated"
    cleared_items = [it for it in reaudit_res.items if it.resolution_state == "CLEARED_BY_REVISION"]
    assert len(cleared_items) > 0, "Expected auto-resolved items in reaudit_res.items"
    assert "Cleared by Supplier Upload (Rev v2)" in cleared_items[0].resolution_reason_chip
    assert "Fixed in Rev v2" in cleared_items[0].resolution_note
    print(f"   Re-audit completed: rev={reaudit_res.revision_number}, status={reaudit_res.summary.status}")
    print(f"   Auto-resolved diff items in Rev v2: {reaudit_res.auto_resolved_in_latest_rev}")
    print(f"   Cleared item sample note: {cleared_items[0].resolution_note}")
    print(f"   Unresolved discrepancies reduced to: {reaudit_res.summary.unresolved_count}")

    # 4b. Test re_audit guardrail when shipment is 100% cleared
    remaining_item = next(it for it in reaudit_res.items if it.resolution_state in ("UNRESOLVED", "REVISION_REQUESTED"))
    await patch_discrepancy(
        token=token,
        discrepancy_id=remaining_item.id,
        req=PatchResolutionRequest(
            resolution_state="ACCEPTED_EXCEPTION",
            resolution_reason_chip="Approved Waiver",
            resolution_note="Waiver granted for final item",
            actor_role="BUYER_WORKBENCH"
        )
    )
    from fastapi import HTTPException
    guardrail_triggered = False
    try:
        await re_audit(
            token=token,
            invoice_file=UploadFile(filename="late_attempt.pdf", file=io.BytesIO(b"%PDF")),
            packing_list_file=UploadFile(filename="late_attempt.pdf", file=io.BytesIO(b"%PDF")),
            mock=False
        )
    except HTTPException as exc:
        if exc.status_code == 400 and "already cleared and locked" in str(exc.detail):
            guardrail_triggered = True
    assert guardrail_triggered, "Expected re_audit to raise HTTP 400 when shipment is already cleared and locked"
    print("   Guardrail verified: re-audit on cleared/locked packet rejected with HTTP 400!")

    # 5. Test delete_shipment
    from app.api.routes import delete_shipment
    from app.services.session_store import session_store
    print(f"\n5. Testing delete_shipment(token='{token}')...")
    del_res = delete_shipment(token)
    assert del_res["status"] == "success"
    assert session_store.get_session(token) is None
    print(f"   Successfully deleted session '{token}'!")

    print("\n=======================================================")
    print(" ALL BACKEND SERVICE & ROUTE HANDLER TESTS PASSED 100% ")
    print("=======================================================")

if __name__ == "__main__":
    asyncio.run(run_direct_tests())
