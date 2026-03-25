"""Mock hypervisor for testing without QEMU installed."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from virtualize.core.hypervisor import Hypervisor, ensure_dirs
from virtualize.core.models import VMInstance, VMStatus


class MockHypervisor(Hypervisor):
    """In-memory mock hypervisor for testing and development."""

    name = "mock"

    def __init__(self, data_dir: Path | None = None) -> None:
        self._running: set[str] = set()

    def is_available(self) -> bool:
        return True

    async def create_disk(self, instance: VMInstance) -> Path:
        return Path(f"/tmp/virtualize-mock/{instance.id}/disk.qcow2")

    async def start(self, instance: VMInstance) -> VMInstance:
        self._running.add(instance.id)
        instance.status = VMStatus.RUNNING
        instance.started_at = datetime.now(timezone.utc)
        instance.pid = 99999
        instance.ssh_port = 22222
        instance.ip_address = "127.0.0.1"
        instance.hypervisor = self.name
        return instance

    async def stop(self, instance: VMInstance, force: bool = False) -> VMInstance:
        self._running.discard(instance.id)
        instance.status = VMStatus.STOPPED
        instance.stopped_at = datetime.now(timezone.utc)
        instance.pid = None
        return instance

    async def destroy(self, instance: VMInstance) -> VMInstance:
        self._running.discard(instance.id)
        instance.status = VMStatus.DESTROYED
        instance.pid = None
        return instance

    async def status(self, instance: VMInstance) -> VMStatus:
        if instance.id in self._running:
            return VMStatus.RUNNING
        return VMStatus.STOPPED

    async def exec_command(self, instance: VMInstance, command: str, timeout: int = 60) -> tuple[int, str, str]:
        if instance.id not in self._running:
            raise RuntimeError(f"VM {instance.id} is not running")
        # Simulate command execution
        return 0, f"mock output for: {command}\n", ""
