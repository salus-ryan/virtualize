# Virtualize — Agent Context

> This file is for LLMs (Claude, GPT, Gemini, etc.) operating on or with this codebase.
> Read this before taking any action.

## What this project is

Virtualize is a **free, cross-platform VM orchestration platform** for AI workflows. It exposes VM lifecycle, sandboxed execution, file I/O, compliance reporting, and audit querying as **typed algebraic operations** over a formally verified state machine.

Every operation is a morphism in a **typed, finite, partially-defined monoidal category with audit-preserving invariants**. The algebra validates tool chains at plan-time, before any VM is touched.

## Repository layout

```
src/virtualize/
├── core/
│   ├── algebra.py         # Formal algebra: states, transitions, compositor, axioms, rewriting
│   ├── models.py          # Pydantic data models (VMConfig, VMInstance, ExecRequest, AuditEvent)
│   ├── manager.py         # VMManager — orchestrates lifecycle with algebraic pre-validation
│   ├── hypervisor.py      # Cross-platform QEMU abstraction (KVM/HVF/WHPX)
│   ├── mock_hypervisor.py # Mock backend for dev/testing without QEMU
│   └── bootstrap.py       # OS-detecting setup system
├── agent/
│   └── nl_agent.py        # NL→algebra agent (local LLM translates English → tool chains)
├── sandbox/
│   └── executor.py        # Sandboxed code execution with pooled VMs
├── compliance/
│   ├── audit.py           # Append-only, integrity-chained audit log (SHA-256 HMAC)
│   └── policies.py        # SOC 1/2/3, HIPAA, ISO 27001 policy controls
├── mcp_server/
│   └── server.py          # MCP server — 13 tools over stdio transport
├── api/
│   ├── server.py          # FastAPI REST server (port 8420)
│   └── dashboard.py       # Built-in React/Tailwind web dashboard
└── cli/
    └── main.py            # Typer CLI (vm lifecycle, sandbox, compliance, algebra, ask)
tests/
├── test_algebra.py        # 49 tests — carrier set, transitions, composition, axioms, rewriting
├── test_agent.py          # 19 tests — NL agent with mock LLM
├── test_api.py            # 14 tests — REST API endpoints
├── test_compliance.py     # 9 tests  — audit log, policies
└── test_models.py         # 12 tests — data models
```

## The algebra (critical context)

### Carrier set (states)

```
VM states:      vm.nonexistent, vm.created, vm.running, vm.stopped, vm.paused, vm.destroyed
Sandbox states: sandbox.idle, sandbox.executing, sandbox.complete
Filesystem:     fs.readable (when VM running), fs.inaccessible (otherwise)
Audit:          audit.clean, audit.dirty, audit.tampered
```

### Generator set (13 typed morphisms)

| Tool | Precondition (source) | Postcondition (target) | Needs vm_id | Read-only |
|------|----------------------|----------------------|-------------|-----------|
| `identity` | * | * | no | yes |
| `vm_create` | vm.nonexistent | vm.created | no (creates new) | no |
| `vm_start` | vm.created \| vm.stopped | vm.running | yes | no |
| `vm_stop` | vm.running \| vm.paused | vm.stopped | yes | no |
| `vm_destroy` | vm.created \| vm.running \| vm.stopped \| vm.paused | vm.destroyed | yes | no |
| `vm_status` | vm.created \| vm.running \| vm.stopped \| vm.paused | (unchanged) | yes | yes |
| `vm_exec` | vm.running | (unchanged) | yes | no |
| `sandbox_run` | sandbox.idle | sandbox.idle | no | no |
| `vm_file_read` | vm.running | (unchanged) | yes | yes |
| `vm_file_write` | vm.running | (unchanged) | yes | no |
| `audit_query` | * | (unchanged) | no | yes |
| `audit_verify` | * | (unchanged) | no | yes |
| `compliance_report` | * | (unchanged) | no | yes |

### Verified axioms

These are programmatically verified (`virtualize algebra verify`):

1. **Identity**: `id ∘ t = t = t ∘ id` for all generators
2. **Closure**: All generators map C → C
3. **Associativity**: `(t₁ ∘ t₂) ∘ t₃ = t₁ ∘ (t₂ ∘ t₃)`
4. **Audit monotonicity**: `A_{n+1}.seq ≥ A_n.seq` (sequence never decreases)
5. **Audit irreversibility**: `∄ t such that t(A_n) = A_{n-1}` (no tool can undo an audit entry)
6. **Transition determinism**: Each (tool, input_state) pair produces exactly one output state

### Key properties

- **Non-commutative**: `create ∘ start ≠ start ∘ create`
- **Partially defined**: `vm_exec` on a stopped VM is undefined (rejected)
- **Audit chain**: `A_{n+1} = H(A_n ∥ e_n)` — hash-chained, monotonic, irreversible
- **Constraint subalgebra**: Compliance policies define `T_valid ⊆ T*`

