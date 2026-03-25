"""Tests for the formal tool algebra.

Verifies:
    - Carrier set and state types
    - Typed transition rules (preconditions/postconditions)
    - Composition engine (valid/invalid chains)
    - Algebraic axioms (identity, closure, associativity, audit monotonicity/irreversibility)
    - Constraint subalgebra (compliance policies)
    - Algebraic rewriting / optimization
    - Integration with VMManager
"""

from __future__ import annotations

import pytest

from virtualize.core.algebra import (
    IDENTITY_STATE,
    TRANSITIONS,
    AxiomVerifier,
    CompositionConstraint,
    CompositionError,
    Compositor,
    DEFAULT_CONSTRAINTS,
    StateType,
    SystemState,
    ToolInvocation,
    ToolName,
    TransitionRule,
    rewrite_plan,
    validate_plan,
    verify_axioms,
)


# ═══════════════════════════════════════════════════════════════════════════
# §1  Carrier Set
# ═══════════════════════════════════════════════════════════════════════════


class TestCarrierSet:
    def test_identity_state(self):
        s = IDENTITY_STATE
        assert s.vm_states == {}
        assert s.sandbox_state == StateType.SANDBOX_IDLE
        assert s.audit_state == StateType.AUDIT_CLEAN
        assert s.audit_sequence == 0
        assert s.audit_hash == "genesis"

    def test_state_immutability(self):
        s = IDENTITY_STATE
        s2 = s.with_vm("vm-1", StateType.VM_CREATED)
        assert "vm-1" not in s.vm_states  # original unchanged
        assert s2.get_vm_state("vm-1") == StateType.VM_CREATED

    def test_nonexistent_vm_default(self):
        assert IDENTITY_STATE.get_vm_state("no-such-vm") == StateType.VM_NONEXISTENT

    def test_fs_state_derived(self):
        s = IDENTITY_STATE.with_vm("vm-1", StateType.VM_RUNNING)
        assert s.fs_state("vm-1") == StateType.FS_READABLE

        s2 = s.with_vm("vm-1", StateType.VM_STOPPED)
        assert s2.fs_state("vm-1") == StateType.FS_INACCESSIBLE

    def test_with_sandbox(self):
        s = IDENTITY_STATE.with_sandbox(StateType.SANDBOX_EXECUTING)
        assert s.sandbox_state == StateType.SANDBOX_EXECUTING

    def test_with_audit(self):
        s = IDENTITY_STATE.with_audit(StateType.AUDIT_DIRTY, 5, "abc123")
        assert s.audit_state == StateType.AUDIT_DIRTY
        assert s.audit_sequence == 5
        assert s.audit_hash == "abc123"


# ═══════════════════════════════════════════════════════════════════════════
# §2  Transition Rules
# ═══════════════════════════════════════════════════════════════════════════


class TestTransitionRules:
    def test_all_tools_have_rules(self):
        for tool in ToolName:
            assert tool in TRANSITIONS, f"Missing rule for {tool}"

    def test_identity_rule(self):
        rule = TRANSITIONS[ToolName.IDENTITY]
        assert rule.is_identity is True
        assert rule.is_read_only is True

    def test_create_precondition(self):
        rule = TRANSITIONS[ToolName.VM_CREATE]
        assert StateType.VM_NONEXISTENT in rule.required_vm_states
        assert rule.produced_vm_state == StateType.VM_CREATED

    def test_start_precondition(self):
        rule = TRANSITIONS[ToolName.VM_START]
        assert StateType.VM_CREATED in rule.required_vm_states
        assert StateType.VM_STOPPED in rule.required_vm_states
        assert rule.produced_vm_state == StateType.VM_RUNNING

    def test_exec_requires_running(self):
        rule = TRANSITIONS[ToolName.VM_EXEC]
        assert rule.required_vm_states == frozenset({StateType.VM_RUNNING})
        assert rule.produced_vm_state is None  # no VM state change

    def test_file_read_is_read_only(self):
        rule = TRANSITIONS[ToolName.VM_FILE_READ]
        assert rule.is_read_only is True

    def test_file_write_requires_running(self):
        rule = TRANSITIONS[ToolName.VM_FILE_WRITE]
        assert StateType.VM_RUNNING in rule.required_vm_states

    def test_destroy_accepts_multiple_sources(self):
        rule = TRANSITIONS[ToolName.VM_DESTROY]
        assert StateType.VM_CREATED in rule.required_vm_states
        assert StateType.VM_RUNNING in rule.required_vm_states
        assert StateType.VM_STOPPED in rule.required_vm_states

    def test_audit_tools_are_read_only(self):
        assert TRANSITIONS[ToolName.AUDIT_QUERY].is_read_only is True
        assert TRANSITIONS[ToolName.AUDIT_VERIFY].is_read_only is True
        assert TRANSITIONS[ToolName.COMPLIANCE_REPORT].is_read_only is True


