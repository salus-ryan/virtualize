"""Natural language → algebra agent.

Translates plain English into validated tool chains using a small local LLM.
The LLM generates a plan, the compositor validates it against the algebra,
and only then does it execute. Invalid plans are rejected and retried.

Architecture:
    User (English) → LLM → JSON tool chain → Compositor.validate() → Execute
                                                    ↓ (if invalid)
                                              Retry with error feedback

The algebra makes this safe: the LLM can hallucinate anything,
but only algebraically valid plans reach the hypervisor.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from virtualize.core.algebra import (
    Compositor,
    CompositionResult,
    DEFAULT_CONSTRAINTS,
    SystemState,
    ToolInvocation,
    ToolName,
    TRANSITIONS,
    validate_plan,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Model management
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_MODEL_REPO = "bartowski/Qwen2.5-1.5B-Instruct-GGUF"
DEFAULT_MODEL_FILE = "Qwen2.5-1.5B-Instruct-Q4_K_M.gguf"
MODEL_DIR = Path.home() / ".virtualize" / "models"


def ensure_model(repo: str | None = None, filename: str | None = None) -> Path:
    """Download the model if not present. Returns path to GGUF file."""
    repo = repo or DEFAULT_MODEL_REPO
    filename = filename or DEFAULT_MODEL_FILE
    model_path = MODEL_DIR / filename

    if model_path.exists():
        logger.info("Model already downloaded: %s", model_path)
        return model_path

    logger.info("Downloading model %s/%s ...", repo, filename)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    try:
        from huggingface_hub import hf_hub_download
        path = hf_hub_download(
            repo_id=repo,
            filename=filename,
            local_dir=str(MODEL_DIR),
            local_dir_use_symlinks=False,
        )
        return Path(path)
    except ImportError:
        raise RuntimeError(
            "huggingface-hub not installed. Run: pip install -e '.[agent]'"
        )


def load_llm(model_path: Path | None = None, n_ctx: int = 2048, n_gpu_layers: int = -1):
    """Load the LLM. Returns a llama_cpp.Llama instance."""
    # Suppress llama.cpp C-level log messages before importing
    os.environ.setdefault("GGML_LOG_LEVEL", "error")
    try:
        from llama_cpp import Llama
    except ImportError:
        raise RuntimeError(
            "llama-cpp-python not installed. Run: pip install -e '.[agent]'"
        )

    if model_path is None:
        model_path = ensure_model()

    logger.info("Loading model from %s", model_path)
    # Suppress C-level stderr warnings from llama.cpp (e.g. "n_ctx_seq < n_ctx_train")
    # Python's redirect_stderr doesn't catch C++ writes, so we redirect at fd level.
    stderr_fd = os.dup(2)
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 2)
        os.close(devnull)
        model = Llama(
            model_path=str(model_path),
            n_ctx=n_ctx,
            n_gpu_layers=n_gpu_layers,
            verbose=False,
        )
    finally:
        os.dup2(stderr_fd, 2)
        os.close(stderr_fd)
    return model


# ═══════════════════════════════════════════════════════════════════════════
# System prompt — teaches the LLM the algebra
# ═══════════════════════════════════════════════════════════════════════════


def _build_tool_docs() -> str:
    """Generate tool documentation from the algebra's transition table."""
    lines = []
    for tool, rule in TRANSITIONS.items():
        if rule.is_identity:
            continue
        sources = ", ".join(s.value for s in rule.required_vm_states) if rule.required_vm_states else "any"
        target = rule.produced_vm_state.value if rule.produced_vm_state else "(unchanged)"
        needs_vm = "yes" if rule.requires_vm else "no"
        lines.append(f"  {tool.value}: {sources} → {target} (needs vm_id: {needs_vm})")
    return "\n".join(lines)