### Rewriting laws

The algebra supports these optimization rules:

1. **Identity elimination**: Remove all `identity` invocations
2. **Idempotent collapse**: Consecutive identical read-only ops → single op
3. **Annihilation**: `vm_create ∘ vm_destroy` (with no ops between) → ε (empty)
4. **Dead code elimination**: Ops on a VM after `vm_destroy` are removed

## How to generate valid tool chains

When generating plans, output a JSON array of steps:

```json
[
  ["tool_name", "vm_id_or_null", {"arg": "value"}],
  ...
]
```

### Rules

1. `vm_create` takes `vm_id = null` and requires `{"name": "..."}` in args
2. Subsequent steps reference the VM by the name given in `vm_create`
3. `vm_exec` requires `{"command": "..."}` in args
4. `vm_file_write` requires `{"path": "...", "content": "..."}` in args
5. `sandbox_run` requires `{"code": "...", "language": "..."}` in args
6. You MUST respect the transition table — e.g., you cannot `vm_exec` before `vm_start`
7. You MUST respect ordering — the algebra is non-commutative

### Valid lifecycle example

```json
[
  ["vm_create", null, {"name": "my-vm"}],
  ["vm_start", "my-vm", {}],
  ["vm_exec", "my-vm", {"command": "uname -a"}],
  ["vm_stop", "my-vm", {}],
  ["vm_destroy", "my-vm", {}]
]
```

### Invalid examples (and why)

```json
// INVALID: vm_exec requires vm.running, but "ghost" is vm.nonexistent
[["vm_exec", "ghost", {"command": "echo"}]]

// INVALID: vm_start requires vm.created|vm.stopped, but vm is vm.running
[["vm_create", null, {"name": "x"}], ["vm_start", "x", {}], ["vm_start", "x", {}]]

// INVALID: vm_stop requires vm.running|vm.paused, but vm is vm.destroyed
[["vm_create", null, {"name": "x"}], ["vm_destroy", "x", {}], ["vm_stop", "x", {}]]
```

## Validation

Plans are validated against the algebra BEFORE execution:

```python
from virtualize.core.algebra import validate_plan

result = validate_plan([
    ("vm_create", None, {"name": "my-vm"}),
    ("vm_start", "my-vm", {}),
])
assert result.valid is True
```

Invalid plans return specific error messages describing the algebraic violation.

## Compliance constraints (subalgebra)

Active constraints that further restrict valid plans:

- **SOC2-CC7.2**: Cannot read files (`vm_file_read`) when `audit_state == audit.tampered`
- **ISO27001-A.13.1**: Maximum concurrent running VMs (configurable limit)
- Custom constraints can be added as `CompositionConstraint` objects

## CLI commands

```bash
virtualize create <name>                    # Create VM
virtualize start <vm_id>                    # Start VM
virtualize stop <vm_id>                     # Stop VM
virtualize destroy <vm_id>                  # Destroy VM
virtualize exec <vm_id> "<command>"         # Run command in VM
virtualize list                             # List VMs
virtualize sandbox run "<code>"             # Sandboxed execution
virtualize ask "<english>"                  # NL → algebra (requires .[agent])
virtualize ask "<english>" --execute        # NL → algebra → execute
virtualize algebra verify                   # Verify all 6 axioms
virtualize algebra validate '<json_plan>'   # Validate a tool chain
virtualize algebra rewrite '<json_plan>'    # Optimize via algebraic laws
virtualize algebra state                    # Show transition table
virtualize compliance report <framework>    # soc1, soc2, soc3, hipaa, iso27001
virtualize setup                            # OS-detecting QEMU installer
virtualize doctor                           # System health check
```

## MCP server

13 tools exposed via Model Context Protocol (stdio transport):

```json
{
  "mcpServers": {
    "virtualize": {
      "command": "python",
      "args": ["-m", "virtualize.mcp_server.server"]
    }
  }
}
```

## Development

```bash
# Setup
git clone https://github.com/salus-ryan/virtualize.git
cd virtualize
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Tests (103 passing)
pytest

# With NL agent
pip install -e ".[agent]"

# Verify algebra
virtualize algebra verify
```

## Architecture invariants (do not break these)

1. Every mutating VM operation MUST go through `VMManager`, which calls `_pre_validate()` against the algebra
2. The audit log is append-only and integrity-chained — never delete or modify entries
3. The `SystemState` is immutable (frozen dataclass) — state evolution creates new instances
4. All transitions are defined in `TRANSITIONS` dict in `algebra.py` — add new tools there
5. Tests must pass: `pytest` should show 103 passed, 0 warnings