# ═══════════════════════════════════════════════════════════════════════════
# §3  Composition Engine
# ═══════════════════════════════════════════════════════════════════════════


class TestCompositor:
    def setup_method(self):
        self.comp = Compositor()  # no constraints for pure algebra tests

    def test_valid_lifecycle(self):
        """create → start → exec → stop → destroy is valid."""
        vm = "test-vm"
        result = self.comp.validate([
            ToolInvocation(tool=ToolName.VM_CREATE, vm_id=vm, args={"name": vm}),
            ToolInvocation(tool=ToolName.VM_START, vm_id=vm),
            ToolInvocation(tool=ToolName.VM_EXEC, vm_id=vm, args={"command": "echo hi"}),
            ToolInvocation(tool=ToolName.VM_STOP, vm_id=vm),
            ToolInvocation(tool=ToolName.VM_DESTROY, vm_id=vm),
        ])
        assert result.valid is True
        assert result.steps_validated == 5
        assert result.final_state is not None
        assert result.final_state.get_vm_state(vm) == StateType.VM_DESTROYED

    def test_start_without_create_fails(self):
        """Cannot start a VM that doesn't exist."""
        result = self.comp.validate([
            ToolInvocation(tool=ToolName.VM_START, vm_id="ghost"),
        ])
        assert result.valid is False
        assert len(result.errors) == 1
        assert "vm.nonexistent" in result.errors[0].message

    def test_exec_on_stopped_fails(self):
        """Cannot exec on a stopped VM."""
        vm = "test-vm"
        result = self.comp.validate([
            ToolInvocation(tool=ToolName.VM_CREATE, vm_id=vm, args={"name": vm}),
            ToolInvocation(tool=ToolName.VM_EXEC, vm_id=vm, args={"command": "echo"}),
        ])
        assert result.valid is False
        assert "vm.running" in result.errors[0].message.lower() or "running" in result.errors[0].message.lower()

    def test_double_start(self):
        """Cannot start an already running VM (it's not in created/stopped)."""
        vm = "test-vm"
        result = self.comp.validate([
            ToolInvocation(tool=ToolName.VM_CREATE, vm_id=vm, args={"name": vm}),
            ToolInvocation(tool=ToolName.VM_START, vm_id=vm),
            ToolInvocation(tool=ToolName.VM_START, vm_id=vm),  # already running
        ])
        assert result.valid is False

    def test_stop_after_destroy_fails(self):
        """Cannot stop a destroyed VM."""
        vm = "test-vm"
        result = self.comp.validate([
            ToolInvocation(tool=ToolName.VM_CREATE, vm_id=vm, args={"name": vm}),
            ToolInvocation(tool=ToolName.VM_DESTROY, vm_id=vm),
            ToolInvocation(tool=ToolName.VM_STOP, vm_id=vm),
        ])
        assert result.valid is False

    def test_file_read_requires_running(self):
        vm = "test-vm"
        result = self.comp.validate([
            ToolInvocation(tool=ToolName.VM_CREATE, vm_id=vm, args={"name": vm}),
            ToolInvocation(tool=ToolName.VM_FILE_READ, vm_id=vm),
        ])
        assert result.valid is False  # not running yet

    def test_file_ops_when_running(self):
        vm = "test-vm"
        result = self.comp.validate([
            ToolInvocation(tool=ToolName.VM_CREATE, vm_id=vm, args={"name": vm}),
            ToolInvocation(tool=ToolName.VM_START, vm_id=vm),
            ToolInvocation(tool=ToolName.VM_FILE_READ, vm_id=vm),
            ToolInvocation(tool=ToolName.VM_FILE_WRITE, vm_id=vm),
        ])
        assert result.valid is True

    def test_sandbox_run(self):
        result = self.comp.validate([
            ToolInvocation(tool=ToolName.SANDBOX_RUN, args={"code": "print(1)"}),
        ])
        assert result.valid is True

    def test_read_only_ops_dont_dirty_audit(self):
        state = IDENTITY_STATE
        result = self.comp.validate([
            ToolInvocation(tool=ToolName.AUDIT_QUERY),
            ToolInvocation(tool=ToolName.COMPLIANCE_REPORT),
        ], state)
        assert result.valid is True
        assert result.final_state.audit_sequence == 0  # unchanged

    def test_mutating_ops_evolve_audit(self):
        vm = "test-vm"
        result = self.comp.validate([
            ToolInvocation(tool=ToolName.VM_CREATE, vm_id=vm, args={"name": vm}),
            ToolInvocation(tool=ToolName.VM_START, vm_id=vm),
        ])
        assert result.valid is True
        assert result.final_state.audit_sequence == 2
        assert result.final_state.audit_hash != "genesis"

    def test_multi_vm_independence(self):
        """Operations on different VMs are independent."""
        result = self.comp.validate([
            ToolInvocation(tool=ToolName.VM_CREATE, vm_id="vm-a", args={"name": "a"}),
            ToolInvocation(tool=ToolName.VM_CREATE, vm_id="vm-b", args={"name": "b"}),
            ToolInvocation(tool=ToolName.VM_START, vm_id="vm-a"),
            ToolInvocation(tool=ToolName.VM_DESTROY, vm_id="vm-b"),
        ])
        assert result.valid is True
        assert result.final_state.get_vm_state("vm-a") == StateType.VM_RUNNING
        assert result.final_state.get_vm_state("vm-b") == StateType.VM_DESTROYED

    def test_identity_is_noop(self):
        vm = "test-vm"
        state = IDENTITY_STATE.with_vm(vm, StateType.VM_CREATED)
        result = self.comp.validate([
            ToolInvocation(tool=ToolName.IDENTITY),
        ], state)
        assert result.valid is True
        assert result.final_state.get_vm_state(vm) == StateType.VM_CREATED


