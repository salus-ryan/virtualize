"""Core data models for Virtualize."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class VMStatus(str, enum.Enum):
    CREATING = "creating"
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    ERROR = "error"
    DESTROYED = "destroyed"


class GPUMode(str, enum.Enum):
    NONE = "none"
    PASSTHROUGH = "passthrough"  # VFIO / direct assignment
    VIRTUAL = "virtual"  # virtio-gpu / virtual display


class DiskFormat(str, enum.Enum):
    QCOW2 = "qcow2"
    RAW = "raw"
    VDI = "vdi"
    VMDK = "vmdk"


class NetworkMode(str, enum.Enum):
    NAT = "nat"
    BRIDGE = "bridge"
    ISOLATED = "isolated"
    HOST = "host"


class OSType(str, enum.Enum):
    LINUX = "linux"
    WINDOWS = "windows"
    MACOS = "macos"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Configuration models
# ---------------------------------------------------------------------------


class DiskConfig(BaseModel):
    size_gb: int = Field(default=20, ge=1, le=2048, description="Disk size in GB")
    format: DiskFormat = DiskFormat.QCOW2
    path: str | None = None  # auto-generated if not set


class NetworkConfig(BaseModel):
    mode: NetworkMode = NetworkMode.NAT
    mac_address: str | None = None
    ports: dict[int, int] = Field(default_factory=dict, description="Host→Guest port map")


class GPUConfig(BaseModel):
    mode: GPUMode = GPUMode.NONE
    device_id: str | None = None  # PCI address for passthrough
    vram_mb: int = Field(default=256, ge=64, description="VRAM for virtual GPU")


class ResourceLimits(BaseModel):
    """Enforcement limits for sandboxed execution."""
    max_cpu_percent: float = Field(default=100.0, ge=1.0, le=100.0)
    max_memory_mb: int | None = None  # None = use VM memory
    max_disk_io_mbps: int | None = None
    max_network_mbps: int | None = None
    timeout_seconds: int = Field(default=300, ge=1, description="Hard kill after this many seconds")


class VMConfig(BaseModel):
    """Full specification for a VM."""
    name: str = Field(..., min_length=1, max_length=128)
    os_type: OSType = OSType.LINUX
    vcpus: int = Field(default=2, ge=1, le=256)
    memory_mb: int = Field(default=2048, ge=256, le=1048576)
    disk: DiskConfig = Field(default_factory=DiskConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    gpu: GPUConfig = Field(default_factory=GPUConfig)
    iso_path: str | None = None  # boot ISO
    base_image: str | None = None  # pre-built image name or path
    cloud_init: dict[str, Any] | None = None
    resource_limits: ResourceLimits = Field(default_factory=ResourceLimits)
    labels: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------


class VMInstance(BaseModel):
    """Live state of a VM."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    config: VMConfig
    status: VMStatus = VMStatus.CREATING
    pid: int | None = None
    ip_address: str | None = None
    ssh_port: int | None = None
    vnc_port: int | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    error: str | None = None
    hypervisor: str | None = None  # qemu, hyperkit, hyperv

    @property
    def uptime_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.stopped_at or datetime.now(timezone.utc)
        return (end - self.started_at).total_seconds()


# ---------------------------------------------------------------------------
# Execution models (sandbox)
# ---------------------------------------------------------------------------


class ExecRequest(BaseModel):
    """Request to execute code/commands inside a VM."""
    vm_id: str
    command: str | None = None
    code: str | None = None
    language: str = "bash"
    timeout_seconds: int = Field(default=60, ge=1, le=3600)
    stdin: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class ExecResult(BaseModel):
    """Result of an execution inside a VM."""
    vm_id: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False


# ---------------------------------------------------------------------------
# Audit / Compliance
# ---------------------------------------------------------------------------


class AuditAction(str, enum.Enum):
    VM_CREATE = "vm.create"
    VM_START = "vm.start"
    VM_STOP = "vm.stop"
    VM_DESTROY = "vm.destroy"
    VM_EXEC = "vm.exec"
    VM_SNAPSHOT = "vm.snapshot"
    AUTH_LOGIN = "auth.login"
    AUTH_LOGOUT = "auth.logout"
    CONFIG_CHANGE = "config.change"
    DATA_ACCESS = "data.access"
    DATA_EXPORT = "data.export"


class AuditEvent(BaseModel):
    """Immutable audit log entry — core to SOC 2 / HIPAA / ISO 27001."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    action: AuditAction
    actor: str  # user or service identity
    resource_id: str | None = None
    resource_type: str = "vm"
    detail: dict[str, Any] = Field(default_factory=dict)
    ip_address: str | None = None
    success: bool = True
    error_message: str | None = None
