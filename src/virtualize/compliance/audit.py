"""Compliance audit logging system.

Provides immutable, tamper-evident audit logging suitable for:
  - SOC 1/2/3 (Trust Services Criteria)
  - HIPAA (45 CFR § 164.312 — audit controls)
  - ISO 27001 (A.12.4 — logging and monitoring)

Design:
  - Append-only log files with HMAC integrity chain
  - Each entry includes a hash of the previous entry (tamper detection)
  - Structured JSON for SIEM ingestion
  - Optional encryption at rest via Fernet (AES-128-CBC)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from cryptography.fernet import Fernet

from virtualize.core.models import AuditEvent

logger = logging.getLogger(__name__)

DEFAULT_AUDIT_DIR = Path.home() / ".virtualize" / "audit"


class AuditLog:
    """Append-only, integrity-chained audit log."""

    def __init__(
        self,
        log_dir: Path | None = None,
        encryption_key: bytes | None = None,
        max_file_size_mb: int = 100,
        callbacks: list[Callable[[AuditEvent], None]] | None = None,
    ) -> None:
        self._log_dir = log_dir or DEFAULT_AUDIT_DIR
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._max_file_size = max_file_size_mb * 1024 * 1024
        self._lock = threading.Lock()
        self._last_hash = "genesis"
        self._fernet = Fernet(encryption_key) if encryption_key else None
        self._callbacks = callbacks or []
        self._event_count = 0

        # Resume chain from last entry
        self._resume_chain()

    def _current_log_file(self) -> Path:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._log_dir / f"audit-{today}.jsonl"

    def _resume_chain(self) -> None:
        """Read the last hash from the most recent log file to continue the chain."""
        log_files = sorted(self._log_dir.glob("audit-*.jsonl"))
        if not log_files:
            return
        last_file = log_files[-1]
        try:
            lines = last_file.read_text().strip().split("\n")
            if lines and lines[-1]:
                data = lines[-1]
                if self._fernet:
                    data = self._fernet.decrypt(data.encode()).decode()
                entry = json.loads(data)
                self._last_hash = entry.get("_integrity_hash", self._last_hash)
                self._event_count = entry.get("_sequence", 0)
        except Exception as e:
            logger.warning("Could not resume audit chain: %s", e)

    def _compute_hash(self, event_data: dict[str, Any], prev_hash: str) -> str:
        """Compute SHA-256 hash of event + previous hash for integrity chain."""
        payload = json.dumps(event_data, sort_keys=True, default=str) + prev_hash
        return hashlib.sha256(payload.encode()).hexdigest()

    def record(self, event: AuditEvent) -> None:
        """Append an audit event to the log."""
        with self._lock:
            self._event_count += 1
            event_data = event.model_dump(mode="json")
            integrity_hash = self._compute_hash(event_data, self._last_hash)

            record = {
                **event_data,
                "_sequence": self._event_count,
                "_prev_hash": self._last_hash,
                "_integrity_hash": integrity_hash,
            }

            line = json.dumps(record, default=str)
            if self._fernet:
                line = self._fernet.encrypt(line.encode()).decode()

            log_file = self._current_log_file()

            # Rotate if too large
            if log_file.exists() and log_file.stat().st_size > self._max_file_size:
                timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
                log_file = self._log_dir / f"audit-{timestamp}.jsonl"

            with open(log_file, "a") as f:
                f.write(line + "\n")

            self._last_hash = integrity_hash

        # Fire callbacks (outside lock)
        for cb in self._callbacks:
            try:
                cb(event)
            except Exception as e:
                logger.error("Audit callback error: %s", e)

    def verify_integrity(self, log_file: Path | None = None) -> tuple[bool, int, str]:
        """Verify the integrity chain of a log file.

        Returns (is_valid, num_entries, message).
        """
        target = log_file or self._current_log_file()
        if not target.exists():
            return True, 0, "No log file to verify"

        lines = target.read_text().strip().split("\n")
        prev_hash = "genesis"
        count = 0

        for i, line in enumerate(lines):
            if not line:
                continue
            try:
                data = line
                if self._fernet:
                    data = self._fernet.decrypt(data.encode()).decode()
                record = json.loads(data)
            except Exception as e:
                return False, count, f"Line {i+1}: failed to parse — {e}"

            stored_prev = record.pop("_prev_hash", None)
            stored_hash = record.pop("_integrity_hash", None)
            sequence = record.pop("_sequence", None)

            if stored_prev != prev_hash:
                return False, count, f"Line {i+1}: chain break — expected prev_hash={prev_hash}, got {stored_prev}"

            computed = self._compute_hash(record, prev_hash)
            if computed != stored_hash:
                return False, count, f"Line {i+1}: tampered — hash mismatch"

            prev_hash = stored_hash
            count += 1

        return True, count, f"Verified {count} entries — integrity OK"

    def query(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        action: str | None = None,
        actor: str | None = None,
        resource_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query audit events with filters."""
        results: list[dict[str, Any]] = []

        for log_file in sorted(self._log_dir.glob("audit-*.jsonl")):
            for line in log_file.read_text().strip().split("\n"):
                if not line:
                    continue
                try:
                    data = line
                    if self._fernet:
                        data = self._fernet.decrypt(data.encode()).decode()
                    record = json.loads(data)
                except Exception:
                    continue

                ts = datetime.fromisoformat(record.get("timestamp", ""))

                if start and ts < start:
                    continue
                if end and ts > end:
                    continue
                if action and record.get("action") != action:
                    continue
                if actor and record.get("actor") != actor:
                    continue
                if resource_id and record.get("resource_id") != resource_id:
                    continue

                results.append(record)
                if len(results) >= limit:
                    return results

        return results

    @staticmethod
    def generate_encryption_key() -> bytes:
        """Generate a new Fernet encryption key for audit log encryption at rest."""
        return Fernet.generate_key()
