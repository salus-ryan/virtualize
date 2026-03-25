"""Sandboxed code execution engine.

Runs code inside VMs with strict resource limits, timeouts, and isolation.
Provides a high-level API that agents and the MCP server use.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from virtualize.core.manager import VMManager
from virtualize.core.models import (
    ExecRequest,
    ExecResult,
    NetworkMode,
    ResourceLimits,
    VMConfig,
    VMInstance,
    VMStatus,
)

logger = logging.getLogger(__name__)


class SandboxPool:
    """Pool of pre-warmed VMs for fast sandbox execution."""

    def __init__(
        self,
        manager: VMManager,
        pool_size: int = 2,
        base_image: str | None = None,
        default_limits: ResourceLimits | None = None,
    ) -> None:
        self._manager = manager
        self._pool_size = pool_size
        self._base_image = base_image
        self._default_limits = default_limits or ResourceLimits(timeout_seconds=60)
        self._available: asyncio.Queue[str] = asyncio.Queue()
        self._in_use: set[str] = set()
        self._initialized = False

    async def initialize(self) -> None:
        """Pre-warm the sandbox pool."""
        if self._initialized:
            return
        logger.info("Initializing sandbox pool with %d VMs", self._pool_size)
        for i in range(self._pool_size):
            try:
                vm = await self._create_sandbox_vm(f"sandbox-{i}")
                await self._manager.start(vm.id)
                await self._available.put(vm.id)
            except Exception as e:
                logger.error("Failed to create sandbox VM %d: %s", i, e)
        self._initialized = True

    async def _create_sandbox_vm(self, name: str) -> VMInstance:
        config = VMConfig(
            name=name,
            vcpus=1,
            memory_mb=1024,
            base_image=self._base_image,
            network={"mode": NetworkMode.ISOLATED},
            resource_limits=self._default_limits,
            labels={"role": "sandbox", "pool": "true"},
        )
        return await self._manager.create(config)

    async def acquire(self, timeout: float = 30.0) -> str:
        """Get a sandbox VM from the pool. Blocks if none available."""
        try:
            vm_id = await asyncio.wait_for(self._available.get(), timeout=timeout)
            self._in_use.add(vm_id)
            return vm_id
        except asyncio.TimeoutError:
            # Create a new one on-demand
            logger.warning("Pool exhausted, creating on-demand sandbox")
            vm = await self._create_sandbox_vm(f"sandbox-ondemand-{len(self._in_use)}")
            await self._manager.start(vm.id)
            self._in_use.add(vm.id)
            return vm.id

    async def release(self, vm_id: str, reset: bool = True) -> None:
        """Return a sandbox VM to the pool."""
        self._in_use.discard(vm_id)
        if reset:
            # Reset the VM to clean state
            try:
                await self._manager.stop(vm_id, force=True)
                await self._manager.start(vm_id)
            except Exception as e:
                logger.error("Failed to reset sandbox VM %s: %s", vm_id, e)
                # Destroy and replace
                try:
                    await self._manager.destroy(vm_id)
                    vm = await self._create_sandbox_vm(f"sandbox-replacement")
                    await self._manager.start(vm.id)
                    vm_id = vm.id
                except Exception:
                    return
        await self._available.put(vm_id)

    async def shutdown(self) -> None:
        """Destroy all pool VMs."""
        all_ids = list(self._in_use)
        while not self._available.empty():
            try:
                all_ids.append(self._available.get_nowait())
            except asyncio.QueueEmpty:
                break
        for vm_id in all_ids:
            try:
                await self._manager.destroy(vm_id)
            except Exception:
                pass


class SandboxExecutor:
    """High-level sandboxed code execution.

    Usage:
        executor = SandboxExecutor(manager)
        result = await executor.run("print('hello')", language="python")
    """

    def __init__(self, manager: VMManager, pool: SandboxPool | None = None) -> None:
        self._manager = manager
        self._pool = pool

    async def run(
        self,
        code: str,
        language: str = "python",
        timeout: int = 60,
        env: dict[str, str] | None = None,
        vm_id: str | None = None,
        actor: str = "sandbox",
    ) -> ExecResult:
        """Execute code in a sandboxed VM.

        If vm_id is provided, runs in that specific VM.
        Otherwise, acquires a VM from the pool (or creates one on-the-fly).
        """
        owned_vm = False

        if vm_id is None:
            if self._pool:
                vm_id = await self._pool.acquire()
            else:
                # Create a one-shot VM
                config = VMConfig(
                    name="sandbox-oneshot",
                    vcpus=1,
                    memory_mb=1024,
                    network={"mode": NetworkMode.ISOLATED},
                    resource_limits=ResourceLimits(timeout_seconds=timeout),
                    labels={"role": "sandbox", "oneshot": "true"},
                )
                vm = await self._manager.create(config, actor=actor)
                await self._manager.start(vm.id, actor=actor)
                vm_id = vm.id
                owned_vm = True

        try:
            request = ExecRequest(
                vm_id=vm_id,
                code=code,
                language=language,
                timeout_seconds=timeout,
                env=env or {},
            )
            result = await self._manager.exec(request, actor=actor)
            return result
        finally:
            if self._pool and not owned_vm:
                await self._pool.release(vm_id)
            elif owned_vm:
                try:
                    await self._manager.destroy(vm_id, actor=actor)
                except Exception:
                    pass

    async def run_command(
        self,
        command: str,
        timeout: int = 60,
        vm_id: str | None = None,
        actor: str = "sandbox",
    ) -> ExecResult:
        """Execute a shell command in a sandboxed VM."""
        owned_vm = False

        if vm_id is None:
            if self._pool:
                vm_id = await self._pool.acquire()
            else:
                config = VMConfig(
                    name="sandbox-cmd-oneshot",
                    vcpus=1,
                    memory_mb=1024,
                    network={"mode": NetworkMode.ISOLATED},
                    resource_limits=ResourceLimits(timeout_seconds=timeout),
                    labels={"role": "sandbox", "oneshot": "true"},
                )
                vm = await self._manager.create(config, actor=actor)
                await self._manager.start(vm.id, actor=actor)
                vm_id = vm.id
                owned_vm = True

        try:
            request = ExecRequest(
                vm_id=vm_id,
                command=command,
                timeout_seconds=timeout,
            )
            return await self._manager.exec(request, actor=actor)
        finally:
            if self._pool and not owned_vm:
                await self._pool.release(vm_id)
            elif owned_vm:
                try:
                    await self._manager.destroy(vm_id, actor=actor)
                except Exception:
                    pass