# ═══════════════════════════════════════════════════════════════════════════
# §4  Axiom Verification
# ═══════════════════════════════════════════════════════════════════════════


class TestAxioms:
    def test_all_axioms_hold(self):
        results = verify_axioms()
        for r in results:
            assert r.holds is True, f"Axiom {r.axiom} failed: {r.message}"

    def test_identity_axiom(self):
        v = AxiomVerifier()
        r = v.verify_identity()
        assert r.holds is True

    def test_closure_axiom(self):
        v = AxiomVerifier()
        r = v.verify_closure()
        assert r.holds is True

    def test_associativity_axiom(self):
        v = AxiomVerifier()
        r = v.verify_associativity()
        assert r.holds is True

    def test_audit_monotonicity_axiom(self):
        v = AxiomVerifier()
        r = v.verify_audit_monotonicity()
        assert r.holds is True

    def test_audit_irreversibility_axiom(self):
        v = AxiomVerifier()
        r = v.verify_audit_irreversibility()
        assert r.holds is True

    def test_transition_determinism(self):
        v = AxiomVerifier()
        r = v.verify_transition_determinism()
        assert r.holds is True


# ═══════════════════════════════════════════════════════════════════════════
# §5  Constraint Subalgebra
# ═══════════════════════════════════════════════════════════════════════════


