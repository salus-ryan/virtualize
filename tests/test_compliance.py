"""Tests for compliance audit logging and policies."""

import json
import tempfile
from pathlib import Path

import pytest

from virtualize.compliance.audit import AuditLog
from virtualize.compliance.policies import (
    ComplianceFramework,
    generate_report,
    get_controls,
)
from virtualize.core.models import AuditAction, AuditEvent


class TestAuditLog:
    def setup_method(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.log = AuditLog(log_dir=self.tmpdir)

    def test_record_and_query(self):
        event = AuditEvent(action=AuditAction.VM_CREATE, actor="test", resource_id="vm-1")
        self.log.record(event)

        events = self.log.query(actor="test")
        assert len(events) == 1
        assert events[0]["action"] == "vm.create"
        assert events[0]["actor"] == "test"

    def test_integrity_chain(self):
        for i in range(5):
            event = AuditEvent(action=AuditAction.VM_EXEC, actor="test", resource_id=f"vm-{i}")
            self.log.record(event)

        valid, count, message = self.log.verify_integrity()
        assert valid is True
        assert count == 5

    def test_tamper_detection(self):
        for i in range(3):
            event = AuditEvent(action=AuditAction.VM_CREATE, actor="test", resource_id=f"vm-{i}")
            self.log.record(event)

        # Tamper with the log
        log_files = list(self.tmpdir.glob("audit-*.jsonl"))
        assert len(log_files) == 1
        lines = log_files[0].read_text().strip().split("\n")
        record = json.loads(lines[1])
        record["actor"] = "hacker"
        lines[1] = json.dumps(record)
        log_files[0].write_text("\n".join(lines) + "\n")

        valid, count, message = self.log.verify_integrity()
        assert valid is False
        assert "tampered" in message.lower() or "chain break" in message.lower()

    def test_encrypted_log(self):
        key = AuditLog.generate_encryption_key()
        encrypted_log = AuditLog(log_dir=self.tmpdir / "encrypted", encryption_key=key)

        event = AuditEvent(action=AuditAction.VM_START, actor="secure-user", resource_id="vm-1")
        encrypted_log.record(event)

        # Verify raw file is not plaintext
        log_files = list((self.tmpdir / "encrypted").glob("audit-*.jsonl"))
        raw = log_files[0].read_text().strip()
        assert "secure-user" not in raw  # encrypted

        # But can still query
        events = encrypted_log.query(actor="secure-user")
        assert len(events) == 1

        # And verify integrity
        valid, count, _ = encrypted_log.verify_integrity()
        assert valid is True
        assert count == 1

    def test_query_filters(self):
        self.log.record(AuditEvent(action=AuditAction.VM_CREATE, actor="alice", resource_id="vm-1"))
        self.log.record(AuditEvent(action=AuditAction.VM_START, actor="bob", resource_id="vm-1"))
        self.log.record(AuditEvent(action=AuditAction.VM_EXEC, actor="alice", resource_id="vm-2"))

        assert len(self.log.query(actor="alice")) == 2
        assert len(self.log.query(actor="bob")) == 1
        assert len(self.log.query(action="vm.create")) == 1
        assert len(self.log.query(resource_id="vm-1")) == 2


class TestPolicies:
    def test_get_all_controls(self):
        controls = get_controls()
        assert len(controls) > 0

    def test_filter_by_framework(self):
        soc2 = get_controls(ComplianceFramework.SOC2)
        hipaa = get_controls(ComplianceFramework.HIPAA)
        assert len(soc2) > 0
        assert len(hipaa) > 0
        assert all(c.framework == ComplianceFramework.SOC2 for c in soc2)

    def test_generate_report(self):
        report = generate_report(ComplianceFramework.SOC2)
        assert report.framework == ComplianceFramework.SOC2
        assert report.total_controls > 0
        assert report.compliant is True  # all enabled by default

    def test_all_frameworks_have_controls(self):
        for fw in ComplianceFramework:
            controls = get_controls(fw)
            assert len(controls) > 0, f"No controls for {fw}"
