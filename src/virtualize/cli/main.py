"""Virtualize CLI — manage VMs from the command line.

Usage:
    virtualize create my-vm --cpus 4 --memory 4096 --disk 50
    virtualize start <vm_id>
    virtualize stop <vm_id>
    virtualize destroy <vm_id>
    virtualize list
    virtualize exec <vm_id> "uname -a"
    virtualize sandbox run "print('hello')" --language python
    virtualize compliance report soc2
    virtualize audit verify
    virtualize mcp serve
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

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
)

app = typer.Typer(
    name="virtualize",
    help="Free, cross-platform VM orchestration for AI workflows",
    no_args_is_help=False,
    invoke_without_command=True,
)
console = Console()

# Sub-command groups
sandbox_app = typer.Typer(help="Sandboxed code execution")
compliance_app = typer.Typer(help="Compliance & audit tools")
mcp_app = typer.Typer(help="MCP server management")
algebra_app = typer.Typer(help="Formal algebra verification & planning tools")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(compliance_app, name="compliance")
app.add_typer(mcp_app, name="mcp")
app.add_typer(algebra_app, name="algebra")


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


def _get_manager() -> tuple[VMManager, AuditLog]:
    audit_log = AuditLog()
    manager = VMManager(audit_callback=audit_log.record)
    return manager, audit_log


@app.callback()
def main_callback(ctx: typer.Context):
    """Launch interactive shell if no subcommand is given."""
    if ctx.invoked_subcommand is None:
        _interactive_shell()


def _interactive_shell():
    """Interactive REPL — every input goes through the NL agent."""
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn

    console.print()
    console.print(Panel(
        "[bold]Virtualize Interactive Shell[/bold]\n\n"
        "Type anything in plain English. I'll figure out what to do.\n"
        "Examples: 'create a vm', 'check hipaa compliance', 'help'\n\n"
        "[dim]Type 'exit' or 'quit' to leave. Ctrl+C works too.[/dim]",
        border_style="blue",
        padding=(1, 2),
    ))
    console.print()

    try:
        from virtualize.agent.nl_agent import NLAgent
    except Exception:
        console.print("[red]Agent dependencies not installed.[/red]")
        console.print("Run: [cyan]pip install -e '.[agent]'[/cyan]")
        console.print()
        console.print("[dim]Or use explicit commands: virtualize --help[/dim]")
        raise typer.Exit(code=1)

    # Load model once
    with Progress(SpinnerColumn(), TextColumn("[bold blue]Loading model (first time may download ~1GB)..."),
                  console=console, transient=True) as prog:
        prog.add_task("load", total=None)
        agent = NLAgent(n_gpu_layers=-1)
        agent._ensure_llm()

    console.print("[green]Ready.[/green]")
    console.print()

    manager, audit_log = _get_manager()

    while True:
        try:
            user_input = console.input("[bold cyan]virtualize>[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye.[/dim]")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "q"):
            console.print("[dim]Goodbye.[/dim]")
            break

        # Send to agent
        with Progress(SpinnerColumn(), TextColumn("[bold blue]Thinking..."),
                      console=console, transient=True) as prog:
            prog.add_task("think", total=None)
            result = agent.plan(user_input, system_state=manager.system_state)

        # Clarification — agent needs more info
        if result.clarification:
            console.print(f"\n  [yellow]{result.clarification}[/yellow]\n")
            continue

        # Error — couldn't parse or validate
        if result.error:
            console.print(f"\n  [red]{result.error}[/red]\n")
            continue

        # Show the plan
        console.print()
        console.print(f"  [bold]Plan:[/bold] {result.explanation}")
        if result.validation and result.validation.valid:
            console.print(f"  [green]VALID[/green] — {result.validation.steps_validated} steps")
        else:
            console.print(f"  [red]INVALID[/red]")
            if result.validation:
                for err in result.validation.errors:
                    console.print(f"    [red]{err.message}[/red]")
            console.print()
            continue

        console.print(f"  [dim]JSON: {json.dumps(result.plan)}[/dim]")
        console.print()

        # Ask to execute
        try:
            execute = typer.confirm("  Execute this plan?", default=True)
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Skipped.[/dim]")
            continue

        if not execute:
            console.print("  [dim]Skipped.[/dim]\n")
            continue

        # Execute
        with Progress(SpinnerColumn(), TextColumn("[bold]Executing..."),
                      console=console, transient=True) as prog:
            prog.add_task("exec", total=None)
            exec_result = _run(agent.plan_and_execute(user_input, manager=manager))

        console.print()
        for step in exec_result.execution_results:
            tool = step.get("tool", "?")
            if "error" in step:
                console.print(f"  [red]✗[/red] {tool}: {step['error']}")
            else:
                status = step.get("status", step.get("stdout", "done"))
                vm_id = step.get("vm_id", "")
                extra = f" ({vm_id})" if vm_id else ""
                console.print(f"  [green]✓[/green] {tool}{extra}: {status}")

        if exec_result.error:
            console.print(f"\n  [red]{exec_result.error}[/red]")
        console.print()


# ---------------------------------------------------------------------------
# VM lifecycle commands
# ---------------------------------------------------------------------------


@app.command()
def create(
    name: str = typer.Argument(..., help="VM name"),
    cpus: int = typer.Option(2, "--cpus", "-c", help="Number of vCPUs"),
    memory: int = typer.Option(2048, "--memory", "-m", help="Memory in MB"),
    disk: int = typer.Option(20, "--disk", "-d", help="Disk size in GB"),
    os_type: str = typer.Option("linux", "--os", help="OS type"),
    gpu: str = typer.Option("none", "--gpu", "-g", help="GPU mode: none, passthrough, virtual"),
    network: str = typer.Option("nat", "--network", "-n", help="Network mode: nat, bridge, isolated, host"),
    base_image: Optional[str] = typer.Option(None, "--image", "-i", help="Base image path"),
    iso: Optional[str] = typer.Option(None, "--iso", help="Boot ISO path"),
):
    """Create a new virtual machine."""
    manager, _ = _get_manager()
    config = VMConfig(
        name=name,
        vcpus=cpus,
        memory_mb=memory,
        disk=DiskConfig(size_gb=disk),
        os_type=os_type,
        gpu=GPUConfig(mode=GPUMode(gpu)),
        network=NetworkConfig(mode=NetworkMode(network)),
        base_image=base_image,
        iso_path=iso,
    )
    vm = _run(manager.create(config, actor="cli"))
    console.print(f"[green]Created VM[/green] {vm.id} ({vm.config.name})")
    console.print(f"  vCPUs: {config.vcpus}  Memory: {config.memory_mb}MB  Disk: {config.disk.size_gb}GB")
    console.print(f"  GPU: {config.gpu.mode.value}  Network: {config.network.mode.value}")


@app.command()
def start(vm_id: str = typer.Argument(..., help="VM identifier")):
    """Start a virtual machine."""
    manager, _ = _get_manager()
    # Need to re-create — in a real implementation, state would be persisted
    vm = _run(manager.start(vm_id, actor="cli"))
    console.print(f"[green]Started[/green] {vm.id}")
    if vm.ssh_port:
        console.print(f"  SSH: ssh -p {vm.ssh_port} root@{vm.ip_address}")


@app.command()
def stop(
    vm_id: str = typer.Argument(..., help="VM identifier"),
    force: bool = typer.Option(False, "--force", "-f", help="Force stop"),
):
    """Stop a virtual machine."""
    manager, _ = _get_manager()
    vm = _run(manager.stop(vm_id, force=force, actor="cli"))
    console.print(f"[yellow]Stopped[/yellow] {vm.id}")


@app.command()
def destroy(vm_id: str = typer.Argument(..., help="VM identifier")):
    """Destroy a VM and all its data."""
    manager, _ = _get_manager()
    vm = _run(manager.destroy(vm_id, actor="cli"))
    console.print(f"[red]Destroyed[/red] {vm.id}")


@app.command(name="list")
def list_vms():
    """List all virtual machines."""
    manager, _ = _get_manager()
    vms = _run(manager.list_vms())

    if not vms:
        console.print("[dim]No VMs found[/dim]")
        return

    table = Table(title="Virtual Machines")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="bold")
    table.add_column("Status")
    table.add_column("vCPUs", justify="right")
    table.add_column("Memory", justify="right")
    table.add_column("Disk", justify="right")
    table.add_column("GPU")
    table.add_column("Network")
    table.add_column("SSH")

    for vm in vms:
        status_color = {
            "running": "green",
            "stopped": "red",
            "creating": "yellow",
            "error": "red bold",
        }.get(vm.status.value, "dim")

        ssh = f":{vm.ssh_port}" if vm.ssh_port else "-"
        table.add_row(
            vm.id,
            vm.config.name,
            f"[{status_color}]{vm.status.value}[/{status_color}]",
            str(vm.config.vcpus),
            f"{vm.config.memory_mb}MB",
            f"{vm.config.disk.size_gb}GB",
            vm.config.gpu.mode.value,
            vm.config.network.mode.value,
            ssh,
        )

    console.print(table)


@app.command(name="exec")
def exec_cmd(
    vm_id: str = typer.Argument(..., help="VM identifier"),
    command: str = typer.Argument(..., help="Command to execute"),
    timeout: int = typer.Option(60, "--timeout", "-t", help="Timeout in seconds"),
):
    """Execute a command inside a VM."""
    manager, _ = _get_manager()
    request = ExecRequest(vm_id=vm_id, command=command, timeout_seconds=timeout)
    result = _run(manager.exec(request, actor="cli"))

    if result.stdout:
        console.print(result.stdout, end="")
    if result.stderr:
        console.print(f"[red]{result.stderr}[/red]", end="")
    if result.timed_out:
        console.print("[red]Command timed out[/red]")

    raise typer.Exit(code=result.exit_code)


@app.command()
def status(vm_id: str = typer.Argument(..., help="VM identifier")):
    """Get detailed VM status."""
    manager, _ = _get_manager()
    vm = manager.get_vm(vm_id)
    live_status = _run(manager.status(vm_id))

    console.print(f"[bold]{vm.config.name}[/bold] ({vm.id})")
    console.print(f"  Status:     {live_status.value}")
    console.print(f"  vCPUs:      {vm.config.vcpus}")
    console.print(f"  Memory:     {vm.config.memory_mb}MB")
    console.print(f"  Disk:       {vm.config.disk.size_gb}GB")
    console.print(f"  GPU:        {vm.config.gpu.mode.value}")
    console.print(f"  Network:    {vm.config.network.mode.value}")
    console.print(f"  IP:         {vm.ip_address or '-'}")
    console.print(f"  SSH Port:   {vm.ssh_port or '-'}")
    console.print(f"  PID:        {vm.pid or '-'}")
    console.print(f"  Created:    {vm.created_at}")
    if vm.uptime_seconds is not None:
        console.print(f"  Uptime:     {vm.uptime_seconds:.0f}s")


# ---------------------------------------------------------------------------
# Sandbox commands
# ---------------------------------------------------------------------------


@sandbox_app.command(name="run")
def sandbox_run(
    code: str = typer.Argument(..., help="Code to execute"),
    language: str = typer.Option("python", "--lang", "-l", help="Language"),
    timeout: int = typer.Option(60, "--timeout", "-t", help="Timeout in seconds"),
):
    """Run code in an isolated sandbox VM."""
    from virtualize.sandbox.executor import SandboxExecutor

    manager, _ = _get_manager()
    executor = SandboxExecutor(manager)
    result = _run(executor.run(code=code, language=language, timeout=timeout, actor="cli"))

    if result.stdout:
        console.print(result.stdout, end="")
    if result.stderr:
        console.print(f"[red]{result.stderr}[/red]", end="")

    console.print(f"\n[dim]Exit: {result.exit_code} | Duration: {result.duration_seconds}s | Timed out: {result.timed_out}[/dim]")
    raise typer.Exit(code=result.exit_code)


# ---------------------------------------------------------------------------
# Compliance commands
# ---------------------------------------------------------------------------


@compliance_app.command(name="report")
def compliance_report(
    framework: str = typer.Argument(..., help="Framework: soc1, soc2, soc3, hipaa, iso27001"),
):
    """Generate a compliance posture report."""
    from virtualize.compliance.policies import ComplianceFramework, generate_report

    report = generate_report(ComplianceFramework(framework))

    status = "[green]COMPLIANT[/green]" if report.compliant else "[red]NON-COMPLIANT[/red]"
    console.print(f"\n[bold]{report.framework.value.upper()} Compliance Report[/bold]  {status}\n")

    table = Table()
    table.add_column("ID", style="cyan")
    table.add_column("Title", style="bold")
    table.add_column("Technical Control")
    table.add_column("Status")

    for ctrl in report.controls:
        st = "[green]Enabled[/green]" if ctrl.enabled else "[red]Disabled[/red]"
        table.add_row(ctrl.id, ctrl.title, ctrl.technical_control[:80], st)

    console.print(table)
    console.print(f"\n  Total: {report.total_controls}  Enabled: {report.enabled_controls}  Disabled: {report.disabled_controls}")


@compliance_app.command(name="controls")
def list_controls(
    framework: Optional[str] = typer.Option(None, "--framework", "-f", help="Filter by framework"),
):
    """List all compliance controls."""
    from virtualize.compliance.policies import ComplianceFramework, get_controls

    fw = ComplianceFramework(framework) if framework else None
    controls = get_controls(fw)

    table = Table(title="Compliance Controls")
    table.add_column("ID", style="cyan")
    table.add_column("Framework")
    table.add_column("Title", style="bold")
    table.add_column("Description")

    for ctrl in controls:
        table.add_row(ctrl.id, ctrl.framework.value.upper(), ctrl.title, ctrl.description[:60])

    console.print(table)


@compliance_app.command(name="audit-verify")
def audit_verify():
    """Verify audit log integrity."""
    audit_log = AuditLog()
    valid, count, message = audit_log.verify_integrity()

    if valid:
        console.print(f"[green]PASS[/green] {message}")
    else:
        console.print(f"[red]FAIL[/red] {message}")
        raise typer.Exit(code=1)


@compliance_app.command(name="audit-query")
def audit_query(
    action: Optional[str] = typer.Option(None, "--action", "-a"),
    actor: Optional[str] = typer.Option(None, "--actor"),
    resource_id: Optional[str] = typer.Option(None, "--resource", "-r"),
    limit: int = typer.Option(50, "--limit", "-n"),
):
    """Query audit log events."""
    audit_log = AuditLog()
    events = audit_log.query(action=action, actor=actor, resource_id=resource_id, limit=limit)

    if not events:
        console.print("[dim]No matching events[/dim]")
        return

    table = Table(title=f"Audit Events ({len(events)} results)")
    table.add_column("Time", style="dim")
    table.add_column("Action", style="cyan")
    table.add_column("Actor")
    table.add_column("Resource")
    table.add_column("Success")

    for evt in events:
        success = "[green]Yes[/green]" if evt.get("success") else "[red]No[/red]"
        table.add_row(
            evt.get("timestamp", "")[:19],
            evt.get("action", ""),
            evt.get("actor", ""),
            evt.get("resource_id", "-"),
            success,
        )

    console.print(table)


# ---------------------------------------------------------------------------
# MCP server command
# ---------------------------------------------------------------------------


@mcp_app.command(name="serve")
def mcp_serve():
    """Start the MCP server (stdio transport for agent integration)."""
    from virtualize.mcp_server.server import run_mcp_server

    console.print("[bold]Starting Virtualize MCP Server...[/bold]")
    console.print("Listening on stdio. Connect your AI agent to this process.")
    asyncio.run(run_mcp_server())


# ---------------------------------------------------------------------------
# NL Agent — natural language → algebra
# ---------------------------------------------------------------------------


@app.command()
def ask(
    query: str = typer.Argument(..., help="Natural language request, e.g. 'start me a vm for openclaw'"),
    execute: bool = typer.Option(False, "--execute", "-x", help="Execute the plan after validation"),
    gpu_layers: int = typer.Option(-1, "--gpu-layers", "-g", help="GPU layers for LLM (-1 = all, 0 = CPU only)"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Path to GGUF model file"),
):
    """Ask in plain English — the agent translates to algebraic tool chains."""
    from pathlib import Path as _Path

    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn

    try:
        from virtualize.agent.nl_agent import NLAgent
    except Exception:
        console.print("[red]Agent dependencies not installed.[/red] Run: [cyan]pip install -e '.[agent]'[/cyan]")
        raise typer.Exit(code=1)

    # Load model
    model_path = _Path(model) if model else None
    agent = NLAgent(model_path=model_path, n_gpu_layers=gpu_layers)

    console.print()
    console.print(f"  [bold]Query:[/bold] {query}")
    console.print()

    with Progress(SpinnerColumn(), TextColumn("[bold blue]Thinking..."), console=console, transient=True) as prog:
        prog.add_task("think", total=None)
        result = agent.plan(query)

    if result.error:
        console.print(f"[red]Error:[/red] {result.error}")
        raise typer.Exit(code=1)

    # Show plan
    console.print(Panel(
        result.explanation,
        title="[bold]Execution Plan[/bold]",
        border_style="blue",
        padding=(1, 2),
    ))

    # Show validation
    if result.validation and result.validation.valid:
        state = result.validation.final_state
        console.print(f"  [green]VALID[/green] — {result.validation.steps_validated} steps, "
                      f"audit seq → {state.audit_sequence}")
    else:
        console.print(f"  [red]INVALID[/red]")
        if result.validation:
            for err in result.validation.errors:
                console.print(f"    [red]{err.message}[/red]")
        raise typer.Exit(code=1)

    # Show raw plan
    console.print(f"\n  [dim]JSON: {json.dumps(result.plan)}[/dim]")

    if not execute:
        console.print(f"\n  [dim]Add --execute (-x) to run this plan.[/dim]\n")
        return

    # Execute
    console.print()
    proceed = typer.confirm("Execute this plan?", default=True)
    if not proceed:
        console.print("[dim]Cancelled.[/dim]")
        raise typer.Exit(code=0)

    console.print()
    with Progress(SpinnerColumn(), TextColumn("[bold]Executing..."), console=console, transient=True) as prog:
        prog.add_task("exec", total=None)
        result = _run(agent.plan_and_execute(query))

    if result.error:
        console.print(f"[red]Execution error:[/red] {result.error}")

    for step in result.execution_results:
        tool = step.get("tool", "?")
        if "error" in step:
            console.print(f"  [red]✗[/red] {tool}: {step['error']}")
        else:
            status = step.get("status", step.get("stdout", "done"))
            console.print(f"  [green]✓[/green] {tool}: {status}")

    console.print()


# ---------------------------------------------------------------------------
# Algebra commands
# ---------------------------------------------------------------------------


@algebra_app.command(name="verify")
def algebra_verify():
    """Verify all algebraic axioms hold for the tool algebra."""
    from virtualize.core.algebra import verify_axioms

    results = verify_axioms()
    all_ok = True

    console.print("\n[bold]Algebraic Axiom Verification[/bold]\n")
    for r in results:
        icon = "[green]PASS[/green]" if r.holds else "[red]FAIL[/red]"
        console.print(f"  {icon}  [bold]{r.axiom}[/bold] — {r.message}")
        if r.counterexample:
            console.print(f"         [red]Counterexample: {r.counterexample}[/red]")
        if not r.holds:
            all_ok = False

    console.print()
    if all_ok:
        console.print("[green]All axioms hold.[/green] The algebra is consistent.\n")
    else:
        console.print("[red]Some axioms failed.[/red] The algebra has structural issues.\n")
        raise typer.Exit(code=1)


@algebra_app.command(name="validate")
def algebra_validate(
    plan: str = typer.Argument(..., help='JSON plan: [["tool", "vm_id", {}], ...]'),
):
    """Validate a tool chain / execution plan against the algebra."""
    from virtualize.core.algebra import validate_plan

    try:
        steps = json.loads(plan)
    except json.JSONDecodeError as e:
        console.print(f"[red]Invalid JSON:[/red] {e}")
        raise typer.Exit(code=1)

    # Convert list-of-lists to list-of-tuples
    tool_steps = [(s[0], s[1] if len(s) > 1 else None, s[2] if len(s) > 2 else {}) for s in steps]
    result = validate_plan(tool_steps)

    if result.valid:
        console.print(f"\n[green]VALID[/green] — {result.steps_validated} steps validated")
        if result.final_state:
            vm_states = {k: v.value for k, v in result.final_state.vm_states.items()}
            console.print(f"  Final VM states: {vm_states}")
            console.print(f"  Audit sequence:  {result.final_state.audit_sequence}")
            console.print(f"  Audit hash:      {result.final_state.audit_hash}")
    else:
        console.print(f"\n[red]INVALID[/red] — {len(result.errors)} errors:")
        for err in result.errors:
            console.print(f"  Step {err.step}: [red]{err.message}[/red]")
        raise typer.Exit(code=1)
    console.print()


@algebra_app.command(name="rewrite")
def algebra_rewrite(
    plan: str = typer.Argument(..., help='JSON plan: [["tool", "vm_id", {}], ...]'),
):
    """Optimize a tool chain via algebraic rewriting laws."""
    from virtualize.core.algebra import rewrite_plan

    try:
        steps = json.loads(plan)
    except json.JSONDecodeError as e:
        console.print(f"[red]Invalid JSON:[/red] {e}")
        raise typer.Exit(code=1)

    tool_steps = [(s[0], s[1] if len(s) > 1 else None, s[2] if len(s) > 2 else {}) for s in steps]
    optimized = rewrite_plan(tool_steps)

    original_len = len(tool_steps)
    new_len = len(optimized)

    console.print(f"\n[bold]Algebraic Rewrite[/bold]")
    console.print(f"  Original: {original_len} steps → Optimized: {new_len} steps")
    if new_len < original_len:
        console.print(f"  [green]Eliminated {original_len - new_len} steps via algebraic laws[/green]")

    console.print(f"\nOptimized plan:")
    for i, (tool, vm_id, args) in enumerate(optimized):
        vm_str = f" ({vm_id})" if vm_id else ""
        console.print(f"  {i+1}. [cyan]{tool}[/cyan]{vm_str}")

    console.print(f"\n[dim]JSON: {json.dumps(optimized)}[/dim]\n")


@algebra_app.command(name="state")
def algebra_state():
    """Show the current algebraic system state."""
    from virtualize.core.algebra import SystemState, TRANSITIONS, ToolName

    console.print("\n[bold]Algebra State[/bold]\n")

    console.print(f"  [bold]Generators (tools):[/bold] {len(ToolName)} morphisms")
    console.print(f"  [bold]Transition rules:[/bold]  {len(TRANSITIONS)} typed")

    # Show the transition graph
    from rich.table import Table
    table = Table(title="Transition Rules (t: C_source → C_target)")
    table.add_column("Tool", style="cyan")
    table.add_column("Source States")
    table.add_column("Target State")
    table.add_column("Read-Only")
    table.add_column("Needs VM")

    for tool, rule in TRANSITIONS.items():
        if rule.is_identity:
            table.add_row(tool.value, "*", "*", "yes", "no")
            continue
        sources = ", ".join(s.value for s in rule.required_vm_states) if rule.required_vm_states else "-"
        target = rule.produced_vm_state.value if rule.produced_vm_state else "(unchanged)"
        table.add_row(
            tool.value,
            sources,
            target,
            "yes" if rule.is_read_only else "no",
            "yes" if rule.requires_vm else "no",
        )

    console.print(table)
    console.print()


# ---------------------------------------------------------------------------
# Setup / Bootstrap
# ---------------------------------------------------------------------------


@app.command()
def setup(
    auto: bool = typer.Option(False, "--auto", "-y", help="Auto-install without prompting"),
    check_only: bool = typer.Option(False, "--check", help="Only check system, don't install anything"),
):
    """Detect your OS and install QEMU + dependencies interactively."""
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn

    from virtualize.core.bootstrap import detect_system, get_install_steps, run_install_command

    # -- Step 1: Detect system --
    console.print()
    with Progress(SpinnerColumn(), TextColumn("[bold blue]Detecting system..."), console=console, transient=True) as progress:
        task = progress.add_task("detect", total=None)
        info = detect_system()

    # -- Step 2: Display detected info --
    accel = "unknown"
    if info.has_kvm:
        accel = "[green]KVM[/green] (hardware acceleration)"
    elif info.has_hvf:
        accel = "[green]Hypervisor.framework[/green] (hardware acceleration)"
    elif info.has_whpx:
        accel = "[green]WHPX[/green] (hardware acceleration)"
    elif info.cpu_virt_extensions:
        accel = "[yellow]Available but not enabled[/yellow] (VT-x/AMD-V detected)"
    else:
        accel = "[red]None[/red] (software emulation only)"

    qemu_status = f"[green]{info.qemu_version}[/green]" if info.has_qemu else "[red]Not installed[/red]"
    pkg_mgr = info.package_manager.value if info.package_manager.value != "unknown" else "[red]Not detected[/red]"

    gpu_text = "\n".join(f"    {g}" for g in info.gpu_devices) if info.gpu_devices else "    None detected"

    panel_text = (
        f"  [bold]OS:[/bold]              {info.os_name}\n"
        f"  [bold]Architecture:[/bold]    {info.arch}\n"
        f"  [bold]Package Manager:[/bold] {pkg_mgr}\n"
        f"  [bold]QEMU:[/bold]            {qemu_status}\n"
        f"  [bold]Acceleration:[/bold]    {accel}\n"
        f"  [bold]Sudo:[/bold]            {'[green]Yes[/green]' if info.has_sudo else '[yellow]No[/yellow]'}\n"
        f"  [bold]GPUs:[/bold]\n{gpu_text}"
    )
    console.print(Panel(panel_text, title="[bold]System Detection[/bold]", border_style="blue", padding=(1, 2)))

    # -- Step 3: Check if everything is already installed --
    if info.has_qemu and not info.missing_deps:
        console.print("\n[green bold]All dependencies are installed.[/green bold] You're ready to go!\n")
        console.print("  Run [cyan]virtualize create my-vm[/cyan] to create your first VM.")
        if info.has_kvm or info.has_hvf or info.has_whpx:
            console.print("  Hardware acceleration is [green]enabled[/green] — VMs will run at near-native speed.")
        console.print()
        return

    if check_only:
        console.print(f"\n[yellow]Missing dependencies:[/yellow] {', '.join(info.missing_deps)}")
        console.print("Run [cyan]virtualize setup[/cyan] (without --check) to install them.\n")
        raise typer.Exit(code=1)

    # -- Step 4: Generate install steps --
    steps = get_install_steps(info)

    if not steps:
        console.print("\n[yellow]Could not determine install steps for your system.[/yellow]")
        console.print("Please install QEMU manually: [link=https://www.qemu.org/download/]https://www.qemu.org/download/[/link]\n")
        raise typer.Exit(code=1)

    # -- Step 5: Show the install plan --
    console.print(f"\n[bold]Install Plan[/bold] ({len(steps)} steps)\n")
    for i, step in enumerate(steps, 1):
        opt = " [dim](optional)[/dim]" if step.optional else ""
        sudo = " [yellow](requires sudo)[/yellow]" if step.requires_sudo else ""
        if step.command:
            console.print(f"  {i}. {step.description}{opt}{sudo}")
            console.print(f"     [dim]$ {step.command}[/dim]")
        else:
            console.print(f"  {i}. [yellow]{step.description}[/yellow]{opt}")
    console.print()

    # -- Step 6: Prompt or auto-run --
    if not auto:
        proceed = typer.confirm("Proceed with installation?", default=True)
        if not proceed:
            console.print("[dim]Setup cancelled.[/dim]")
            raise typer.Exit(code=0)

    # -- Step 7: Execute each step --
    console.print()
    failed = False
    for i, step in enumerate(steps, 1):
        if step.command is None:
            console.print(f"  [{i}/{len(steps)}] [yellow]Manual step:[/yellow] {step.description}")
            if not auto:
                typer.confirm("  Press Enter when done, or skip?", default=True)
            continue

        console.print(f"  [{i}/{len(steps)}] {step.description}")
        console.print(f"         [dim]$ {step.command}[/dim]")

        if step.optional and not auto:
            do_it = typer.confirm("         Run this optional step?", default=True)
            if not do_it:
                console.print("         [dim]Skipped[/dim]")
                continue

        with Progress(SpinnerColumn(), TextColumn("[bold]Running..."), console=console, transient=True) as progress:
            progress.add_task("install", total=None)
            success, output = run_install_command(step)

        if success:
            console.print(f"         [green]Done[/green]")
        else:
            console.print(f"         [red]Failed[/red]")
            if output:
                # Show last few lines of output
                lines = output.strip().splitlines()
                for line in lines[-5:]:
                    console.print(f"         [dim]{line}[/dim]")
            if step.optional:
                console.print("         [dim]Optional step — continuing...[/dim]")
            else:
                failed = True
                console.print("         [red]Required step failed. You may need to run this manually.[/red]")
                if not auto:
                    cont = typer.confirm("         Continue with remaining steps?", default=True)
                    if not cont:
                        break

    # -- Step 8: Verify --
    console.print()
    if not failed:
        # Re-check
        post_info = detect_system()
        if post_info.has_qemu:
            console.print(Panel(
                f"  [green bold]Setup complete![/green bold]\n\n"
                f"  QEMU {post_info.qemu_version} is installed and ready.\n"
                f"  Acceleration: {'KVM' if post_info.has_kvm else 'HVF' if post_info.has_hvf else 'WHPX' if post_info.has_whpx else 'software'}\n\n"
                f"  Get started:\n"
                f"    [cyan]virtualize create my-vm --cpus 2 --memory 2048[/cyan]\n"
                f"    [cyan]virtualize start <vm_id>[/cyan]\n"
                f"    [cyan]virtualize exec <vm_id> 'uname -a'[/cyan]",
                title="[bold green]Ready[/bold green]",
                border_style="green",
                padding=(1, 2),
            ))
        else:
            console.print("[yellow]QEMU still not detected.[/yellow] You may need to restart your shell or add it to PATH.\n")
    else:
        console.print("[yellow]Some steps failed.[/yellow] Review the output above and try running the commands manually.\n")
        raise typer.Exit(code=1)


@app.command()
def doctor():
    """Check system readiness and show diagnostics."""
    from virtualize.core.bootstrap import detect_system

    import shutil

    info = detect_system()

    checks = [
        ("QEMU installed", info.has_qemu, info.qemu_version or "not found"),
        ("Hardware acceleration", info.has_kvm or info.has_hvf or info.has_whpx,
         "KVM" if info.has_kvm else "HVF" if info.has_hvf else "WHPX" if info.has_whpx else "none"),
        ("CPU virtualization extensions", info.cpu_virt_extensions, "VT-x/AMD-V"),
        ("Package manager", info.package_manager.value != "unknown", info.package_manager.value),
        ("Sudo available", info.has_sudo, ""),
        ("qemu-img tool", shutil.which("qemu-img") is not None, shutil.which("qemu-img") or "not found"),
    ]

    console.print("\n[bold]Virtualize Doctor[/bold]\n")
    all_ok = True
    for label, ok, detail in checks:
        icon = "[green]PASS[/green]" if ok else "[red]FAIL[/red]"
        detail_str = f" — {detail}" if detail else ""
        console.print(f"  {icon}  {label}{detail_str}")
        if not ok and label in ("QEMU installed",):
            all_ok = False

    console.print()
    if all_ok:
        console.print("[green]System is ready for Virtualize.[/green]\n")
    else:
        console.print("[yellow]Run [cyan]virtualize setup[/cyan] to install missing dependencies.[/yellow]\n")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------


@app.command()
def version():
    """Show version information."""
    from virtualize import __version__
    console.print(f"Virtualize v{__version__}")
    console.print("Free, cross-platform VM orchestration for AI workflows")


if __name__ == "__main__":
    app()
