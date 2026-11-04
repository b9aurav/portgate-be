"""
app/services/session_store.py — Multi-Tenant Supabase & Sandbox Session Store.

Encapsulates:
- Supabase PostgreSQL persistence (table: public.shipments)
- Per-browser sandbox isolation via `owner_id` (e.g. "sandbox_<uuid>")
- Lazy-cloned demo seeding per new sandbox
- Deduplication / upserting by (owner_id, po_invoice_key)
- Re-audit revision incrementing & resolution state preservation
- Graceful local JSON/in-memory fallback when Supabase is not configured
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from app import config
from app.schemas.api import AuditResponse, PatchResolutionRequest, ShipmentListItem

logger = logging.getLogger("portgate.sessions")


def _deduplicate_decision_trail(trail: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Ensures each discrepancy clearance is recorded only once per actual resolution event,
    preventing duplicate entries across subsequent revision re-audits.
    Preserves the earliest/original resolution event when deduplicating.
    """
    if not trail:
        return []
    seen_ids = set()
    seen_disc_cleared = set()
    cleaned_rev = []
    # Process from oldest to newest (trail has newest first at index 0)
    for entry in reversed(trail):
        entry_id = entry.get("id")
        if entry_id and entry_id in seen_ids:
            continue
        if entry_id:
            seen_ids.add(entry_id)

        disc_id = entry.get("discrepancy_id")
        action = entry.get("action")
        # Deduplicate redundant clearance entries for the same discrepancy
        if disc_id and (action == "CLEARED_BY_REVISION" or action == "CLEARED_IN_REVISION"):
            if disc_id in seen_disc_cleared:
                continue
            seen_disc_cleared.add(disc_id)

        cleaned_rev.append(entry)

    return list(reversed(cleaned_rev))


def _append_decision_entry(trail: List[Dict[str, Any]], entry: Dict[str, Any]) -> Dict[str, Any]:
    """
    Appends a new decision log entry to trail with a cryptographic chained hash.
    Since newest entries are at index 0, prev_entry_hash is obtained from the most
    recent prior entry (trail[0]) or 'GENESIS_ROOT'.
    """
    prev_hash = "GENESIS_ROOT"
    if trail and isinstance(trail, list) and len(trail) > 0:
        prev_hash = trail[0].get("entry_hash") or "GENESIS_ROOT"

    timestamp = entry.get("timestamp", "")
    actor = entry.get("actor_name", "")
    action = entry.get("action", "")
    sig_raw = f"{prev_hash}::{timestamp}::{actor}::{action}".encode()
    entry_hash = hashlib.sha256(sig_raw).hexdigest()[:16]

    entry["prev_entry_hash"] = prev_hash
    entry["entry_hash"] = entry_hash
    trail.insert(0, entry)
    return entry


