"""
app/services/session_store.py — Magic Link Session & Document Store.

Encapsulates dynamic session storage, re-audit revision numbering,
and resolution state preservation across re-extractions.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from app import config
from app.schemas.api import AuditResponse, PatchResolutionRequest, ShipmentListItem

logger = logging.getLogger("portgate.sessions")


class SessionStore:
    """
    Manages in-memory audit sessions and file bytes with state preservation
    and disk persistence across server restarts.
    """

    def __init__(self, persist_dir: Optional[Path] = None) -> None:
        self._sessions: Dict[str, AuditResponse] = {}
        self._files: Dict[str, Dict[str, Any]] = {}
        self._persist_dir = persist_dir or config.SESSIONS_PERSIST_DIR
        self._persist_file = self._persist_dir / "sessions_store.json"

        # Attempt to load persisted sessions; seed canonical demo if missing
        loaded = self._load_from_disk()
        if not loaded or "shreeji-po-9921" not in self._sessions:
            self._seed_default_demo_sessions()
            self._save_to_disk()

    def _save_to_disk(self) -> None:
        """Persists all current sessions to a local JSON file."""
        try:
            self._persist_dir.mkdir(parents=True, exist_ok=True)
            dump_data = {
                tok: session.model_dump(mode="json")
                for tok, session in self._sessions.items()
            }
            self._persist_file.write_text(json.dumps(dump_data, indent=2), encoding="utf-8")
            logger.debug("Persisted %d sessions to disk at %s", len(dump_data), self._persist_file)
        except Exception as exc:
            logger.warning("Failed to persist sessions to disk: %s", exc)

    def _load_from_disk(self) -> bool:
        """Loads sessions from local JSON file if present."""
        if not self._persist_file.exists():
            return False
        try:
            raw = self._persist_file.read_text(encoding="utf-8")
            if not raw.strip():
                return False
            data = json.loads(raw)
            if not isinstance(data, dict):
                return False
            for tok, s_dict in data.items():
                self._sessions[tok] = AuditResponse.model_validate(s_dict)
            logger.info("Loaded %d sessions from disk (%s)", len(self._sessions), self._persist_file)
            return len(self._sessions) > 0
        except Exception as exc:
            logger.warning("Failed to load sessions from %s: %s", self._persist_file, exc)
            return False

    def _seed_default_demo_sessions(self) -> None:
        """Seeds canonical demo sessions so dashboard has rich data out-of-the-box."""
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            from app.services.extractor import build_mock_packet, extracted_to_shipment_packet
            from app.services.linter import run_deterministic_audit

            # 1. Canonical Shreeji Textiles (Action Required, Rev 1)
            token = "shreeji-po-9921"
            if token not in self._sessions:
                packet = extracted_to_shipment_packet(build_mock_packet())
                default_audit = run_deterministic_audit(
                    packet=packet,
                    session_token=token,
                    revision_number=1,
                )
                default_audit.updated_at = now_iso
                self._sessions[token] = default_audit
                self._files[token] = {
                    "po": b"",
                    "inv": b"",
                    "pl": b"",
                    "mock": True,
                    "tolerance_pct": 5.0,
                }
                logger.info("Seeded canonical demo session '%s'", token)
        except Exception as exc:
            logger.warning("Failed to seed default session: %s", exc)

    def create_session(
        self,
        audit: AuditResponse,
        files: Optional[Dict[str, Any]] = None,
        token: Optional[str] = None,
    ) -> str:
        """
        Creates and stores a new audit session with a unique URL-safe token.
        """
        session_token = token or secrets.token_urlsafe(10)
        audit.session_token = session_token
        audit.revision_number = 1
        audit.updated_at = datetime.now(timezone.utc).isoformat()
        self._sessions[session_token] = audit

        if files:
            self._files[session_token] = files
        else:
            self._files.setdefault(session_token, {})

        self._save_to_disk()
        logger.info(
            "Created session %s (status=%s, findings=%d)",
            session_token,
            audit.summary.status,
            audit.summary.total_findings,
        )
        return session_token

    def get_session(self, token: str) -> Optional[AuditResponse]:
        """Fetches the current AuditResponse for the given token."""
        return self._sessions.get(token)

    def get_session_files(self, token: str) -> Dict[str, Any]:
        """Returns the stored uploaded file bytes and metadata for the session."""
        return self._files.setdefault(token, {})

    def update_session_reaudit(
        self,
        token: str,
        new_audit: AuditResponse,
        new_files: Optional[Dict[str, Any]] = None,
    ) -> AuditResponse:
        """
        Applies a revised audit against an existing session:
        1. Increments revision_number (old + 1)
        2. Preserves prior buyer resolutions (ACCEPTED_EXCEPTION, ESCALATED_TO_BROKER)
        3. Recalculates summary status and unresolved counts
        4. Updates stored files and saves to session store
        """
        old_audit = self._sessions.get(token)
        old_revision = old_audit.revision_number if old_audit else 1
        new_revision = old_revision + 1

        new_audit.session_token = token
        new_audit.revision_number = new_revision
        new_audit.updated_at = datetime.now(timezone.utc).isoformat()

        auto_resolved_ids: list[str] = []
        auto_resolved_items: list[Any] = []

        # Preserve Buyer Sign-Offs across revisions and track auto-resolved items
        if old_audit:
            for new_d in new_audit.discrepancies:
                for old_d in old_audit.discrepancies:
                    # Match by id or rule_id
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

            # Identify discrepancies from previous revision that are now FIXED by supplier
            for old_d in old_audit.discrepancies:
                # Check if this discrepancy is still triggered in new_audit
                is_still_triggered = any(
                    new_d.id == old_d.id or new_d.rule_id == old_d.rule_id
                    for new_d in new_audit.discrepancies
                )

                if not is_still_triggered:
                    auto_resolved_ids.append(old_d.id)
                    fixed_item = old_d.model_copy(deep=True)
                    fixed_item.resolution_state = "CLEARED_BY_REVISION"
                    fixed_item.resolution_reason_chip = f"Cleared by Supplier Upload (Rev v{new_revision})"
                    fixed_item.resolved_by = f"Supplier Upload (Rev v{new_revision})"
                    fixed_item.resolved_at = datetime.now(timezone.utc).strftime("%H:%M:%S")

                    # Extract specific fixed values for the note
                    rule_key = (old_d.rule_id or old_d.id or "").upper()
                    if "CARTON" in rule_key:
                        inv_ctns = sum(new_audit.cartons.get("inv", [])) if isinstance(new_audit.cartons, dict) else 0
                        pl_ctns = sum(new_audit.cartons.get("pl", [])) if isinstance(new_audit.cartons, dict) else 0
                        if inv_ctns and pl_ctns:
                            fixed_item.resolution_note = f"Fixed in Rev v{new_revision} ({inv_ctns} CTNS = {pl_ctns} CTNS)"
                        else:
                            fixed_item.resolution_note = f"Fixed in Rev v{new_revision}: Total carton count reconciled"
                    elif "WEIGHT" in rule_key:
                        fixed_item.resolution_note = f"Fixed in Rev v{new_revision}: Tare & gross weights re-weighed (Net <= Gross verified)"
                    elif "MATH" in rule_key or "PRICE" in rule_key:
                        fixed_item.resolution_note = f"Fixed in Rev v{new_revision}: Unit rates and extended line arithmetic reconciled"
                    elif "HSN" in rule_key or "HS" in rule_key:
                        fixed_item.resolution_note = f"Fixed in Rev v{new_revision}: 6-digit HS root harmonized with Buyer PO schedule"
                    else:
                        fixed_item.resolution_note = f"Fixed in Rev v{new_revision}: Re-audited and reconciled in revised documents"

                    auto_resolved_items.append(fixed_item)
                elif old_d.resolution_state == "CLEARED_BY_REVISION":
                    # Retain earlier auto-resolved items from v2, v3
                    auto_resolved_items.append(old_d.model_copy(deep=True))

            # Include auto_resolved_items in discrepancies & items for the Resolved tab
            new_audit.discrepancies.extend(auto_resolved_items)

            # Mirror preserved resolutions to items list
            for it in new_audit.items:
                for d in new_audit.discrepancies:
                    if it.id == d.id:
                        it.resolution_state = d.resolution_state
                        it.resolution_reason_chip = d.resolution_reason_chip
                        it.resolution_note = d.resolution_note
                        it.resolved_by = d.resolved_by
                        it.resolved_at = d.resolved_at
                        break

            # Also ensure all auto_resolved_items are present in items list
            item_ids = {it.id for it in new_audit.items}
            for auto_item in auto_resolved_items:
                if auto_item.id not in item_ids:
                    new_audit.items.append(auto_item)

        new_audit.auto_resolved_in_latest_rev = auto_resolved_ids

        # Recompute summary with preserved resolutions
        unresolved = sum(
            1 for d in new_audit.discrepancies if d.resolution_state in ("UNRESOLVED", "REVISION_REQUESTED")
        )
        new_audit.summary.unresolved_count = unresolved
        new_audit.summary.status = (
            "ACTION_REQUIRED"
            if unresolved > 0
            else ("CLEARED_WITH_EXCEPTIONS" if new_audit.discrepancies else "VERIFIED_CLEAN")
        )

        if new_files:
            stored = self._files.setdefault(token, {})
            stored.update(new_files)

        self._sessions[token] = new_audit
        self._save_to_disk()
        logger.info(
            "Session %s updated to rev v%d (status=%s, unresolved=%d)",
            token,
            new_revision,
            new_audit.summary.status,
            unresolved,
        )
        return new_audit

    def resolve_discrepancy(
        self,
        token: str,
        discrepancy_id: str,
        patch_data: PatchResolutionRequest,
    ) -> AuditResponse:
        """
        Updates the resolution state of a specific discrepancy inside the session.
        """
        session = self._sessions.get(token)
        if not session:
            raise KeyError(f"Audit session '{token}' not found.")

        actor_name = (
            "US Buyer Import Specialist"
            if patch_data.actor_role == "BUYER_WORKBENCH"
            else "Supplier Logistics Team"
        )
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")

        found = False
        for disc in session.discrepancies:
            if disc.id == discrepancy_id:
                disc.resolution_state = patch_data.resolution_state
                disc.resolution_reason_chip = patch_data.resolution_reason_chip
                disc.resolution_note = patch_data.resolution_note
                disc.resolved_by = actor_name
                disc.resolved_at = timestamp
                found = True
                break

        for it in session.items:
            if it.id == discrepancy_id:
                it.resolution_state = patch_data.resolution_state
                it.resolution_reason_chip = patch_data.resolution_reason_chip
                it.resolution_note = patch_data.resolution_note
                it.resolved_by = actor_name
                it.resolved_at = timestamp
                found = True
                break

        if not found:
            raise ValueError(f"Discrepancy '{discrepancy_id}' not found in audit session.")

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
        session.updated_at = datetime.now(timezone.utc).isoformat()

        self._save_to_disk()
        logger.info(
            "Discrepancy %s updated: state=%s unresolved=%d",
            discrepancy_id,
            patch_data.resolution_state,
            unresolved,
        )
        return session

    def delete_session(self, token: str) -> bool:
        """
        Deletes a session from in-memory cache and persisted storage on disk.
        """
        deleted = False
        if token in self._sessions:
            del self._sessions[token]
            deleted = True
        if token in self._files:
            del self._files[token]
            deleted = True
        if deleted:
            self._save_to_disk()
            logger.info("Session %s deleted", token)
        return deleted

    def list_shipments(self) -> list[ShipmentListItem]:
        """
        Generates multi-shipment summary objects for the Queue Dashboard table.
        Sorts unresolved shipments first, ordered by most recent updated_at.
        """
        results: list[ShipmentListItem] = []
        for token, s in self._sessions.items():
            packet = s.packet
            meta = s.metadata or {}

            unresolved_items = [
                d for d in s.discrepancies if d.resolution_state in ("UNRESOLVED", "REVISION_REQUESTED")
            ]
            unresolved_count = len(unresolved_items)
            det_count = sum(1 for d in unresolved_items if d.category == "DETERMINISTIC_CONFLICT")
            com_count = sum(1 for d in unresolved_items if d.category == "COMMERCIAL_VARIANCE")
            adv_count = sum(1 for d in unresolved_items if d.category == "COMPLIANCE_ADVISORY")

            # Granular status calculation
            if unresolved_count == 0:
                status_str = "CLEARED_WITH_EXCEPTIONS" if s.discrepancies else "VERIFIED_CLEAN"
            elif any(d.resolution_state == "REVISION_REQUESTED" for d in s.discrepancies):
                status_str = "AWAITING_SUPPLIER_REVISION"
            elif det_count == 0 and (com_count > 0 or adv_count > 0):
                status_str = "BUYER_REVIEW"
            else:
                status_str = "ACTION_REQUIRED"

            shipment_id = packet.shipment_id or s.shipment_ref or "SHP-UNKNOWN"
            corridor = packet.corridor or f"{meta.get('port_of_loading', 'INMUN1')} -> {meta.get('port_of_discharge', 'USLAX')}"
            po_number = packet.buyer_po.po_number if packet.buyer_po else meta.get("po_number", "PO-UNKNOWN")
            invoice_number = packet.commercial_invoice.invoice_number if packet.commercial_invoice else meta.get("invoice_no", "INV-UNKNOWN")
            exporter_name = packet.commercial_invoice.exporter_name if packet.commercial_invoice else meta.get("exporter_name", "Exporter")
            buyer_name = packet.buyer_po.buyer_name if packet.buyer_po else meta.get("importer_name", "Pacific Coast Retail Group LLC")
            total_usd = float(packet.commercial_invoice.total_invoice_value_usd if packet.commercial_invoice else 0.0)

            updated_at = s.updated_at or meta.get("invoice_date", datetime.now(timezone.utc).isoformat())

            results.append(
                ShipmentListItem(
                    session_token=token,
                    shipment_id=shipment_id,
                    corridor=corridor,
                    po_number=po_number,
                    invoice_number=invoice_number,
                    exporter_name=exporter_name,
                    buyer_name=buyer_name,
                    total_invoice_usd=total_usd,
                    revision_number=s.revision_number,
                    status=status_str,
                    unresolved_count=unresolved_count,
                    deterministic_conflict_count=det_count,
                    commercial_variance_count=com_count,
                    compliance_advisory_count=adv_count,
                    updated_at=updated_at,
                )
            )

        # Sort: unresolved shipments first, then newest updated_at first
        results.sort(key=lambda x: str(x.updated_at), reverse=True)
        results.sort(key=lambda x: 0 if x.unresolved_count > 0 else 1)
        return results

    @property
    def raw_sessions(self) -> Dict[str, AuditResponse]:
        """Direct access for legacy tests or inspection."""
        return self._sessions


# Global singleton instance
session_store = SessionStore()
MAGIC_LINK_SESSIONS = session_store.raw_sessions