class TestConstraints:
    def test_audit_integrity_constraint(self):
        """SOC2-CC7.2: Cannot read files when audit is tampered."""
        comp = Compositor(constraints=DEFAULT_CONSTRAINTS)
        state = IDENTITY_STATE.with_vm("vm-1", StateType.VM_RUNNING)
        state = state.with_audit(StateType.AUDIT_TAMPERED)

        result = comp.validate([
            ToolInvocation(tool=ToolName.VM_FILE_READ, vm_id="vm-1"),
        ], state)
        assert result.valid is False
        assert "SOC2" in result.errors[0].message

    def test_max_concurrent_vms(self):
        """ISO27001-A.13.1: Max concurrent VMs constraint."""
        from virtualize.core.algebra import _max_concurrent_vms

        constraint = _max_concurrent_vms(limit=2)
        comp = Compositor(constraints=[constraint])

        # Create & start 3 VMs — third should fail
        chain = []
        for i in range(3):
            vm = f"vm-{i}"
            chain.append(ToolInvocation(tool=ToolName.VM_CREATE, vm_id=vm, args={"name": vm}))
            chain.append(ToolInvocation(tool=ToolName.VM_START, vm_id=vm))

        result = comp.validate(chain)
        assert result.valid is False
        assert any("concurrent" in e.message.lower() for e in result.errors)

    def test_no_constraints_allows_all(self):
        """With no constraints, all valid transitions are allowed."""
        comp = Compositor(constraints=[])
        state = IDENTITY_STATE.with_vm("vm-1", StateType.VM_RUNNING)
        state = state.with_audit(StateType.AUDIT_TAMPERED)

        result = comp.validate([
            ToolInvocation(tool=ToolName.VM_FILE_READ, vm_id="vm-1"),
        ], state)
        assert result.valid is True  # no constraint to block it

    def test_custom_constraint(self):
        """User-defined constraint subalgebra."""
        def no_destroy_allowed(inv, state, step):
            if inv.tool == ToolName.VM_DESTROY:
                return CompositionError(
                    step=step, tool=inv.tool, vm_id=inv.vm_id,
                    expected_states=frozenset(), actual_state=StateType.VM_RUNNING,
                    message="Custom policy: destroy is forbidden",
                )
            return None

        constraint = CompositionConstraint("no_destroy", "Forbid destroy", no_destroy_allowed)
        comp = Compositor(constraints=[constraint])

        result = comp.validate([
            ToolInvocation(tool=ToolName.VM_CREATE, vm_id="vm-1", args={"name": "vm-1"}),
            ToolInvocation(tool=ToolName.VM_DESTROY, vm_id="vm-1"),
        ])
        assert result.valid is False
        assert "forbidden" in result.errors[0].message.lower()


# ═══════════════════════════════════════════════════════════════════════════
# §6  Algebraic Rewriting
# ═══════════════════════════════════════════════════════════════════════════


