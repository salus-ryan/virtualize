"""VM lifecycle manager — central orchestration layer.

Integrates the formal algebra to pre-validate all state transitions
before executing them against the hypervisor.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from virtualize.core.algebra import (
    VM_STATUS_TO_STATE,
    Compositor,
    CompositionConstraint,
    DEFAULT_CONSTRAINTS,
    StateType,
    SystemState,
    ToolInvocation,
    ToolName,
    TRANSITIONS,
)
from virtualize.core.hypervisor import Hypervisor, detect_hypervisor
from virtualize.core.models import (
    AuditAction,
    AuditEvent,
    ExecRequest,
    ExecResult,
    VMConfig,
    VMInstance,
    VMStatus,
)

logger = logging.getLogger(__name__)


class VMManager:
    """Manages the full lifecycle of VMs with audit logging.

    Every mutating operation is pre-validated against the algebra's
    transition rules before being executed by the hypervisor.
    """

    def __init__(
        self,
        hypervisor: Hypervisor | None = None,
        data_dir: Path | None = None,
        audit_callback: Any | None = None,
        algebraic_validation: bool = True,
        constraints: list[CompositionConstraint] | None = None,
    ) -> None:
        self._hypervisor = hypervisor or detect_hypervisor(data_dir=data_dir)
        self._vms: dict[str, VMInstance] = {}
        self._audit_callback = audit_callback  # callable(AuditEvent) -> None
        self._algebraic_validation = algebraic_validation
        self._compositor = Compositor(constraints=constraints or DEFAULT_CONSTRAINTS)
        self._system_state = SystemState()

    # -- algebra helpers --

    @property
    def system_state(self) -> SystemState:
        """Current algebraic state of the system."""
        return self._system_state

    def _pre_validate(self, tool: ToolName, vm_id: str | None = None,
                      args: dict[str, Any] | None = None) -> None:
        """Validate a single transition against the algebra before executing."""
        if not self._algebraic_validation:
            return
        inv = ToolInvocation(tool=tool, vm_id=vm_id, args=args or {})
        result = self._compositor.validate([inv], self._system_state)
        if not result.valid:
            errors = "; ".join(e.message for e in result.errors)
            raise ValueError(f"Algebraic violation: {errors}")

    def _evolve_state(self, tool: ToolName, vm_id: str | None = None) -> None:
        """Evolve the system state after a successful operation."""
        rule = TRANSITIONS.get(tool)
        if rule is None:
            return
        if rule.produced_vm_state is not None and vm_id:
            self._system_state = self._system_state.with_vm(vm_id, rule.produced_vm_state)
        if rule.produced_sandbox is not None:
            self._system_state = self._system_state.with_sandbox(rule.produced_sandbox)
        if not rule.is_read_only:
            import hashlib
            seq = self._system_state.audit_sequence + 1
            payload = f"{self._system_state.audit_hash}:{tool.value}:{seq}"
            new_hash = hashlib.sha256(payload.encode()).hexdigest()[:16]
            self._system_state = self._system_state.with_audit(
                StateType.AUDIT_DIRTY, seq, new_hash)

    def _sync_vm_state(self, vm_id: str, status: VMStatus) -> None:
        """Sync runtime VMStatus into the algebraic state."""
        state_type = VM_STATUS_TO_STATE.get(status, StateType.VM_STOPPED)
        self._system_state = self._system_state.with_vm(vm_id, state_type)

    # -- audit helper --

    def _audit(self, action: AuditAction, actor: str, resource_id: str | None = None,
               detail: dict[str, Any] | None = None, success: bool = True, error_message: str | None = None) -> None:
        event = AuditEvent(
            action=action,
            actor=actor,
            resource_id=resource_id,
            detail=detail or {},
            success=success,
            error_message=error_message,
        )
        logger.info("AUDIT: %s %s %s success=%s", action, actor, resource_id, success)
        if self._audit_callback:
            self._audit_callback(event)

    # -- lifecycle --

    @property
    def vms(self) -> dict[str, VMInstance]:
        return dict(self._vms)

    def get_vm(self, vm_id: str) -> VMInstance:
        if vm_id not in self._vms:
            raise KeyError(f"VM {vm_id} not found")
        return self._vms[vm_id]

    async def create(self, config: VMConfig, actor: str = "system") -> VMInstance:
        instance = VMInstance(config=config)
        self._pre_validate(ToolName.VM_CREATE, vm_id=instance.id, args={"name": config.name})
        self._vms[instance.id] = instance
        self._evolve_state(ToolName.VM_CREATE, instance.id)
        self._audit(AuditAction.VM_CREATE, actor, instance.id, detail=config.model_dump())
        logger.info("Created VM %s (%s)", instance.id, config.name)
        return instance

    async def start(self, vm_id: str, actor: str = "system") -> VMInstance:
        instance = self.get_vm(vm_id)
        self._pre_validate(ToolName.VM_START, vm_id=vm_id)
        try:
            instance = await self._hypervisor.start(instance)
            self._vms[vm_id] = instance
            self._evolve_state(ToolName.VM_START, vm_id)
            self._audit(AuditAction.VM_START, actor, vm_id)
            return instance
        except Exception as e:
            self._audit(AuditAction.VM_START, actor, vm_id, success=False, error_message=str(e))
            raise

    async def stop(self, vm_id: str, force: bool = False, actor: str = "system") -> VMInstance:
        instance = self.get_vm(vm_id)
        self._pre_validate(ToolName.VM_STOP, vm_id=vm_id)
        try:
            instance = await self._hypervisor.stop(instance, force=force)
            self._vms[vm_id] = instance
            self._evolve_state(ToolName.VM_STOP, vm_id)
            self._audit(AuditAction.VM_STOP, actor, vm_id, detail={"force": force})
            return instance
        except Exception as e:
            self._audit(AuditAction.VM_STOP, actor, vm_id, success=False, error_message=str(e))
            raise

    async def destroy(self, vm_id: str, actor: str = "system") -> VMInstance:
        instance = self.get_vm(vm_id)
        self._pre_validate(ToolName.VM_DESTROY, vm_id=vm_id)
        try:
            instance = await self._hypervisor.destroy(instance)
            self._evolve_state(ToolName.VM_DESTROY, vm_id)
            self._audit(AuditAction.VM_DESTROY, actor, vm_id)
            del self._vms[vm_id]
            return instance
        except Exception as e:
            self._audit(AuditAction.VM_DESTROY, actor, vm_id, success=False, error_message=str(e))
            raise

    async def status(self, vm_id: str) -> VMStatus:
        instance = self.get_vm(vm_id)
        live_status = await self._hypervisor.status(instance)
        instance.status = live_status
        return live_status

    async def list_vms(self) -> list[VMInstance]:
        return list(self._vms.values())

    # -- execution --

    async def exec(self, request: ExecRequest, actor: str = "system") -> ExecResult:
        instance = self.get_vm(request.vm_id)

        command = request.command
        if request.code and not command:
            # Wrap code execution based on language
            lang_runners: dict[str, str] = {
                "python": "python3 -c",
                "bash": "bash -c",
                "sh": "sh -c",
                "node": "node -e",
                "javascript": "node -e",
                "ruby": "ruby -e",
                "perl": "perl -e",
            }
            runner = lang_runners.get(request.language, "bash -c")
            # Escape single quotes in code
            escaped = request.code.replace("'", "'\\''")
            command = f"{runner} '{escaped}'"

        if not command:
            raise ValueError("Either command or code must be provided")

        # Prepend env vars
        if request.env:
            env_prefix = " ".join(f"{k}={v}" for k, v in request.env.items())
            command = f"{env_prefix} {command}"

        import time
        start_time = time.monotonic()

        try:
            exit_code, stdout, stderr = await self._hypervisor.exec_command(
                instance, command, timeout=request.timeout_seconds
            )
            duration = time.monotonic() - start_time
            timed_out = False
        except asyncio.TimeoutError:
            duration = time.monotonic() - start_time
            exit_code, stdout, stderr = -1, "", "Execution timed out"
            timed_out = True

        result = ExecResult(
            vm_id=request.vm_id,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=round(duration, 3),
            timed_out=timed_out,
        )

        self._audit(AuditAction.VM_EXEC, actor, request.vm_id, detail={
            "command": command[:500],
            "exit_code": exit_code,
            "timed_out": timed_out,
            "duration": result.duration_seconds,
        })

        return result
