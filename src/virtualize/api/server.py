"""REST API server for Virtualize.

Provides HTTP endpoints for VM management, sandboxed execution,
and compliance operations. Designed for integration with web dashboards,
CI/CD pipelines, and programmatic access.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from virtualize.compliance.audit import AuditLog
from virtualize.core.manager import VMManager
from virtualize.core.models import (
    DiskConfig,
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
from virtualize.sandbox.executor import SandboxExecutor

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------

_manager: VMManager | None = None
_audit_log: AuditLog | None = None
_executor: SandboxExecutor | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _manager, _audit_log, _executor
    _audit_log = AuditLog()
    _manager = VMManager(audit_callback=_audit_log.record)
    _executor = SandboxExecutor(_manager)
    logger.info("Virtualize API server started")
    yield
    logger.info("Virtualize API server shutting down")


app = FastAPI(
    title="Virtualize",
    description="Free, cross-platform VM orchestration for AI workflows",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request/Response schemas
# ---------------------------------------------------------------------------


class CreateVMRequest(BaseModel):
    name: str
    vcpus: int = 2
    memory_mb: int = 2048
    disk_size_gb: int = 20
    os_type: str = "linux"
    gpu: str = "none"
    network: str = "nat"
    base_image: str | None = None
    iso_path: str | None = None
    ports: dict[int, int] = Field(default_factory=dict)
    cloud_init: dict[str, Any] | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class VMResponse(BaseModel):
    id: str
    name: str
    status: str
    vcpus: int
    memory_mb: int
    disk_gb: int
    gpu: str
    network: str
    ip_address: str | None
    ssh_port: int | None
    created_at: str
    uptime_seconds: float | None


class ExecCommandRequest(BaseModel):
    command: str
    timeout: int = 60


class SandboxRunRequest(BaseModel):
    code: str
    language: str = "python"
    timeout: int = 60
    env: dict[str, str] = Field(default_factory=dict)


class ExecResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool


class FileWriteRequest(BaseModel):
    path: str
    content: str


def _vm_to_response(vm: VMInstance) -> VMResponse:
    return VMResponse(
        id=vm.id,
        name=vm.config.name,
        status=vm.status.value,
        vcpus=vm.config.vcpus,
        memory_mb=vm.config.memory_mb,
        disk_gb=vm.config.disk.size_gb,
        gpu=vm.config.gpu.mode.value,
        network=vm.config.network.mode.value,
        ip_address=vm.ip_address,
        ssh_port=vm.ssh_port,
        created_at=str(vm.created_at),
        uptime_seconds=vm.uptime_seconds,
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    from virtualize.api.dashboard import get_dashboard_html
    return get_dashboard_html()


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


# ---------------------------------------------------------------------------
# VM CRUD
# ---------------------------------------------------------------------------


@app.post("/api/v1/vms", response_model=VMResponse, status_code=201)
async def create_vm(req: CreateVMRequest):
    assert _manager
    config = VMConfig(
        name=req.name,
        vcpus=req.vcpus,
        memory_mb=req.memory_mb,
        disk=DiskConfig(size_gb=req.disk_size_gb),
        os_type=req.os_type,
        gpu=GPUConfig(mode=GPUMode(req.gpu)),
        network=NetworkConfig(mode=NetworkMode(req.network), ports=req.ports),
        base_image=req.base_image,
        iso_path=req.iso_path,
        cloud_init=req.cloud_init,
        labels=req.labels,
    )
    vm = await _manager.create(config, actor="api")
    return _vm_to_response(vm)


@app.get("/api/v1/vms", response_model=list[VMResponse])
async def list_vms():
    assert _manager
    vms = await _manager.list_vms()
    return [_vm_to_response(vm) for vm in vms]


@app.get("/api/v1/vms/{vm_id}", response_model=VMResponse)
async def get_vm(vm_id: str):
    assert _manager
    try:
        vm = _manager.get_vm(vm_id)
        return _vm_to_response(vm)
    except KeyError:
        raise HTTPException(404, f"VM {vm_id} not found")


@app.post("/api/v1/vms/{vm_id}/start", response_model=VMResponse)
async def start_vm(vm_id: str):
    assert _manager
    try:
        vm = await _manager.start(vm_id, actor="api")
        return _vm_to_response(vm)
    except KeyError:
        raise HTTPException(404, f"VM {vm_id} not found")
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@app.post("/api/v1/vms/{vm_id}/stop", response_model=VMResponse)
async def stop_vm(vm_id: str, force: bool = Query(False)):
    assert _manager
    try:
        vm = await _manager.stop(vm_id, force=force, actor="api")
        return _vm_to_response(vm)
    except KeyError:
        raise HTTPException(404, f"VM {vm_id} not found")


@app.delete("/api/v1/vms/{vm_id}", response_model=VMResponse)
async def destroy_vm(vm_id: str):
    assert _manager
    try:
        vm = await _manager.destroy(vm_id, actor="api")
        return _vm_to_response(vm)
    except KeyError:
        raise HTTPException(404, f"VM {vm_id} not found")


@app.get("/api/v1/vms/{vm_id}/status")
async def vm_status(vm_id: str):
    assert _manager
    try:
        status = await _manager.status(vm_id)
        return {"vm_id": vm_id, "status": status.value}
    except KeyError:
        raise HTTPException(404, f"VM {vm_id} not found")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@app.post("/api/v1/vms/{vm_id}/exec", response_model=ExecResponse)
async def exec_in_vm(vm_id: str, req: ExecCommandRequest):
    assert _manager
    try:
        request = ExecRequest(vm_id=vm_id, command=req.command, timeout_seconds=req.timeout)
        result = await _manager.exec(request, actor="api")
        return ExecResponse(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=result.duration_seconds,
            timed_out=result.timed_out,
        )
    except KeyError:
        raise HTTPException(404, f"VM {vm_id} not found")
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@app.post("/api/v1/sandbox/run", response_model=ExecResponse)
async def sandbox_run(req: SandboxRunRequest):
    assert _executor
    result = await _executor.run(
        code=req.code,
        language=req.language,
        timeout=req.timeout,
        env=req.env,
        actor="api",
    )
    return ExecResponse(
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_seconds=result.duration_seconds,
        timed_out=result.timed_out,
    )


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------


@app.get("/api/v1/vms/{vm_id}/files")
async def read_file(vm_id: str, path: str = Query(...)):
    assert _manager
    try:
        request = ExecRequest(vm_id=vm_id, command=f"cat {path}", timeout_seconds=10)
        result = await _manager.exec(request, actor="api")
        if result.exit_code != 0:
            raise HTTPException(400, result.stderr)
        return {"path": path, "content": result.stdout}
    except KeyError:
        raise HTTPException(404, f"VM {vm_id} not found")


@app.post("/api/v1/vms/{vm_id}/files")
async def write_file(vm_id: str, req: FileWriteRequest):
    assert _manager
    try:
        request = ExecRequest(
            vm_id=vm_id,
            command=f"cat > {req.path} << 'VIRTUALIZE_EOF'\n{req.content}\nVIRTUALIZE_EOF",
            timeout_seconds=10,
        )
        result = await _manager.exec(request, actor="api")
        if result.exit_code != 0:
            raise HTTPException(500, result.stderr)
        return {"path": req.path, "success": True}
    except KeyError:
        raise HTTPException(404, f"VM {vm_id} not found")


# ---------------------------------------------------------------------------
# Compliance
# ---------------------------------------------------------------------------


@app.get("/api/v1/compliance/report/{framework}")
async def compliance_report(framework: str):
    from virtualize.compliance.policies import ComplianceFramework, generate_report
    try:
        fw = ComplianceFramework(framework)
    except ValueError:
        raise HTTPException(400, f"Unknown framework: {framework}. Use: soc1, soc2, soc3, hipaa, iso27001")
    report = generate_report(fw)
    return report.model_dump()


@app.get("/api/v1/compliance/controls")
async def compliance_controls(framework: str | None = Query(None)):
    from virtualize.compliance.policies import ComplianceFramework, get_controls
    fw = ComplianceFramework(framework) if framework else None
    controls = get_controls(fw)
    return [c.model_dump() for c in controls]


@app.get("/api/v1/audit/events")
async def audit_events(
    action: str | None = Query(None),
    actor: str | None = Query(None),
    resource_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=1000),
):
    assert _audit_log
    events = _audit_log.query(action=action, actor=actor, resource_id=resource_id, limit=limit)
    return {"count": len(events), "events": events}


@app.get("/api/v1/audit/verify")
async def audit_verify():
    assert _audit_log
    valid, count, message = _audit_log.verify_integrity()
    return {"valid": valid, "entries_checked": count, "message": message}


# ---------------------------------------------------------------------------
# System info
# ---------------------------------------------------------------------------


@app.get("/api/v1/system/info")
async def system_info():
    import platform
    import shutil

    import psutil

    return {
        "platform": platform.system(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": psutil.cpu_count(),
        "memory_total_mb": psutil.virtual_memory().total // (1024 * 1024),
        "memory_available_mb": psutil.virtual_memory().available // (1024 * 1024),
        "disk_total_gb": psutil.disk_usage("/").total // (1024 ** 3),
        "disk_free_gb": psutil.disk_usage("/").free // (1024 ** 3),
        "qemu_available": shutil.which("qemu-system-x86_64") is not None,
        "kvm_available": Path("/dev/kvm").exists() if platform.system() == "Linux" else False,
    }


def run_server(host: str = "0.0.0.0", port: int = 8420) -> None:
    """Run the API server."""
    import uvicorn
    uvicorn.run(app, host=host, port=port)


# Need Path import for system_info
from pathlib import Path