class TestRewriting:
    def test_identity_elimination(self):
        result = rewrite_plan([
            ("identity", None, {}),
            ("vm_create", "vm-1", {"name": "vm-1"}),
            ("identity", None, {}),
            ("vm_start", "vm-1", {}),
            ("identity", None, {}),
        ])
        tools = [r[0] for r in result]
        assert "identity" not in tools
        assert len(result) == 2

    def test_idempotent_collapse(self):
        """Consecutive identical read-only ops collapse."""
        result = rewrite_plan([
            ("vm_create", "vm-1", {"name": "vm-1"}),
            ("vm_status", "vm-1", {}),
            ("vm_status", "vm-1", {}),
            ("vm_status", "vm-1", {}),
        ])
        status_count = sum(1 for r in result if r[0] == "vm_status")
        assert status_count == 1

    def test_create_destroy_annihilation(self):
        """create immediately followed by destroy = no-op."""
        result = rewrite_plan([
            ("vm_create", "vm-1", {"name": "vm-1"}),
            ("vm_destroy", "vm-1", {}),
        ])
        assert len(result) == 0

    def test_dead_code_after_destroy(self):
        """Operations after destroy on same VM are eliminated."""
        result = rewrite_plan([
            ("vm_create", "vm-1", {"name": "vm-1"}),
            ("vm_start", "vm-1", {}),
            ("vm_destroy", "vm-1", {}),
            ("vm_start", "vm-1", {}),   # dead — VM is destroyed
            ("vm_exec", "vm-1", {}),    # dead
        ])
        # Should have create, start, destroy only
        assert len(result) == 3
        assert result[-1][0] == "vm_destroy"

    def test_no_rewrite_needed(self):
        """A clean chain is unchanged."""
        original = [
            ("vm_create", "vm-1", {"name": "vm-1"}),
            ("vm_start", "vm-1", {}),
            ("vm_exec", "vm-1", {"command": "echo hi"}),
        ]
        result = rewrite_plan(original)
        assert len(result) == 3

    def test_independent_vms_not_affected(self):
        """Rewriting only affects ops on the same VM."""
        result = rewrite_plan([
            ("vm_create", "vm-1", {"name": "vm-1"}),
            ("vm_destroy", "vm-1", {}),
            ("vm_create", "vm-2", {"name": "vm-2"}),
            ("vm_start", "vm-2", {}),
        ])
        # vm-1 create+destroy annihilated, vm-2 untouched
        assert len(result) == 2
        assert result[0][1] == "vm-2"


# ═══════════════════════════════════════════════════════════════════════════
# §7  High-Level API
# ═══════════════════════════════════════════════════════════════════════════


class TestHighLevelAPI:
    def test_validate_plan_valid(self):
        result = validate_plan([
            ("vm_create", None, {"name": "my-vm"}),
            ("vm_start", "my-vm", {}),
            ("vm_exec", "my-vm", {"command": "echo hello"}),
            ("vm_stop", "my-vm", {}),
            ("vm_destroy", "my-vm", {}),
        ])
        assert result.valid is True

    def test_validate_plan_invalid(self):
        result = validate_plan([
            ("vm_exec", "ghost", {"command": "echo"}),
        ])
        assert result.valid is False

    def test_verify_axioms_returns_results(self):
        results = verify_axioms()
        assert len(results) >= 6
        assert all(r.holds for r in results)


# ═══════════════════════════════════════════════════════════════════════════
# §8  Non-commutativity proof
# ═══════════════════════════════════════════════════════════════════════════


class TestNonCommutativity:
    def test_create_start_is_not_start_create(self):
        """Prove the algebra is non-commutative: create ∘ start ≠ start ∘ create."""
        comp = Compositor()
        vm = "nc-test"

        # create then start = valid
        r1 = comp.validate([
            ToolInvocation(tool=ToolName.VM_CREATE, vm_id=vm, args={"name": vm}),
            ToolInvocation(tool=ToolName.VM_START, vm_id=vm),
        ])

        # start then create = invalid (can't start nonexistent)
        r2 = comp.validate([
            ToolInvocation(tool=ToolName.VM_START, vm_id=vm),
            ToolInvocation(tool=ToolName.VM_CREATE, vm_id=vm, args={"name": vm}),
        ])

        assert r1.valid is True
        assert r2.valid is False  # proves non-commutativity

    def test_exec_stop_is_not_stop_exec(self):
        """exec ∘ stop ≠ stop ∘ exec on a running VM."""
        comp = Compositor()
        vm = "nc-test"
        state = IDENTITY_STATE.with_vm(vm, StateType.VM_RUNNING)

        # exec then stop = valid
        r1 = comp.validate([
            ToolInvocation(tool=ToolName.VM_EXEC, vm_id=vm, args={"command": "echo"}),
            ToolInvocation(tool=ToolName.VM_STOP, vm_id=vm),
        ], state)

        # stop then exec = invalid (can't exec on stopped)
        r2 = comp.validate([
            ToolInvocation(tool=ToolName.VM_STOP, vm_id=vm),
            ToolInvocation(tool=ToolName.VM_EXEC, vm_id=vm, args={"command": "echo"}),
        ], state)

        assert r1.valid is True
        assert r2.valid is False
