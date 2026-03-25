"""Tests for core data models."""

import pytest
from virtualize.core.models import (
    AuditAction,
    AuditEvent,
    DiskConfig,
    DiskFormat,
    ExecRequest,
    ExecResult,
    GPUConfig,
    GPUMode,
    NetworkConfig,
    NetworkMode,
    ResourceLimits,
    VMConfig,
    VMInstance,
    VMStatus,
)


class TestVMConfig:
    def test_defaults(self):
        config = VMConfig(name="test-vm")
        assert config.vcpus == 2
        assert config.memory_mb == 2048
        assert config.disk.size_gb == 20
        assert config.disk.format == DiskFormat.QCOW2
        assert config.gpu.mode == GPUMode.NONE
        assert config.network.mode == NetworkMode.NAT

    def test_custom_config(self):
        config = VMConfig(
            name="beefy",
            vcpus=16,
            memory_mb=65536,
            disk=DiskConfig(size_gb=500, format=DiskFormat.RAW),
            gpu=GPUConfig(mode=GPUMode.PASSTHROUGH, device_id="0000:01:00.0"),
            network=NetworkConfig(mode=NetworkMode.BRIDGE),
        )
        assert config.vcpus == 16
        assert config.memory_mb == 65536
        assert config.disk.size_gb == 500
        assert config.gpu.device_id == "0000:01:00.0"

    def test_name_validation(self):
        with pytest.raises(Exception):
            VMConfig(name="")

    def test_resource_limits(self):
        limits = ResourceLimits(max_cpu_percent=50.0, timeout_seconds=120)
        assert limits.max_cpu_percent == 50.0
        assert limits.timeout_seconds == 120

    def test_labels(self):
        config = VMConfig(name="labeled", labels={"env": "dev", "team": "ml"})
        assert config.labels["env"] == "dev"


class TestVMInstance:
    def test_creation(self):
        config = VMConfig(name="test")
        instance = VMInstance(config=config)
        assert instance.id  # auto-generated
        assert len(instance.id) == 12
        assert instance.status == VMStatus.CREATING
        assert instance.pid is None

    def test_uptime_not_started(self):
        instance = VMInstance(config=VMConfig(name="test"))
        assert instance.uptime_seconds is None


class TestExecModels:
    def test_exec_request(self):
        req = ExecRequest(vm_id="abc123", command="ls -la")
        assert req.language == "bash"
        assert req.timeout_seconds == 60

    def test_exec_request_code(self):
        req = ExecRequest(vm_id="abc123", code="print('hello')", language="python")
        assert req.code == "print('hello')"

    def test_exec_result(self):
        result = ExecResult(vm_id="abc123", exit_code=0, stdout="hello\n", stderr="", duration_seconds=0.5)
        assert not result.timed_out


class TestAuditEvent:
    def test_creation(self):
        event = AuditEvent(action=AuditAction.VM_CREATE, actor="test-user", resource_id="vm-123")
        assert event.id  # auto-generated
        assert event.success is True
        assert event.timestamp is not None

    def test_failure_event(self):
        event = AuditEvent(
            action=AuditAction.VM_START,
            actor="test-user",
            resource_id="vm-123",
            success=False,
            error_message="QEMU not found",
        )
        assert not event.success
        assert event.error_message == "QEMU not found"
