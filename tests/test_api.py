"""Tests for the REST API server."""

import tempfile
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from virtualize.api.server import app


@pytest.fixture
def client(tmp_path):
    """Create a test client with an isolated temp audit directory."""
    with patch("virtualize.compliance.audit.DEFAULT_AUDIT_DIR", tmp_path / "audit"):
        with TestClient(app) as c:
            yield c


class TestHealth:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    def test_dashboard(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Virtualize" in resp.text


class TestVMEndpoints:
    def test_list_empty(self, client):
        resp = client.get("/api/v1/vms")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_create_vm(self, client):
        resp = client.post("/api/v1/vms", json={"name": "test-vm", "vcpus": 1, "memory_mb": 512})
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "test-vm"
        assert data["status"] == "creating"
        assert data["vcpus"] == 1
        assert "id" in data

    def test_create_and_list(self, client):
        client.post("/api/v1/vms", json={"name": "vm-1"})
        client.post("/api/v1/vms", json={"name": "vm-2"})
        resp = client.get("/api/v1/vms")
        assert len(resp.json()) == 2

    def test_get_vm(self, client):
        create_resp = client.post("/api/v1/vms", json={"name": "findme"})
        vm_id = create_resp.json()["id"]
        resp = client.get(f"/api/v1/vms/{vm_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "findme"

    def test_get_nonexistent(self, client):
        resp = client.get("/api/v1/vms/nonexistent")
        assert resp.status_code == 404

    def test_destroy_vm(self, client):
        create_resp = client.post("/api/v1/vms", json={"name": "doomed"})
        vm_id = create_resp.json()["id"]
        resp = client.delete(f"/api/v1/vms/{vm_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "destroyed"


class TestComplianceEndpoints:
    def test_report(self, client):
        resp = client.get("/api/v1/compliance/report/soc2")
        assert resp.status_code == 200
        data = resp.json()
        assert data["framework"] == "soc2"
        assert data["compliant"] is True

    def test_controls(self, client):
        resp = client.get("/api/v1/compliance/controls")
        assert resp.status_code == 200
        assert len(resp.json()) > 0

    def test_invalid_framework(self, client):
        resp = client.get("/api/v1/compliance/report/invalid")
        assert resp.status_code == 400


class TestAuditEndpoints:
    def test_audit_events(self, client):
        resp = client.get("/api/v1/audit/events")
        assert resp.status_code == 200

    def test_audit_verify(self, client):
        resp = client.get("/api/v1/audit/verify")
        assert resp.status_code == 200
        assert resp.json()["valid"] is True


class TestSystemInfo:
    def test_system_info(self, client):
        resp = client.get("/api/v1/system/info")
        assert resp.status_code == 200
        data = resp.json()
        assert "platform" in data
        assert "cpu_count" in data
        assert "qemu_available" in data
