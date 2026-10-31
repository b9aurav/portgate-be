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
from app.schemas.api import AuditResponse, PatchResolutionRequest

logger = logging.getLogger("portgate.sessions")


class SessionStore:
    """
    Manages in-memory audit sessions and file bytes with state preservation
    and optional disk persistence.
    """

    def __init__(self, persist_dir: Optional[Path] = None) -> None:
        self._sessions: Dict[str, AuditResponse] = {}
        self._files: Dict[str, Dict[str, Any]] = {}
        self._persist_dir = persist_dir or config.SESSIONS_PERSIST_DIR
        self._seed_default_demo_session()

    def _seed_default_demo_session(self) -> None:
        """Seeds canonical demo session so test links work immediately."""
        token = "shreeji-po-9921"
        try:
            from app.services.extractor import build_mock_packet, extracted_to_shipment_packet
            from app.services.linter import run_deterministic_audit

            packet = extracted_to_shipment_packet(build_mock_packet())
            default_audit = run_deterministic_audit(
                packet=packet,
                session_token=token,
                revision_number=1,
            )
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
        self._sessions[session_token] = audit

        if files:
            self._files[session_token] = files
        else:
            self._files.setdefault(session_token, {})

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

        # Preserve Buyer Sign-Offs across revisions
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

            # Also mirror preserved resolutions to items list
            for it in new_audit.items:
                for d in new_audit.discrepancies:
                    if it.id == d.id:
                        it.resolution_state = d.resolution_state
                        it.resolution_reason_chip = d.resolution_reason_chip
                        it.resolution_note = d.resolution_note
                        it.resolved_by = d.resolved_by
                        it.resolved_at = d.resolved_at
                        break

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

        logger.info(
            "Discrepancy %s updated: state=%s unresolved=%d",
            discrepancy_id,
            patch_data.resolution_state,
            unresolved,
        )
        return session

    @property
    def raw_sessions(self) -> Dict[str, AuditResponse]:
        """Direct access for legacy tests or inspection."""
        return self._sessions


# Global singleton instance
session_store = SessionStore()
MAGIC_LINK_SESSIONS = session_store.raw_sessions