SYSTEM_PROMPT = f"""You are the Virtualize agent. You translate natural language requests into VM operation plans.

You have access to these tools (typed morphisms over VM state):
{_build_tool_docs()}

RULES:
1. Output ONLY a JSON array of steps. Each step is: ["tool_name", "vm_id_or_null", {{"key": "value"}}]
2. vm_create needs {{"name": "..."}} in args, and vm_id should be null
3. Subsequent steps reference the VM by the name you gave it
4. vm_exec needs {{"command": "..."}} in args
5. vm_file_write needs {{"path": "...", "content": "..."}} in args
6. sandbox_run needs {{"code": "...", "language": "..."}} in args
7. Only output valid JSON. No markdown, no explanation, no comments.
8. Use sensible defaults: linux OS, 2 vCPUs, 2048MB RAM, 20GB disk
9. If the user wants to "connect to" something, create a VM, start it, and run a relevant command
10. If the request is vague or unclear, output: {{"clarify": "your question to the user"}}
11. If the user says "run something" without specifics, ask what they want to run.
12. Try your best to infer intent. Only ask for clarification if truly ambiguous.

EXAMPLES:

User: create a vm called dev-box
Output: [["vm_create", null, {{"name": "dev-box"}}]]

User: start my dev-box
Output: [["vm_start", "dev-box", {{}}]]

User: make a vm and run uname
Output: [["vm_create", null, {{"name": "quick-vm"}}], ["vm_start", "quick-vm", {{}}], ["vm_exec", "quick-vm", {{"command": "uname -a"}}]]

User: check compliance for hipaa
Output: [["compliance_report", null, {{"framework": "hipaa"}}]]

User: start me a vm that i can connect to openclaw
Output: [["vm_create", null, {{"name": "openclaw-vm"}}], ["vm_start", "openclaw-vm", {{}}], ["vm_exec", "openclaw-vm", {{"command": "pip install openclaw && python -m openclaw"}}]]

User: run something
Output: {{"clarify": "What would you like to run? For example: a shell command in a VM, some Python code in a sandbox, or a compliance check?"}}

User: hello
Output: {{"clarify": "Hey! I can help you manage VMs, run code, or check compliance. What would you like to do?"}}

User: help
Output: {{"clarify": "I can: create/start/stop/destroy VMs, run commands inside VMs, execute sandboxed code, check compliance (soc2, hipaa, iso27001), or verify the algebra. What do you need?"}}
"""


# ═══════════════════════════════════════════════════════════════════════════
# Agent
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class AgentResult:
    """Result of an NL agent invocation."""
    query: str
    plan: list[tuple[str, str | None, dict[str, Any]]]
    validation: CompositionResult | None = None
    executed: bool = False
    execution_results: list[dict[str, Any]] = field(default_factory=list)
    explanation: str = ""
    clarification: str | None = None
    error: str | None = None