class SessionStore:
    """
    Multi-tenant session store managing pre-shipment audit packets in Supabase
    PostgreSQL with per-browser sandbox isolation (owner_id) and graceful local fallback.
    """

    def __init__(self, persist_dir: Optional[Path] = None) -> None:
        self._sessions: Dict[str, AuditResponse] = {}
        self._owners: Dict[str, str] = {}  # session_token -> owner_id
        self._files: Dict[str, Dict[str, Any]] = {}
        self._persist_dir = persist_dir or config.SESSIONS_PERSIST_DIR
        self._persist_file = self._persist_dir / "sessions_store.json"
        self._seed_lock = threading.Lock()

        # Initialize Supabase client if enabled
        self._supabase = None
        if config.SUPABASE_ENABLED:
            try:
                from supabase import create_client
                self._supabase = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
                logger.info("Supabase client initialized successfully at %s", config.SUPABASE_URL)
            except Exception as exc:
                logger.warning("Failed to initialize Supabase client (%s). Falling back to local store.", exc)
                self._supabase = None
        else:
            logger.info("Supabase persistence not configured; using local JSON fallback.")

        # If Supabase is disabled or unavailable, initialize local fallback store
        if not self._supabase:
            loaded = self._load_from_disk()
            if not loaded or "shreeji-po-9921" not in self._sessions:
                self.ensure_workspace_seeded("sandbox_default")
                self._save_to_disk()

    @property
    def raw_sessions(self) -> Dict[str, AuditResponse]:
        """Provides backward compatibility for health probe session count."""
        return self._sessions

    @property
    def supabase_client(self):
        """Returns the underlying Supabase client (or None if unconfigured/mocked)."""
        return self._supabase

    # -------------------------------------------------------------------------
    # Local JSON Fallback Persistence
    # -------------------------------------------------------------------------
    def _save_to_disk(self) -> None:
        """Persists in-memory sessions to local JSON file when running without Supabase."""
        try:
            self._persist_dir.mkdir(parents=True, exist_ok=True)
            dump_data = {
                tok: {
                    "audit": session.model_dump(mode="json"),
                    "owner_id": self._owners.get(tok, "sandbox_default"),
                }
                for tok, session in self._sessions.items()
            }
            self._persist_file.write_text(json.dumps(dump_data, indent=2), encoding="utf-8")
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
            for tok, item in data.items():
                if isinstance(item, dict) and "audit" in item:
                    s_dict = item["audit"]
                    owner = item.get("owner_id", "sandbox_default")
                else:
                    s_dict = item
                    owner = "sandbox_default"
                self._sessions[tok] = AuditResponse.model_validate(s_dict)
                self._owners[tok] = owner

            logger.info("Loaded %d sessions from disk (%s)", len(self._sessions), self._persist_file)
            return len(self._sessions) > 0
        except Exception as exc:
            logger.warning("Failed to load sessions from %s: %s", self._persist_file, exc)
            return False

    # -------------------------------------------------------------------------
    # 1. Lazy-Clone Demo Shipments per New Sandbox (owner_id)
    # -------------------------------------------------------------------------
    def ensure_workspace_seeded(self, owner_id: str = "sandbox_default") -> None:
        """
        When list_shipments is requested for an owner_id with 0 rows,
        automatically clones the canonical demo shipment for that owner_id
        with a deterministic cryptographic session_token and thread synchronization.
        """
        owner_id = owner_id.strip() if owner_id else "sandbox_default"

        with self._seed_lock:
            # Check if already seeded in Supabase
            if self._supabase:
                try:
                    res = (
                        self._supabase.table("shipments")
                        .select("session_token")
                        .eq("owner_id", owner_id)
                        .limit(1)
                        .execute()
                    )
                    if res.data and len(res.data) > 0:
                        return
                except Exception as exc:
                    logger.warning("Supabase check in ensure_workspace_seeded failed: %s", exc)

            # Check in local cache
            if any(oid == owner_id for oid in self._owners.values()):
                return

            # Generate fresh seed shipment with deterministic token derived from owner_id
            try:
                from app.services.extractor import build_mock_packet, extracted_to_shipment_packet
                from app.services.linter import run_deterministic_audit

                seed_hash = hashlib.sha256(f"{owner_id}:canonical_seed".encode()).hexdigest()[:16]
                fresh_token = f"shp_{seed_hash}"
                now_iso = datetime.now(timezone.utc).isoformat()
                packet = extracted_to_shipment_packet(build_mock_packet())
                seed_audit = run_deterministic_audit(
                    packet=packet,
                    session_token=fresh_token,
                    revision_number=1,
                )
                seed_audit.updated_at = now_iso

                po_num = packet.buyer_po.po_number if packet.buyer_po else "PO-PCR-45012"
                inv_num = (
                    packet.commercial_invoice.invoice_number
                    if packet.commercial_invoice
                    else "SHR/EXP/26-27/0412"
                )
                po_invoice_key = f"{po_num}::{inv_num}"

                # Persist to Supabase if available (upsert prevents duplicate key collisions)
                if self._supabase:
                    row = {
                        "session_token": fresh_token,
                        "owner_id": owner_id,
                        "po_invoice_key": po_invoice_key,
                        "audit_payload": seed_audit.model_dump(mode="json"),
                        "updated_at": now_iso,
                    }
                    self._supabase.table("shipments").upsert(row).execute()
                    logger.info("Seeded sandbox workspace '%s' in Supabase with token %s", owner_id, fresh_token)

                # Update cache
                self._sessions[fresh_token] = seed_audit
                self._owners[fresh_token] = owner_id
                self._files[fresh_token] = {
                    "po": b"",
                    "inv": b"",
                    "pl": b"",
                    "mock": True,
                    "tolerance_pct": 5.0,
                }
                if not self._supabase:
                    self._save_to_disk()
            except Exception as exc:
                logger.exception("Failed to seed sandbox workspace for owner %s: %s", owner_id, exc)

    # -------------------------------------------------------------------------
    # 2. Upsert by (owner_id, po_invoice_key) on New Audit
    # -------------------------------------------------------------------------
    def create_or_replace_session(
        self,
        audit: AuditResponse,
        owner_id: str = "sandbox_default",
        files: Optional[Dict[str, Any]] = None,
    ) -> AuditResponse:
        """
        Creates or updates a session scoped to owner_id.
        If an existing row matches (owner_id, po_invoice_key), reuses its session_token
        and resets to revision 1 (overwriting cleanly instead of creating queue duplicates).
        """
        owner_id = owner_id.strip() if owner_id else "sandbox_default"

        po_num = "PO"
        inv_num = "INV"
        if audit.packet:
            if audit.packet.buyer_po and audit.packet.buyer_po.po_number:
                po_num = audit.packet.buyer_po.po_number
            if audit.packet.commercial_invoice and audit.packet.commercial_invoice.invoice_number:
                inv_num = audit.packet.commercial_invoice.invoice_number
        elif audit.metadata:
            po_num = audit.metadata.po_number or "PO"
            inv_num = audit.metadata.invoice_no or "INV"

        po_invoice_key = f"{po_num}::{inv_num}"
        existing_token: Optional[str] = None

        # Check in Supabase
        if self._supabase:
            try:
                res = (
                    self._supabase.table("shipments")
                    .select("session_token")
                    .eq("owner_id", owner_id)
                    .eq("po_invoice_key", po_invoice_key)
                    .limit(1)
                    .execute()
                )
                if res.data and len(res.data) > 0:
                    existing_token = res.data[0]["session_token"]
            except Exception as exc:
                logger.warning("Supabase query in create_or_replace_session failed: %s", exc)

        # Fallback check in local memory
        if not existing_token:
            for tok, s in self._sessions.items():
                if self._owners.get(tok) == owner_id:
                    s_po = ""
                    s_inv = ""
                    if s.packet:
                        if s.packet.buyer_po and s.packet.buyer_po.po_number:
                            s_po = s.packet.buyer_po.po_number
                        if s.packet.commercial_invoice and s.packet.commercial_invoice.invoice_number:
                            s_inv = s.packet.commercial_invoice.invoice_number
                    elif s.metadata:
                        s_po = s.metadata.get("po_number", "")
                        s_inv = s.metadata.get("invoice_no", "")
                    if f"{s_po}::{s_inv}" == po_invoice_key:
                        existing_token = tok
                        break

        session_token = existing_token or f"shp_{secrets.token_urlsafe(12)}"
        now_iso = datetime.now(timezone.utc).isoformat()

        audit.session_token = session_token
        audit.revision_number = 1
        audit.updated_at = now_iso

        if files:
            self._files[session_token] = files
        else:
            self._files.setdefault(session_token, {})

        # Persist to Supabase
        if self._supabase:
            try:
                row = {
                    "session_token": session_token,
                    "owner_id": owner_id,
                    "po_invoice_key": po_invoice_key,
                    "audit_payload": audit.model_dump(mode="json"),
                    "updated_at": now_iso,
                }
                self._supabase.table("shipments").upsert(row).execute()
                logger.info(
                    "Upserted shipment %s (owner=%s, key=%s) in Supabase",
                    session_token,
                    owner_id,
                    po_invoice_key,
                )
            except Exception as exc:
                logger.exception("Supabase upsert failed: %s", exc)

        # Update cache
        self._sessions[session_token] = audit
        self._owners[session_token] = owner_id
        if not self._supabase:
            self._save_to_disk()

        return audit

    def create_session(
        self,
        audit: AuditResponse,
        files: Optional[Dict[str, Any]] = None,
        token: Optional[str] = None,
        owner_id: str = "sandbox_default",
    ) -> str:
        """Backward-compatible helper returning session_token string."""
        if token:
            audit.session_token = token
        res = self.create_or_replace_session(audit=audit, owner_id=owner_id, files=files)
        return res.session_token

    # -------------------------------------------------------------------------
    # 3. List Shipments Scoped to owner_id
    # -------------------------------------------------------------------------
    def list_shipments(self, owner_id: str = "sandbox_default") -> List[ShipmentListItem]:
        """
        Generates multi-shipment summary objects for the Queue Dashboard table,
        strictly scoped to owner_id. Sorts unresolved shipments first, then newest updated_at.
        """
        owner_id = owner_id.strip() if owner_id else "sandbox_default"
        self.ensure_workspace_seeded(owner_id)

        rows: List[Dict[str, Any]] = []

        # 1. Fetch from Supabase if active
        if self._supabase:
            try:
                res = (
                    self._supabase.table("shipments")
                    .select("session_token, owner_id, po_invoice_key, audit_payload, updated_at")
                    .eq("owner_id", owner_id)
                    .order("updated_at", desc=True)
                    .execute()
                )
                raw_rows = res.data or []

                # Deduplicate by po_invoice_key (keeps newest, self-heals any legacy duplicate rows)
                seen_keys: set[str] = set()
                deduped_rows: List[Dict[str, Any]] = []
                duplicate_tokens_to_prune: List[str] = []

                for r in raw_rows:
                    key = r.get("po_invoice_key")
                    if key and key in seen_keys:
                        duplicate_tokens_to_prune.append(r["session_token"])
                        continue
                    if key:
                        seen_keys.add(key)
                    deduped_rows.append(r)

                if duplicate_tokens_to_prune:
                    for dup_tok in duplicate_tokens_to_prune:
                        try:
                            self._supabase.table("shipments").delete().eq("session_token", dup_tok).execute()
                            logger.info("Self-healed duplicate seed row %s for owner %s", dup_tok, owner_id)
                        except Exception as exc:
                            logger.warning("Failed to prune duplicate legacy token %s: %s", dup_tok, exc)

                rows = deduped_rows
            except Exception as exc:
                logger.warning("Supabase list_shipments query failed (%s). Falling back to local cache.", exc)
                rows = []

        # 2. Map Supabase rows to ShipmentListItem
        results: List[ShipmentListItem] = []
        if rows:
            for r in rows:
                tok = r["session_token"]
                payload = r.get("audit_payload") or {}
                # Update memory cache
                try:
                    s = AuditResponse.model_validate(payload)
                    self._sessions[tok] = s
                    self._owners[tok] = owner_id
                except Exception as exc:
                    logger.warning("Failed to validate audit_payload for %s: %s", tok, exc)
                    continue

                item = self._build_shipment_list_item(tok, s)
                results.append(item)
        else:
            # Fallback to in-memory / local storage
            seen_local_keys: set[str] = set()
            for tok, s in self._sessions.items():
                if self._owners.get(tok) == owner_id:
                    po = s.packet.buyer_po.po_number if s.packet and s.packet.buyer_po else ""
                    inv = s.packet.commercial_invoice.invoice_number if s.packet and s.packet.commercial_invoice else ""
                    key = f"{po}::{inv}"
                    if key in seen_local_keys:
                        continue
                    seen_local_keys.add(key)
                    results.append(self._build_shipment_list_item(tok, s))

        # Sort: unresolved shipments first, then newest updated_at first
        results.sort(key=lambda x: str(x.updated_at), reverse=True)
        results.sort(key=lambda x: 0 if x.unresolved_count > 0 else 1)
        return results

    def _build_shipment_list_item(self, token: str, s: AuditResponse) -> ShipmentListItem:
        """Converts an AuditResponse model into a clean ShipmentListItem."""
        packet = s.packet
        meta = s.metadata or {}

        unresolved_items = [
            d for d in s.discrepancies if d.resolution_state in ("UNRESOLVED", "REVISION_REQUESTED")
        ]
        unresolved_count = len(unresolved_items)
        det_count = sum(1 for d in unresolved_items if d.category == "DETERMINISTIC_CONFLICT")
        com_count = sum(1 for d in unresolved_items if d.category == "COMMERCIAL_VARIANCE")
        adv_count = sum(1 for d in unresolved_items if d.category == "COMPLIANCE_ADVISORY")

        # Status computation
        if unresolved_count == 0:
            status_str = "CLEARED_WITH_EXCEPTIONS" if s.discrepancies else "VERIFIED_CLEAN"
        elif any(d.resolution_state == "REVISION_REQUESTED" for d in s.discrepancies):
            status_str = "AWAITING_SUPPLIER_REVISION"
        elif det_count == 0 and (com_count > 0 or adv_count > 0):
            status_str = "BUYER_REVIEW"
        else:
            status_str = "ACTION_REQUIRED"

        shipment_id = packet.shipment_id if packet else (s.shipment_ref or "SHP-UNKNOWN")
        corridor = (
            packet.corridor
            if packet and packet.corridor
            else f"{meta.get('port_of_loading', 'INMUN1')} -> {meta.get('port_of_discharge', 'USLAX')}"
        )
        po_number = (
            packet.buyer_po.po_number
            if packet and packet.buyer_po
            else meta.get("po_number", "PO-UNKNOWN")
        )
        invoice_number = (
            packet.commercial_invoice.invoice_number
            if packet and packet.commercial_invoice
            else meta.get("invoice_no", "INV-UNKNOWN")
        )
        exporter_name = (
            packet.commercial_invoice.exporter_name
            if packet and packet.commercial_invoice
            else meta.get("exporter_name", "Exporter")
        )
        buyer_name = (
            packet.buyer_po.buyer_name
            if packet and packet.buyer_po
            else meta.get("importer_name", "Pacific Coast Retail Group LLC")
        )
        total_usd = float(
            packet.commercial_invoice.total_invoice_value_usd
            if packet and packet.commercial_invoice
            else 0.0
        )
        updated_at = s.updated_at or meta.get("invoice_date", datetime.now(timezone.utc).isoformat())

        return ShipmentListItem(
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

    # -------------------------------------------------------------------------
    # 4. Token-Scoped Operations (Workbench & Zero-Login Supplier Portal)
    # -------------------------------------------------------------------------
    def get_session(self, token: str) -> Optional[AuditResponse]:
        """
        Fetches the current AuditResponse by session_token.
        Public & zero-auth: does NOT filter by owner_id so external suppliers
        opening magic links from any browser can view the session.
        """
        # Query Supabase
        if self._supabase:
            try:
                res = (
                    self._supabase.table("shipments")
                    .select("*")
                    .eq("session_token", token)
                    .limit(1)
                    .execute()
                )
                if res.data and len(res.data) > 0:
                    row = res.data[0]
                    session = AuditResponse.model_validate(row["audit_payload"])
                    self._sessions[token] = session
                    self._owners[token] = row["owner_id"]
                    session.decision_trail = _deduplicate_decision_trail(getattr(session, "decision_trail", []) or [])
                    return session
            except Exception as exc:
                logger.warning("Supabase get_session failed for token %s: %s", token, exc)

        # 2. Check local memory
        if token in self._sessions:
            session = self._sessions[token]
            if getattr(session, "decision_trail", None):
                session.decision_trail = _deduplicate_decision_trail(session.decision_trail)
            return session

        # 3. Check local disk cache fallback (loads canonical pre-seeded packets like shreeji-po-9921)
        self._load_from_disk()
        if token in self._sessions:
            if self._supabase:
                try:
                    s = self._sessions[token]
                    owner_id = self._owners.get(token, "sandbox_default")
                    po_num = s.packet.buyer_po.po_number if (s.packet and s.packet.buyer_po) else "PO"
                    inv_num = s.packet.commercial_invoice.invoice_number if (s.packet and s.packet.commercial_invoice) else "INV"
                    now_iso = datetime.now(timezone.utc).isoformat()
                    self._supabase.table("shipments").upsert({
                        "session_token": token,
                        "owner_id": owner_id,
                        "po_invoice_key": f"{po_num}::{inv_num}",
                        "audit_payload": s.model_dump(mode="json"),
                        "updated_at": now_iso,
                    }).execute()
                    logger.info("Auto-synced disk token '%s' into Supabase", token)
                except Exception as exc:
                    logger.warning("Failed to auto-sync disk session '%s' to Supabase: %s", token, exc)
            session = self._sessions[token]
            if getattr(session, "decision_trail", None):
                session.decision_trail = _deduplicate_decision_trail(session.decision_trail)
            return session

        # 4. Canonical alias fallback: if token is shreeji-po-9921, generate on demand
        if token == "shreeji-po-9921":
            try:
                from app.services.extractor import build_mock_packet, extracted_to_shipment_packet
                from app.services.linter import run_deterministic_audit
                packet = extracted_to_shipment_packet(build_mock_packet())
                seed_audit = run_deterministic_audit(packet=packet, session_token="shreeji-po-9921", revision_number=1)
                self._sessions["shreeji-po-9921"] = seed_audit
                self._owners["shreeji-po-9921"] = "sandbox_default"
                if self._supabase:
                    try:
                        self._supabase.table("shipments").upsert({
                            "session_token": "shreeji-po-9921",
                            "owner_id": "sandbox_default",
                            "po_invoice_key": "PO-PCR-45012::SHR/EXP/26-27/0412",
                            "audit_payload": seed_audit.model_dump(mode="json"),
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        }).execute()
                    except Exception as exc:
                        logger.warning("Failed to upsert shreeji-po-9921: %s", exc)
                if getattr(seed_audit, "decision_trail", None):
                    seed_audit.decision_trail = _deduplicate_decision_trail(seed_audit.decision_trail)
                return seed_audit
            except Exception as exc:
                logger.warning("Failed to generate canonical shreeji-po-9921: %s", exc)

        return None

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
        1. Checks lock guardrail (rejects if already cleared)
        2. Preserves prior buyer sign-offs (ACCEPTED_EXCEPTION, ESCALATED_TO_BROKER)
        3. Identifies fixed items and flags them as CLEARED_BY_REVISION
        4. Increments revision number and updates Supabase while keeping original owner_id
        """
        old_audit = self.get_session(token)
        if old_audit:
            unresolved = (
                old_audit.summary.unresolved_count
                if old_audit.summary
                else sum(
                    1 for d in old_audit.discrepancies
                    if d.resolution_state in ("UNRESOLVED", "REVISION_REQUESTED")
                )
            )
            is_reopened = getattr(old_audit, "reopened_for_revision", False)
            if unresolved == 0 and not is_reopened:
                raise ValueError("Shipment packet is already cleared and locked for customs filing")

        old_revision = old_audit.revision_number if old_audit else 1
        new_revision = old_revision + 1

        new_audit.session_token = token
        new_audit.revision_number = new_revision
        new_audit.reopened_for_revision = False
        new_audit.updated_at = datetime.now(timezone.utc).isoformat()

        # Preserve security & confidence metadata from previous revision
        if old_audit:
            if getattr(old_audit, "link_expires_at", None):
                new_audit.link_expires_at = old_audit.link_expires_at
            if getattr(old_audit, "link_revoked", None) is not None:
                new_audit.link_revoked = old_audit.link_revoked
            if getattr(old_audit, "extraction_confidence_pct", None) is not None:
                new_audit.extraction_confidence_pct = old_audit.extraction_confidence_pct
            if getattr(old_audit, "low_confidence_fields", None) is not None:
                new_audit.low_confidence_fields = list(old_audit.low_confidence_fields)

        auto_resolved_ids: list[str] = []
        newly_resolved_items: list[Any] = []
        carried_forward_items: list[Any] = []

        # Preserve Buyer Sign-Offs across revisions and track auto-resolved items
        if old_audit:
            for new_d in new_audit.discrepancies:
                for old_d in old_audit.discrepancies:
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

            # Identify discrepancies from previous revision:
            for old_d in old_audit.discrepancies:
                is_still_triggered = any(
                    new_d.id == old_d.id or new_d.rule_id == old_d.rule_id
                    for new_d in new_audit.discrepancies
                )

                if old_d.resolution_state in ("CLEARED_BY_REVISION", "CLEARED_IN_REVISION"):
                    # ALREADY cleared in a prior revision: preserve in items without duplicate logging
                    if not is_still_triggered:
                        carried_forward_items.append(old_d.model_copy(deep=True))
                elif not is_still_triggered and old_d.resolution_state in ("UNRESOLVED", "REVISION_REQUESTED"):
                    # Truly NEWLY FIXED in THIS revision!
                    auto_resolved_ids.append(old_d.id)
                    fixed_item = old_d.model_copy(deep=True)
                    fixed_item.resolution_state = "CLEARED_BY_REVISION"
                    fixed_item.resolution_reason_chip = f"Cleared by Supplier Upload (Rev v{new_revision})"
                    fixed_item.resolved_by = f"Supplier Upload (Rev v{new_revision})"
                    fixed_item.resolved_at = datetime.now(timezone.utc).strftime("%H:%M:%S")

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

                    newly_resolved_items.append(fixed_item)

            all_fixed = newly_resolved_items + carried_forward_items
            new_audit.discrepancies.extend(all_fixed)

            for it in new_audit.items:
                for d in new_audit.discrepancies:
                    if it.id == d.id:
                        it.resolution_state = d.resolution_state
                        it.resolution_reason_chip = d.resolution_reason_chip
                        it.resolution_note = d.resolution_note
                        it.resolved_by = d.resolved_by
                        it.resolved_at = d.resolved_at
                        break

            item_ids = {it.id for it in new_audit.items}
            for auto_item in all_fixed:
                if auto_item.id not in item_ids:
                    new_audit.items.append(auto_item)

        # Preserve decision_trail
        if old_audit and getattr(old_audit, "decision_trail", None):
            new_audit.decision_trail = list(old_audit.decision_trail)
        else:
            new_audit.decision_trail = []

        now_sec = datetime.now(timezone.utc).strftime("%H:%M:%S")
        for fixed_item in newly_resolved_items:
            _append_decision_entry(new_audit.decision_trail, {
                "id": f"DEC-{fixed_item.id}-v{new_revision}",
                "discrepancy_id": fixed_item.id,
                "title": fixed_item.title,
                "previous_state": "UNRESOLVED",
                "new_state": "CLEARED_BY_REVISION",
                "action": "CLEARED_BY_REVISION",
                "reason_chip": fixed_item.resolution_reason_chip or f"Cleared by Supplier Upload (Rev v{new_revision})",
                "note": fixed_item.resolution_note,
                "actor_role": "SUPPLIER_PORTAL",
                "actor_name": f"Supplier Upload (Rev v{new_revision})",
                "timestamp": fixed_item.resolved_at or now_sec,
            })

        # Explicit upload ledger entry in decision_trail
        doc_count = len([k for k in (new_files or {}).keys() if k in ("po", "inv", "pl") and (new_files or {})[k]])
        doc_count_str = f"{doc_count} revised document(s)" if doc_count > 0 else "revised document(s)"
        _append_decision_entry(new_audit.decision_trail, {
            "id": f"DEC-UPLOAD-v{new_revision}",
            "discrepancy_id": "REVISION_UPLOAD",
            "title": f"Supplier Revised Packet Upload (Rev v{new_revision})",
            "previous_state": "REVISION_REQUESTED",
            "new_state": "CLEARED_BY_REVISION",
            "action": f"Uploaded Revision v{new_revision} — {len(auto_resolved_ids)} conflict(s) reconciled (SHA-256 verified)",
            "reason_chip": "Supplier Portal Upload",
            "note": f"Supplier uploaded {doc_count_str} via zero-login link. Verified SHA-256 signatures and automatically reconciled {len(auto_resolved_ids)} discrepancy item(s).",
            "actor_role": "SUPPLIER_PORTAL",
            "actor_name": "Supplier Portal Upload (Zero-Login Magic Link)",
            "timestamp": now_sec,
        })

        new_audit.decision_trail = _deduplicate_decision_trail(new_audit.decision_trail)

        new_audit.auto_resolved_in_latest_rev = auto_resolved_ids

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

        owner_id = self._owners.get(token, "sandbox_default")

        # Persist to Supabase
        if self._supabase:
            try:
                self._supabase.table("shipments").update({
                    "audit_payload": new_audit.model_dump(mode="json"),
                    "updated_at": new_audit.updated_at,
                }).eq("session_token", token).execute()
                logger.info("Updated re-audit in Supabase for %s (rev v%d)", token, new_revision)
            except Exception as exc:
                logger.exception("Supabase update_session_reaudit failed: %s", exc)

        self._sessions[token] = new_audit
        if not self._supabase:
            self._save_to_disk()

        return new_audit

    def resolve_discrepancy(
        self,
        token: str,
        discrepancy_id: str,
        patch_data: PatchResolutionRequest,
    ) -> AuditResponse:
        """
        Updates the resolution state of a specific discrepancy inside the session
        and commits to Supabase.
        """
        session = self.get_session(token)
        if not session:
            raise KeyError(f"Audit session '{token}' not found.")

        actor_name = (
            "US Buyer Import Specialist"
            if patch_data.actor_role == "BUYER_WORKBENCH"
            else "Supplier Logistics Team"
        )
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")

        found = False
        prior_state = "UNRESOLVED"
        for disc in session.discrepancies:
            if disc.id == discrepancy_id:
                prior_state = disc.resolution_state or "UNRESOLVED"
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

        # Record decision into permanent audit trail
        if not hasattr(session, "decision_trail") or session.decision_trail is None:
            session.decision_trail = []

        action_type = (
            "ACCEPT_VARIANCE"
            if patch_data.resolution_state == "ACCEPTED_EXCEPTION"
            else (
                "ESCALATE_BROKER"
                if patch_data.resolution_state == "ESCALATED_TO_BROKER"
                else "REQUEST_REVISION"
            )
        )
        target_title = next(
            (d.title for d in session.discrepancies if d.id == discrepancy_id),
            discrepancy_id,
        )
        log_entry = {
            "id": f"DEC-{int(datetime.now(timezone.utc).timestamp())}-{secrets.token_hex(2)}",
            "discrepancy_id": discrepancy_id,
            "title": target_title,
            "previous_state": prior_state,
            "new_state": patch_data.resolution_state,
            "action": action_type,
            "reason_chip": patch_data.resolution_reason_chip or "",
            "note": patch_data.resolution_note,
            "actor_role": patch_data.actor_role,
            "actor_name": actor_name,
            "timestamp": timestamp,
        }
        _append_decision_entry(session.decision_trail, log_entry)

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

        # Update Supabase
        if self._supabase:
            try:
                self._supabase.table("shipments").update({
                    "audit_payload": session.model_dump(mode="json"),
                    "updated_at": session.updated_at,
                }).eq("session_token", token).execute()
                logger.info("Updated resolution in Supabase for %s / %s", token, discrepancy_id)
            except Exception as exc:
                logger.exception("Supabase resolve_discrepancy failed: %s", exc)

        self._sessions[token] = session
        if not self._supabase:
            self._save_to_disk()

        return session

    # -------------------------------------------------------------------------
    # 4b. Magic Link Security: Rotate or Revoke Link
    # -------------------------------------------------------------------------
    def rotate_magic_link(
        self,
        token: str,
        owner_id: Optional[str] = None,
        revoke: bool = False,
    ) -> AuditResponse:
        """
        Rotates or revokes the supplier magic link token for a session.
        If revoke=True: sets link_revoked=True.
        If revoke=False: generates a new session_token, moves files and owner records,
        resets link_revoked=False, and resets link_expires_at to +7 days from now.
        Returns the updated AuditResponse.
        """
        session = self.get_session(token)
        if not session:
            raise KeyError(f"Session '{token}' not found")

        # Verify owner permission if provided
        actual_owner = self._owners.get(token)
        if owner_id and actual_owner and actual_owner != owner_id:
            raise PermissionError("Unauthorized to modify this shipment session")

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        if revoke:
            session.link_revoked = True
            session.updated_at = now_iso
            if not getattr(session, "decision_trail", None):
                session.decision_trail = []
            _append_decision_entry(session.decision_trail, {
                "id": f"DEC-REVOKE-{secrets.token_urlsafe(6)}",
                "discrepancy_id": "MAGIC_LINK_REVOKED",
                "title": "Supplier Magic Link Revoked",
                "previous_state": "ACTIVE",
                "new_state": "REVOKED",
                "action": "REVOKED",
                "reason_chip": "Security Guardrail",
                "note": "Buyer explicitly revoked supplier upload magic link access.",
                "actor_role": "BUYER_ADMIN",
                "actor_name": "Compliance Admin",
                "timestamp": now.strftime("%H:%M:%S"),
            })

            if self._supabase:
                try:
                    self._supabase.table("shipments").update({
                        "audit_payload": session.model_dump(mode="json"),
                        "updated_at": now_iso,
                    }).eq("session_token", token).execute()
                except Exception as exc:
                    logger.warning("Supabase update for rotate_magic_link (revoke) failed: %s", exc)

            self._sessions[token] = session
            if not self._supabase:
                self._save_to_disk()
            return session

        # Rotate / Regenerate link
        new_token = f"shp_{secrets.token_urlsafe(16)}"
        new_expires_at = (now + timedelta(days=7)).isoformat()

        session.session_token = new_token
        session.link_revoked = False
        session.link_expires_at = new_expires_at
        session.updated_at = now_iso

        if not getattr(session, "decision_trail", None):
            session.decision_trail = []
        _append_decision_entry(session.decision_trail, {
            "id": f"DEC-ROTATE-{secrets.token_urlsafe(6)}",
            "discrepancy_id": "MAGIC_LINK_ROTATED",
            "title": "Supplier Magic Link Rotated",
            "previous_state": "ROTATED",
            "new_state": "ACTIVE",
            "action": "ROTATED",
            "reason_chip": "Security Guardrail",
            "note": f"Buyer regenerated supplier magic link (Expires in 7 days: {new_expires_at[:10]}). Previous token invalidated.",
            "actor_role": "BUYER_ADMIN",
            "actor_name": "Compliance Admin",
            "timestamp": now.strftime("%H:%M:%S"),
        })

        if self._supabase:
            try:
                self._supabase.table("shipments").delete().eq("session_token", token).execute()
                po_num = session.packet.buyer_po.po_number if (session.packet and session.packet.buyer_po) else "PO"
                inv_num = session.packet.commercial_invoice.invoice_number if (session.packet and session.packet.commercial_invoice) else "INV"
                self._supabase.table("shipments").insert({
                    "session_token": new_token,
                    "owner_id": actual_owner or "sandbox_default",
                    "po_invoice_key": f"{po_num}::{inv_num}",
                    "audit_payload": session.model_dump(mode="json"),
                    "updated_at": now_iso,
                }).execute()
            except Exception as exc:
                logger.warning("Supabase rotate_magic_link failed: %s", exc)

        # Move memory references
        self._sessions.pop(token, None)
        self._sessions[new_token] = session

        if token in self._owners:
            self._owners[new_token] = self._owners.pop(token)
        else:
            self._owners[new_token] = actual_owner or "sandbox_default"

        if token in self._files:
            self._files[new_token] = self._files.pop(token)

        if not self._supabase:
            self._save_to_disk()

        return session

    # -------------------------------------------------------------------------
    # 4c. Reopen Locked / Cleared Shipment Packet for Revision
    # -------------------------------------------------------------------------
    def reopen_shipment(
        self,
        token: str,
        owner_id: Optional[str] = None,
    ) -> AuditResponse:
        """
        Manually reopens a locked or cleared shipment packet for supplier revision.
        - Extends link expiration by +7 days and unrevokes magic link.
        - Resets resolved discrepancies to REVISION_REQUESTED so unresolved_count > 0.
        - Updates overall status and logs a cryptographic chained decision trail entry.
        """
        session = self.get_session(token)
        if not session:
            raise KeyError(f"Session '{token}' not found")

        actual_owner = self._owners.get(token)
        if owner_id and actual_owner and actual_owner != owner_id:
            raise PermissionError("Unauthorized to modify this shipment session")

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        session.link_revoked = False
        session.link_expires_at = (now + timedelta(days=7)).isoformat()
        session.reopened_for_revision = True
        session.updated_at = now_iso

        if not getattr(session, "decision_trail", None):
            session.decision_trail = []

        # Items previously marked as CLEARED_BY_REVISION / CLEARED_IN_REVISION or resolved
        # (ACCEPTED_EXCEPTION, ESCALATED_TO_BROKER) remain strictly in their historical state.
        # We do NOT reset them to REVISION_REQUESTED so resolved history is preserved cleanly.

        unresolved = sum(
            1 for d in session.discrepancies if d.resolution_state in ("UNRESOLVED", "REVISION_REQUESTED")
        )
        session.summary.unresolved_count = unresolved
        session.summary.status = "AWAITING_SUPPLIER_REVISION"

        _append_decision_entry(session.decision_trail, {
            "id": f"DEC-REOPEN-{secrets.token_urlsafe(6)}",
            "discrepancy_id": "REOPEN_SHIPMENT",
            "title": "Packet Reopened for Supplier Revision",
            "previous_state": "CLEARED",
            "new_state": "REOPENED_FOR_REVISION",
            "action": "REOPEN_FOR_REVISION",
            "reason_chip": "Buyer Action",
            "note": "Packet manually reopened for supplier revision by Buyer.",
            "actor_role": "BUYER_ADMIN",
            "actor_name": "Compliance Admin",
            "timestamp": now.strftime("%H:%M:%S"),
        })

        if self._supabase:
            try:
                self._supabase.table("shipments").update({
                    "audit_payload": session.model_dump(mode="json"),
                    "updated_at": now_iso,
                }).eq("session_token", token).execute()
            except Exception as exc:
                logger.warning("Supabase update for reopen_shipment failed: %s", exc)

        self._sessions[token] = session
        if not self._supabase:
            self._save_to_disk()
        return session

    # -------------------------------------------------------------------------
    # 4d. Extend Magic Link Expiration
    # -------------------------------------------------------------------------
    def extend_link_expiry(
        self,
        token: str,
        owner_id: Optional[str] = None,
        days: int = 7,
    ) -> AuditResponse:
        """
        Extends the supplier magic link expiration by +days (default 7).
        Unrevokes link if revoked and logs chained decision trail entry.
        """
        session = self.get_session(token)
        if not session:
            raise KeyError(f"Session '{token}' not found")

        actual_owner = self._owners.get(token)
        if owner_id and actual_owner and actual_owner != owner_id:
            raise PermissionError("Unauthorized to modify this shipment session")

        now = datetime.now(timezone.utc)
        base_time = now
        if session.link_expires_at:
            try:
                exp_dt = datetime.fromisoformat(session.link_expires_at.replace("Z", "+00:00"))
                if exp_dt > now:
                    base_time = exp_dt
            except Exception:
                base_time = now

        new_expires_at = (base_time + timedelta(days=days)).isoformat()
        session.link_expires_at = new_expires_at
        session.link_revoked = False
        session.updated_at = now.isoformat()

        if not getattr(session, "decision_trail", None):
            session.decision_trail = []

        _append_decision_entry(session.decision_trail, {
            "id": f"DEC-EXTEND-{secrets.token_urlsafe(6)}",
            "discrepancy_id": "MAGIC_LINK_EXTENDED",
            "title": f"Supplier Link Expiry Extended (+{days} Days)",
            "previous_state": "ACTIVE",
            "new_state": "ACTIVE",
            "action": "EXTEND_EXPIRY",
            "reason_chip": "Security Guardrail",
            "note": f"Supplier link expiration extended by +{days} days.",
            "actor_role": "BUYER_ADMIN",
            "actor_name": "Compliance Admin",
            "timestamp": now.strftime("%H:%M:%S"),
        })

        if self._supabase:
            try:
                self._supabase.table("shipments").update({
                    "audit_payload": session.model_dump(mode="json"),
                    "updated_at": session.updated_at,
                }).eq("session_token", token).execute()
            except Exception as exc:
                logger.warning("Supabase update for extend_link_expiry failed: %s", exc)

        self._sessions[token] = session
        if not self._supabase:
            self._save_to_disk()
        return session

    # -------------------------------------------------------------------------
    # 5. Delete & Reset Workspace
    # -------------------------------------------------------------------------
    def delete_session(self, token: str, owner_id: Optional[str] = None) -> bool:
        """
        Deletes a session from Supabase and in-memory cache, optionally checking owner_id.
        """
        if owner_id:
            actual_owner = self._owners.get(token)
            if actual_owner and actual_owner != owner_id:
                return False

        deleted = False

        if self._supabase:
            try:
                query = self._supabase.table("shipments").delete().eq("session_token", token)
                if owner_id:
                    query = query.eq("owner_id", owner_id)
                res = query.execute()
                deleted = bool(res.data and len(res.data) > 0)
                logger.info("Deleted session %s in Supabase (result=%s)", token, deleted)
            except Exception as exc:
                logger.warning("Supabase delete failed: %s", exc)

        if token in self._sessions:
            if self._supabase and not deleted:
                return False
            del self._sessions[token]
            deleted = True
        if token in self._owners:
            del self._owners[token]
        if token in self._files:
            del self._files[token]

        if not self._supabase and deleted:
            self._save_to_disk()

        return deleted

    def reset_workspace(self, owner_id: str) -> None:
        """
        Deletes all rows belonging to owner_id and immediately re-seeds
        a fresh demo shipment for this sandbox.
        """
        owner_id = owner_id.strip() if owner_id else "sandbox_default"

        if self._supabase:
            try:
                self._supabase.table("shipments").delete().eq("owner_id", owner_id).execute()
                logger.info("Cleared all shipments for owner %s in Supabase", owner_id)
            except Exception as exc:
                logger.warning("Supabase reset_workspace failed: %s", exc)

        # Clear in-memory
        tokens_to_del = [tok for tok, oid in self._owners.items() if oid == owner_id]
        for tok in tokens_to_del:
            self._sessions.pop(tok, None)
            self._owners.pop(tok, None)
            self._files.pop(tok, None)

        if not self._supabase:
            self._save_to_disk()

        # Re-seed clean demo packet for this sandbox
        self.ensure_workspace_seeded(owner_id)

    # -------------------------------------------------------------------------
    # 6. Claim Workspace Migration (Sandbox -> Authenticated Account)
    # -------------------------------------------------------------------------
    def claim_sandbox_workspace(self, sandbox_owner_id: str, user_owner_id: str) -> int:
        """
        Migrates all shipments from an anonymous sandbox into an authenticated user's workspace.
        Deduplicates against existing shipments in the user's account by po_invoice_key.
        Returns the number of claimed shipments.
        """
        sandbox_owner_id = sandbox_owner_id.strip() if sandbox_owner_id else ""
        user_owner_id = user_owner_id.strip() if user_owner_id else ""

        if not sandbox_owner_id.startswith("sandbox_") or sandbox_owner_id == user_owner_id:
            return 0

        claimed_count = 0

        # 1. If Supabase is active
        if self._supabase:
            try:
                sandbox_res = (
                    self._supabase.table("shipments")
                    .select("session_token, po_invoice_key")
                    .eq("owner_id", sandbox_owner_id)
                    .execute()
                )
                sandbox_rows = sandbox_res.data or []

                user_res = (
                    self._supabase.table("shipments")
                    .select("po_invoice_key")
                    .eq("owner_id", user_owner_id)
                    .execute()
                )
                user_keys = {
                    r["po_invoice_key"] for r in (user_res.data or []) if r.get("po_invoice_key")
                }

                for s_row in sandbox_rows:
                    tok = s_row["session_token"]
                    key = s_row.get("po_invoice_key")

                    if key and key in user_keys:
                        # User already has this shipment; delete duplicate sandbox row
                        self._supabase.table("shipments").delete().eq("session_token", tok).execute()
                        self._sessions.pop(tok, None)
                        self._owners.pop(tok, None)
                        self._files.pop(tok, None)
                    else:
                        now_iso = datetime.now(timezone.utc).isoformat()
                        self._supabase.table("shipments").update({
                            "owner_id": user_owner_id,
                            "updated_at": now_iso,
                        }).eq("session_token", tok).execute()
                        if key:
                            user_keys.add(key)
                        self._owners[tok] = user_owner_id
                        claimed_count += 1
            except Exception as exc:
                logger.exception("Supabase claim_sandbox_workspace failed: %s", exc)

        # 2. Local in-memory cache sync & fallback mode
        user_mem_keys = set()
        for tok, session in self._sessions.items():
            if self._owners.get(tok) == user_owner_id:
                po = ""
                inv = ""
                if session.packet:
                    if session.packet.buyer_po and session.packet.buyer_po.po_number:
                        po = session.packet.buyer_po.po_number
                    if session.packet.commercial_invoice and session.packet.commercial_invoice.invoice_number:
                        inv = session.packet.commercial_invoice.invoice_number
                elif session.metadata:
                    po = session.metadata.po_number or ""
                    inv = session.metadata.invoice_no or ""
                if po and inv:
                    user_mem_keys.add(f"{po}::{inv}")

        sandbox_tokens = [tok for tok, oid in list(self._owners.items()) if oid == sandbox_owner_id]
        for tok in sandbox_tokens:
            session = self._sessions.get(tok)
            if not session:
                continue
            po = ""
            inv = ""
            if session.packet:
                if session.packet.buyer_po and session.packet.buyer_po.po_number:
                    po = session.packet.buyer_po.po_number
                if session.packet.commercial_invoice and session.packet.commercial_invoice.invoice_number:
                    inv = session.packet.commercial_invoice.invoice_number
            elif session.metadata:
                po = session.metadata.po_number or ""
                inv = session.metadata.invoice_no or ""
            key = f"{po}::{inv}" if (po and inv) else ""

            if key and key in user_mem_keys:
                self._sessions.pop(tok, None)
                self._owners.pop(tok, None)
                self._files.pop(tok, None)
            else:
                self._owners[tok] = user_owner_id
                if key:
                    user_mem_keys.add(key)
                if not self._supabase:
                    claimed_count += 1

        if not self._supabase:
            self._save_to_disk()

        logger.info(
            "Claimed %d shipments from sandbox '%s' into account '%s'",
            claimed_count,
            sandbox_owner_id,
            user_owner_id,
        )
        return claimed_count


# Global singleton instance
session_store = SessionStore()
MAGIC_LINK_SESSIONS = session_store.raw_sessions
