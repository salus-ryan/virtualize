"""MCP Server for Virtualize.

Exposes VM management, sandboxed execution, and filesystem operations
as MCP tools that AI agents can call directly.

Implements the Model Context Protocol (MCP) specification.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from virtualize.compliance.audit import AuditLog
from virtualize.core.manager import VMManager
from virtualize.core.models import (
    DiskConfig,
    ExecRequest,
    GPUConfig,
    GPUMode,
    NetworkConfig,
    NetworkMode,
    ResourceLimits,
    VMConfig,
    VMStatus,
)
from virtualize.sandbox.executor import SandboxExecutor

logger = logging.getLogger(__name__)


def create_mcp_server(
    manager: VMManager | None = None,
    audit_log: AuditLog | None = None,
) -> Server:
    """Create and configure the Virtualize MCP server."""

    if audit_log is None:
        audit_log = AuditLog()

    if manager is None:
        manager = VMManager(audit_callback=audit_log.record)

    executor = SandboxExecutor(manager)
    server = Server("virtualize")

    # ------------------------------------------------------------------
    # Tool definitions
    # ------------------------------------------------------------------

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="vm_create",
                description="Create a new virtual machine with specified resources",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "VM name"},
                        "vcpus": {"type": "integer", "default": 2, "description": "Number of virtual CPUs"},
                        "memory_mb": {"type": "integer", "default": 2048, "description": "Memory in MB"},
                        "disk_size_gb": {"type": "integer", "default": 20, "description": "Disk size in GB"},
                        "os_type": {"type": "string", "default": "linux", "enum": ["linux", "windows", "macos", "other"]},
                        "gpu": {"type": "string", "default": "none", "enum": ["none", "passthrough", "virtual"]},
                        "network": {"type": "string", "default": "nat", "enum": ["nat", "bridge", "isolated", "host"]},
                        "base_image": {"type": "string", "description": "Base image name or path"},
                        "iso_path": {"type": "string", "description": "ISO file path for installation"},
                    },
                    "required": ["name"],
                },
            ),
            Tool(
                name="vm_start",
                description="Start a stopped virtual machine",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "vm_id": {"type": "string", "description": "VM identifier"},
                    },
                    "required": ["vm_id"],
                },
            ),
            Tool(
                name="vm_stop",
                description="Stop a running virtual machine",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "vm_id": {"type": "string", "description": "VM identifier"},
                        "force": {"type": "boolean", "default": False, "description": "Force stop (kill)"},
                    },
                    "required": ["vm_id"],
                },
            ),
            Tool(
                name="vm_destroy",
                description="Destroy a VM and all its data",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "vm_id": {"type": "string", "description": "VM identifier"},
                    },
                    "required": ["vm_id"],
                },
            ),
            Tool(
                name="vm_list",
                description="List all virtual machines and their status",
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            Tool(
                name="vm_status",
                description="Get detailed status of a specific VM",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "vm_id": {"type": "string", "description": "VM identifier"},
                    },
                    "required": ["vm_id"],
                },
            ),
            Tool(
                name="vm_exec",
                description="Execute a shell command inside a running VM",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "vm_id": {"type": "string", "description": "VM identifier"},
                        "command": {"type": "string", "description": "Shell command to execute"},
                        "timeout": {"type": "integer", "default": 60, "description": "Timeout in seconds"},
                    },
                    "required": ["vm_id", "command"],
                },
            ),
            Tool(
                name="sandbox_run",
                description="Execute code in an isolated sandbox VM. Creates a temporary VM, runs the code, and destroys it.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "Code to execute"},
                        "language": {
                            "type": "string",
                            "default": "python",
                            "enum": ["python", "bash", "node", "javascript", "ruby", "perl", "sh"],
                            "description": "Programming language",
                        },
                        "timeout": {"type": "integer", "default": 60, "description": "Timeout in seconds"},
                        "env": {"type": "object", "description": "Environment variables"},
                    },
                    "required": ["code"],
                },
            ),
            Tool(
                name="vm_file_read",
                description="Read a file from inside a VM",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "vm_id": {"type": "string", "description": "VM identifier"},
                        "path": {"type": "string", "description": "File path inside the VM"},
                    },
                    "required": ["vm_id", "path"],
                },
            ),
            Tool(
                name="vm_file_write",
                description="Write content to a file inside a VM",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "vm_id": {"type": "string", "description": "VM identifier"},
                        "path": {"type": "string", "description": "File path inside the VM"},
                        "content": {"type": "string", "description": "File content to write"},
                    },
                    "required": ["vm_id", "path", "content"],
                },
            ),
            Tool(
                name="compliance_report",
                description="Generate a compliance posture report for a given framework",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "framework": {
                            "type": "string",
                            "enum": ["soc1", "soc2", "soc3", "hipaa", "iso27001"],
                            "description": "Compliance framework",
                        },
                    },
                    "required": ["framework"],
                },
            ),
            Tool(
                name="audit_query",
                description="Query the audit log for events",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "Filter by action type"},
                        "actor": {"type": "string", "description": "Filter by actor"},
                        "resource_id": {"type": "string", "description": "Filter by resource ID"},
                        "limit": {"type": "integer", "default": 50, "description": "Max results"},
                    },
                },
            ),
            Tool(
                name="audit_verify",
                description="Verify the integrity of the audit log chain",
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
        ]

    # ------------------------------------------------------------------
    # Tool handlers
    # ------------------------------------------------------------------

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        try:
            result = await _handle_tool(name, arguments)
            return [TextContent(type="text", text=json.dumps(result, default=str, indent=2))]
        except Exception as e:
            logger.exception("Tool %s failed", name)
            return [TextContent(type="text", text=json.dumps({"error": str(e)}, indent=2))]

    async def _handle_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
        actor = "mcp-agent"

        if name == "vm_create":
            config = VMConfig(
                name=args["name"],
                vcpus=args.get("vcpus", 2),
                memory_mb=args.get("memory_mb", 2048),
                disk=DiskConfig(size_gb=args.get("disk_size_gb", 20)),
                os_type=args.get("os_type", "linux"),
                gpu=GPUConfig(mode=GPUMode(args.get("gpu", "none"))),
                network=NetworkConfig(mode=NetworkMode(args.get("network", "nat"))),
                base_image=args.get("base_image"),
                iso_path=args.get("iso_path"),
            )
            vm = await manager.create(config, actor=actor)
            return {"vm_id": vm.id, "status": vm.status.value, "name": vm.config.name}

        elif name == "vm_start":
            vm = await manager.start(args["vm_id"], actor=actor)
            return {
                "vm_id": vm.id,
                "status": vm.status.value,
                "ssh_port": vm.ssh_port,
                "ip_address": vm.ip_address,
            }

        elif name == "vm_stop":
            vm = await manager.stop(args["vm_id"], force=args.get("force", False), actor=actor)
            return {"vm_id": vm.id, "status": vm.status.value}

        elif name == "vm_destroy":
            vm = await manager.destroy(args["vm_id"], actor=actor)
            return {"vm_id": vm.id, "status": vm.status.value}

        elif name == "vm_list":
            vms = await manager.list_vms()
            return {
                "count": len(vms),
                "vms": [
                    {
                        "id": vm.id,
                        "name": vm.config.name,
                        "status": vm.status.value,
                        "vcpus": vm.config.vcpus,
                        "memory_mb": vm.config.memory_mb,
                        "ip_address": vm.ip_address,
                        "ssh_port": vm.ssh_port,
                    }
                    for vm in vms
                ],
            }

        elif name == "vm_status":
            vm = manager.get_vm(args["vm_id"])
            live_status = await manager.status(args["vm_id"])
            return {
                "id": vm.id,
                "name": vm.config.name,
                "status": live_status.value,
                "vcpus": vm.config.vcpus,
                "memory_mb": vm.config.memory_mb,
                "disk_gb": vm.config.disk.size_gb,
                "gpu": vm.config.gpu.mode.value,
                "network": vm.config.network.mode.value,
                "ip_address": vm.ip_address,
                "ssh_port": vm.ssh_port,
                "uptime_seconds": vm.uptime_seconds,
                "created_at": str(vm.created_at),
            }

        elif name == "vm_exec":
            request = ExecRequest(
                vm_id=args["vm_id"],
                command=args["command"],
                timeout_seconds=args.get("timeout", 60),
            )
            result = await manager.exec(request, actor=actor)
            return {
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "duration_seconds": result.duration_seconds,
                "timed_out": result.timed_out,
            }

        elif name == "sandbox_run":
            result = await executor.run(
                code=args["code"],
                language=args.get("language", "python"),
                timeout=args.get("timeout", 60),
                env=args.get("env"),
                actor=actor,
            )
            return {
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "duration_seconds": result.duration_seconds,
                "timed_out": result.timed_out,
            }

        elif name == "vm_file_read":
            request = ExecRequest(
                vm_id=args["vm_id"],
                command=f"cat {args['path']}",
                timeout_seconds=10,
            )
            result = await manager.exec(request, actor=actor)
            return {"path": args["path"], "content": result.stdout, "error": result.stderr if result.exit_code != 0 else None}

        elif name == "vm_file_write":
            # Escape content for shell
            content = args["content"].replace("'", "'\\''")
            request = ExecRequest(
                vm_id=args["vm_id"],
                command=f"cat > {args['path']} << 'VIRTUALIZE_EOF'\n{args['content']}\nVIRTUALIZE_EOF",
                timeout_seconds=10,
            )
            result = await manager.exec(request, actor=actor)
            return {"path": args["path"], "success": result.exit_code == 0, "error": result.stderr if result.exit_code != 0 else None}

        elif name == "compliance_report":
            from virtualize.compliance.policies import ComplianceFramework, generate_report
            framework = ComplianceFramework(args["framework"])
            report = generate_report(framework)
            return report.model_dump()

        elif name == "audit_query":
            events = audit_log.query(
                action=args.get("action"),
                actor=args.get("actor"),
                resource_id=args.get("resource_id"),
                limit=args.get("limit", 50),
            )
            return {"count": len(events), "events": events}

        elif name == "audit_verify":
            valid, count, message = audit_log.verify_integrity()
            return {"valid": valid, "entries_checked": count, "message": message}

        else:
            raise ValueError(f"Unknown tool: {name}")

    return server


async def run_mcp_server() -> None:
    """Run the MCP server over stdio."""
    audit_log = AuditLog()
    manager = VMManager(audit_callback=audit_log.record)
    server = create_mcp_server(manager=manager, audit_log=audit_log)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