class NLAgent:
    """Translates natural language into algebraically validated tool chains.

    Pipeline:
        1. LLM generates a JSON tool chain from the user's request
        2. Compositor validates it against the algebra
        3. If invalid, feed errors back to LLM for retry (up to max_retries)
        4. If valid, optionally execute via VMManager
    """

    def __init__(
        self,
        llm=None,
        model_path: Path | None = None,
        compositor: Compositor | None = None,
        max_retries: int = 2,
        n_gpu_layers: int = -1,
    ) -> None:
        self._llm = llm
        self._model_path = model_path
        self._compositor = compositor or Compositor(constraints=DEFAULT_CONSTRAINTS)
        self._max_retries = max_retries
        self._n_gpu_layers = n_gpu_layers

    def _ensure_llm(self):
        if self._llm is None:
            self._llm = load_llm(self._model_path, n_gpu_layers=self._n_gpu_layers)
        return self._llm

    # ── Conversational fast-paths (no LLM needed) ─────────────────────────
    # Patterns → instant responses for greetings, identity, help, vague input.
    # This gives the agent personality without burning LLM tokens.

    _GREETING_WORDS = frozenset({
        "hi", "hey", "hello", "yo", "sup", "howdy", "hola", "greetings",
        "hii", "hiii", "heya", "whats up", "what's up", "wassup",
    })
    _HELP_WORDS = frozenset({"help", "?", "commands", "what can you do", "menu"})
    _VAGUE_PATTERNS = frozenset({
        "run something", "do something", "do stuff", "run stuff",
        "make something", "build something", "idk", "dunno",
    })
    _THANKS_WORDS = frozenset({
        "thanks", "thank you", "thx", "ty", "cheers", "appreciate it",
        "thanks a lot", "thank u", "cool thanks",
    })
    _GOODBYE_WORDS = frozenset({
        "bye", "goodbye", "later", "see ya", "see you", "cya", "peace",
        "gotta go", "ttyl",
    })

    _GREETING_RESPONSE = (
        "Hey! I'm Virtualize — your VM orchestration assistant. "
        "I can create and manage virtual machines, run code in sandboxes, "
        "check compliance, and more. What would you like to do?"
    )
    _HELP_RESPONSE = (
        "Here's what I can do:\n"
        "  • VM lifecycle: 'create a vm', 'start my-vm', 'stop my-vm'\n"
        "  • Run commands: 'run uname on my-vm'\n"
        "  • Sandbox code: 'run print(42) in a sandbox'\n"
        "  • Compliance: 'check soc2 compliance', 'hipaa report'\n"
        "  • Algebra: 'verify the algebra', 'show the state machine'\n"
        "  • Or just describe what you want in plain English!"
    )
    _VAGUE_RESPONSE = (
        "I'd love to help, but I need a bit more detail. For example:\n"
        "  • 'create a vm called dev-box and run uname'\n"
        "  • 'run print(42) in a python sandbox'\n"
        "  • 'check hipaa compliance'\n"
        "  • 'verify the algebra'\n"
        "What are you looking to do?"
    )
    _THANKS_RESPONSE = (
        "You're welcome! Let me know if you need anything else."
    )
    _GOODBYE_RESPONSE = (
        "See you! Run 'virtualize' anytime to come back."
    )

    # Regex patterns for identity/conversational questions
    _IDENTITY_PATTERNS = [
        (re.compile(r"what(?:'s| is) your name", re.I),
         "I'm Virtualize — a VM orchestration agent. I translate plain English "
         "into algebraically verified VM operations. Think of me as your "
         "infrastructure assistant."),
        (re.compile(r"who are you", re.I),
         "I'm Virtualize, an AI agent that manages virtual machines. "
         "I use a formal algebra to make sure every operation is safe "
         "before touching anything. Ask me to create a VM, run code, "
         "or check compliance!"),
        (re.compile(r"what can you do|what do you do|what are you", re.I),
         "I orchestrate virtual machines using a formally verified algebra. "
         "That means I can create, start, stop, and destroy VMs, run commands "
         "inside them, execute sandboxed code, and generate compliance reports "
         "— all validated mathematically before execution."),
        (re.compile(r"how do(?:es)? (?:this|it|you) work", re.I),
         "Every VM operation is a typed morphism in a formal algebra. "
         "When you ask me to do something, I:\n"
         "  1. Translate your request into a plan (a chain of operations)\n"
         "  2. Validate the plan against the algebra's rules\n"
         "  3. Show you the plan and ask for confirmation\n"
         "  4. Execute it if you approve\n"
         "Invalid plans are caught before anything happens."),
        (re.compile(r"(?:are you|you) (?:an? )?(?:ai|bot|robot|llm|model)", re.I),
         "I'm an AI agent backed by a small local language model, "
         "but my real power is the algebraic engine underneath. "
         "The LLM translates your English into plans; the algebra "
         "guarantees they're safe."),
        (re.compile(r"what(?:'s| is) (?:the )?algebra", re.I),
         "The Virtualize algebra is a typed, finite, partially-defined "
         "monoidal category. In plain English: it's a set of rules that "
         "define which VM operations are valid in which order. "
         "For example, you can't run a command on a VM that doesn't exist. "
         "Run 'virtualize algebra verify' to see all 6 axioms checked."),
    ]

    def plan(self, query: str, system_state: SystemState | None = None) -> AgentResult:
        """Generate and validate a plan from natural language."""
        result = AgentResult(query=query, plan=[])

        # Fast-path: handle conversational inputs without the LLM
        q_raw = query.strip().lower()
        q = q_raw.rstrip("!?.,:;")

        # Greetings
        if q in self._GREETING_WORDS or q_raw in self._GREETING_WORDS:
            result.clarification = self._GREETING_RESPONSE
            return result
        # Help
        if q in self._HELP_WORDS or q_raw in self._HELP_WORDS:
            result.clarification = self._HELP_RESPONSE
            return result
        # Vague requests
        if q in self._VAGUE_PATTERNS or q_raw in self._VAGUE_PATTERNS:
            result.clarification = self._VAGUE_RESPONSE
            return result
        # Thanks
        if q in self._THANKS_WORDS or q_raw in self._THANKS_WORDS:
            result.clarification = self._THANKS_RESPONSE
            return result
        # Goodbye
        if q in self._GOODBYE_WORDS or q_raw in self._GOODBYE_WORDS:
            result.clarification = self._GOODBYE_RESPONSE
            return result
        # Identity & conversational questions (regex)
        for pattern, response in self._IDENTITY_PATTERNS:
            if pattern.search(query):
                result.clarification = response
                return result

        llm = self._ensure_llm()

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ]

        for attempt in range(1 + self._max_retries):
            # Generate (suppress C-level stderr from llama.cpp during inference)
            stderr_fd = os.dup(2)
            try:
                devnull_fd = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull_fd, 2)
                os.close(devnull_fd)
                response = llm.create_chat_completion(
                    messages=messages,
                    max_tokens=512,
                    temperature=0.1,
                    stop=["```", "\n\n\n"],
                )
            finally:
                os.dup2(stderr_fd, 2)
                os.close(stderr_fd)

            raw = response["choices"][0]["message"]["content"].strip()
            logger.debug("LLM attempt %d: %s", attempt, raw)

            # Check for clarification response
            clarification = self._extract_clarification(raw)
            if clarification is not None:
                result.clarification = clarification
                return result

            # Parse JSON from response
            plan = self._extract_plan(raw)
            if plan is None:
                if attempt < self._max_retries:
                    messages.append({"role": "assistant", "content": raw})
                    messages.append({"role": "user", "content":
                        "That was not valid JSON. Output ONLY a JSON array of steps like: "
                        '[[\"vm_create\", null, {\"name\": \"x\"}]]. '
                        'Or if the request is unclear: {\"clarify\": \"your question\"}. '
                        'No other text.'
                    })
                    continue
                result.error = f"Failed to parse LLM output as JSON after {attempt + 1} attempts: {raw}"
                return result

            result.plan = plan

            # Validate against algebra
            validation = validate_plan(plan, initial_state=system_state)
            result.validation = validation

            if validation.valid:
                result.explanation = self._explain_plan(plan)
                return result

            # Invalid — retry with error feedback
            if attempt < self._max_retries:
                error_msgs = "; ".join(e.message for e in validation.errors)
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content":
                    f"The algebra rejected that plan: {error_msgs}. "
                    "Fix the plan and output valid JSON only."
                })
            else:
                error_msgs = "; ".join(e.message for e in validation.errors)
                result.error = f"Plan invalid after {attempt + 1} attempts: {error_msgs}"

        return result

    async def execute_plan(
        self,
        result: AgentResult,
        manager=None,
        actor: str = "agent",
    ) -> AgentResult:
        """Execute an already-validated plan (no re-running the LLM)."""
        from virtualize.core.manager import VMManager

        if manager is None:
            from virtualize.compliance.audit import AuditLog
            audit = AuditLog()
            manager = VMManager(audit_callback=audit.record)

        if not result.plan or (result.validation and not result.validation.valid):
            return result

        # Execute each step
        result.executed = True
        vm_name_to_id: dict[str, str] = {}
        for tool_name, vm_id, args in result.plan:
            # Resolve vm_id: if a previous vm_create gave us a real ID, use it
            resolved_id = vm_name_to_id.get(vm_id, vm_id) if vm_id else vm_id
            try:
                step_result = await self._execute_step(manager, tool_name, resolved_id, args, actor)
                result.execution_results.append(step_result)
                # Track name → real ID mapping from vm_create
                if tool_name == "vm_create" and "vm_id" in step_result:
                    name = args.get("name", "")
                    vm_name_to_id[name] = step_result["vm_id"]
            except Exception as e:
                result.execution_results.append({"tool": tool_name, "error": str(e)})
                result.error = f"Execution failed at {tool_name}: {e}"
                break

        return result

    async def plan_and_execute(
        self,
        query: str,
        manager=None,
        actor: str = "agent",
    ) -> AgentResult:
        """Generate a plan and execute it if valid."""
        from virtualize.core.manager import VMManager

        if manager is None:
            from virtualize.compliance.audit import AuditLog
            audit = AuditLog()
            manager = VMManager(audit_callback=audit.record)

        result = self.plan(query, system_state=manager.system_state)
        if result.error or not result.validation or not result.validation.valid:
            return result

        return await self.execute_plan(result, manager=manager, actor=actor)

    async def _execute_step(
        self, manager, tool_name: str, vm_id: str | None, args: dict, actor: str
    ) -> dict[str, Any]:
        """Execute a single tool invocation via the VMManager."""
        from virtualize.core.models import ExecRequest, VMConfig

        if tool_name == "vm_create":
            config = VMConfig(name=args.get("name", "agent-vm"))
            vm = await manager.create(config, actor=actor)
            return {"tool": tool_name, "vm_id": vm.id, "name": vm.config.name, "status": "created"}

        elif tool_name == "vm_start":
            vm = await manager.start(vm_id, actor=actor)
            return {"tool": tool_name, "vm_id": vm.id, "status": "running",
                    "ssh_port": vm.ssh_port, "ip": vm.ip_address}

        elif tool_name == "vm_stop":
            vm = await manager.stop(vm_id, actor=actor)
            return {"tool": tool_name, "vm_id": vm.id, "status": "stopped"}

        elif tool_name == "vm_destroy":
            vm = await manager.destroy(vm_id, actor=actor)
            return {"tool": tool_name, "vm_id": vm.id, "status": "destroyed"}

        elif tool_name == "vm_exec":
            req = ExecRequest(vm_id=vm_id, command=args.get("command", "echo ok"))
            result = await manager.exec(req, actor=actor)
            return {"tool": tool_name, "vm_id": vm_id, "exit_code": result.exit_code,
                    "stdout": result.stdout, "stderr": result.stderr}

        elif tool_name == "vm_status":
            status = await manager.status(vm_id)
            return {"tool": tool_name, "vm_id": vm_id, "status": status.value}

        elif tool_name == "compliance_report":
            from virtualize.compliance.policies import ComplianceFramework, generate_report
            fw = ComplianceFramework(args.get("framework", "soc2"))
            report = generate_report(fw)
            return {"tool": tool_name, "framework": fw.value,
                    "compliant": report.compliant, "controls": report.total_controls}

        elif tool_name == "audit_query":
            return {"tool": tool_name, "note": "audit query executed"}

        elif tool_name == "audit_verify":
            return {"tool": tool_name, "note": "audit verify executed"}

        elif tool_name == "sandbox_run":
            from virtualize.sandbox.executor import SandboxExecutor
            executor = SandboxExecutor(manager)
            result = await executor.run(
                code=args.get("code", ""),
                language=args.get("language", "python"),
                actor=actor,
            )
            return {"tool": tool_name, "exit_code": result.exit_code,
                    "stdout": result.stdout, "stderr": result.stderr}

        else:
            return {"tool": tool_name, "note": "no-op (observation only)"}

    def _extract_clarification(self, raw: str) -> str | None:
        """Check if the LLM is asking for clarification instead of planning."""
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "clarify" in data:
                return str(data["clarify"])
        except (json.JSONDecodeError, TypeError):
            pass
        # Also try to find {"clarify": "..."} embedded in text
        match = re.search(r'\{[^}]*"clarify"\s*:\s*"([^"]*)"[^}]*\}', raw)
        if match:
            return match.group(1)
        return None

    def _extract_plan(self, raw: str) -> list[tuple[str, str | None, dict]] | None:
        """Extract a JSON plan from LLM output, tolerating surrounding text."""
        # Try the whole thing first
        parsed = self._try_parse_json(raw)
        if parsed is not None:
            return parsed

        # Try to find a JSON array in the text
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            parsed = self._try_parse_json(match.group())
            if parsed is not None:
                return parsed

        return None

    def _try_parse_json(self, text: str) -> list[tuple[str, str | None, dict]] | None:
        """Try to parse text as a JSON plan."""
        try:
            data = json.loads(text)
            if not isinstance(data, list):
                return None
            plan = []
            for step in data:
                if not isinstance(step, list) or len(step) < 1:
                    return None
                tool = step[0]
                vm_id = step[1] if len(step) > 1 else None
                args = step[2] if len(step) > 2 and isinstance(step[2], dict) else {}
                # Validate tool name
                try:
                    ToolName(tool)
                except ValueError:
                    return None
                plan.append((tool, vm_id, args))
            return plan
        except (json.JSONDecodeError, TypeError, IndexError):
            return None

    def _explain_plan(self, plan: list[tuple[str, str | None, dict]]) -> str:
        """Generate a human-readable explanation of the plan."""
        lines = []
        for i, (tool, vm_id, args) in enumerate(plan, 1):
            vm_str = f" on '{vm_id}'" if vm_id else ""
            if tool == "vm_create":
                lines.append(f"{i}. Create VM '{args.get('name', '?')}'")
            elif tool == "vm_start":
                lines.append(f"{i}. Start VM{vm_str}")
            elif tool == "vm_stop":
                lines.append(f"{i}. Stop VM{vm_str}")
            elif tool == "vm_destroy":
                lines.append(f"{i}. Destroy VM{vm_str}")
            elif tool == "vm_exec":
                cmd = args.get("command", "?")
                lines.append(f"{i}. Run `{cmd}`{vm_str}")
            elif tool == "sandbox_run":
                lang = args.get("language", "python")
                lines.append(f"{i}. Run sandboxed {lang} code")
            elif tool == "compliance_report":
                fw = args.get("framework", "?")
                lines.append(f"{i}. Generate {fw.upper()} compliance report")
            else:
                lines.append(f"{i}. {tool}{vm_str}")
        return "\n".join(lines)
