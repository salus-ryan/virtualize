"""Tests for the NL → algebra agent.

Uses a mock LLM to avoid requiring the actual model for CI.
Tests the full pipeline: parse → validate → explain → execute.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from virtualize.agent.nl_agent import NLAgent, AgentResult
from virtualize.core.algebra import (
    IDENTITY_STATE,
    StateType,
    SystemState,
    validate_plan,
)


# ═══════════════════════════════════════════════════════════════════════════
# Mock LLM — returns pre-defined JSON responses
# ═══════════════════════════════════════════════════════════════════════════


def _make_mock_llm(responses: list[str]):
    """Create a mock LLM that returns pre-defined responses in sequence."""
    llm = MagicMock()
    call_count = {"n": 0}

    def chat_completion(messages, **kwargs):
        idx = min(call_count["n"], len(responses) - 1)
        call_count["n"] += 1
        return {
            "choices": [{"message": {"content": responses[idx]}}]
        }

    llm.create_chat_completion = MagicMock(side_effect=chat_completion)
    return llm


# ═══════════════════════════════════════════════════════════════════════════
# §1  Plan generation + validation
# ═══════════════════════════════════════════════════════════════════════════


class TestPlanGeneration:
    def test_create_vm(self):
        llm = _make_mock_llm(['[["vm_create", null, {"name": "dev-box"}]]'])
        agent = NLAgent(llm=llm)
        result = agent.plan("create a vm called dev-box")

        assert result.error is None
        assert len(result.plan) == 1
        assert result.plan[0][0] == "vm_create"
        assert result.plan[0][2]["name"] == "dev-box"
        assert result.validation.valid is True

    def test_full_lifecycle(self):
        plan_json = json.dumps([
            ["vm_create", None, {"name": "test-vm"}],
            ["vm_start", "test-vm", {}],
            ["vm_exec", "test-vm", {"command": "uname -a"}],
            ["vm_stop", "test-vm", {}],
            ["vm_destroy", "test-vm", {}],
        ])
        llm = _make_mock_llm([plan_json])
        agent = NLAgent(llm=llm)
        result = agent.plan("make a vm, start it, run uname, stop and destroy it")

        assert result.error is None
        assert result.validation.valid is True
        assert len(result.plan) == 5
        assert result.validation.final_state.get_vm_state("test-vm") == StateType.VM_DESTROYED

    def test_compliance_report(self):
        llm = _make_mock_llm(['[["compliance_report", null, {"framework": "hipaa"}]]'])
        agent = NLAgent(llm=llm)
        result = agent.plan("check hipaa compliance")

        assert result.error is None
        assert result.validation.valid is True
        assert result.plan[0][0] == "compliance_report"

    def test_sandbox_run(self):
        llm = _make_mock_llm(['[["sandbox_run", null, {"code": "print(42)", "language": "python"}]]'])
        agent = NLAgent(llm=llm)
        result = agent.plan("run print(42) in sandbox")

        assert result.error is None
        assert result.validation.valid is True

    def test_openclaw_scenario(self):
        plan_json = json.dumps([
            ["vm_create", None, {"name": "openclaw-vm"}],
            ["vm_start", "openclaw-vm", {}],
            ["vm_exec", "openclaw-vm", {"command": "pip install openclaw && python -m openclaw"}],
        ])
        llm = _make_mock_llm([plan_json])
        agent = NLAgent(llm=llm)
        result = agent.plan("start me a vm that i can connect to openclaw")

        assert result.error is None
        assert result.validation.valid is True
        assert len(result.plan) == 3
        assert "openclaw" in result.plan[2][2]["command"]


# ═══════════════════════════════════════════════════════════════════════════
# §2  Invalid plan → retry with error feedback
# ═══════════════════════════════════════════════════════════════════════════


class TestRetry:
    def test_retry_on_invalid_plan(self):
        """First attempt is algebraically invalid; second attempt is valid."""
        bad_plan = '[["vm_exec", "ghost", {"command": "echo"}]]'
        good_plan = '[["vm_create", null, {"name": "ghost"}], ["vm_start", "ghost", {}], ["vm_exec", "ghost", {"command": "echo"}]]'

        llm = _make_mock_llm([bad_plan, good_plan])
        agent = NLAgent(llm=llm, max_retries=2)
        result = agent.plan("run echo in a vm")

        assert result.error is None
        assert result.validation.valid is True
        assert len(result.plan) == 3
        # LLM was called twice
        assert llm.create_chat_completion.call_count == 2

    def test_retry_on_bad_json(self):
        """First attempt is not valid JSON; second is."""
        bad = "Sure! Here's what I'd do..."
        good = '[["vm_create", null, {"name": "my-vm"}]]'

        llm = _make_mock_llm([bad, good])
        agent = NLAgent(llm=llm, max_retries=2)
        result = agent.plan("create a vm")

        assert result.error is None
        assert result.validation.valid is True

    def test_all_retries_fail(self):
        """All attempts produce invalid output."""
        bad = "I don't understand"
        llm = _make_mock_llm([bad, bad, bad])
        agent = NLAgent(llm=llm, max_retries=2)
        result = agent.plan("do something impossible")

        assert result.error is not None
        assert "Failed to parse" in result.error


# ═══════════════════════════════════════════════════════════════════════════
# §3  JSON extraction
# ═══════════════════════════════════════════════════════════════════════════


class TestJSONExtraction:
    def setup_method(self):
        self.agent = NLAgent(llm=_make_mock_llm([]))

    def test_clean_json(self):
        plan = self.agent._extract_plan('[["vm_create", null, {"name": "x"}]]')
        assert plan is not None
        assert plan[0][0] == "vm_create"

    def test_json_with_surrounding_text(self):
        raw = 'Here is the plan:\n[["vm_create", null, {"name": "x"}]]\nDone!'
        plan = self.agent._extract_plan(raw)
        assert plan is not None
        assert plan[0][0] == "vm_create"

    def test_invalid_tool_name(self):
        plan = self.agent._extract_plan('[["fake_tool", null, {}]]')
        assert plan is None

    def test_not_a_list(self):
        plan = self.agent._extract_plan('{"key": "value"}')
        assert plan is None

    def test_garbage(self):
        plan = self.agent._extract_plan('hello world')
        assert plan is None

    def test_empty_array(self):
        plan = self.agent._extract_plan('[]')
        assert plan is not None
        assert len(plan) == 0


# ═══════════════════════════════════════════════════════════════════════════
# §4  Plan explanation
# ═══════════════════════════════════════════════════════════════════════════


class TestExplanation:
    def setup_method(self):
        self.agent = NLAgent(llm=_make_mock_llm([]))

    def test_lifecycle_explanation(self):
        plan = [
            ("vm_create", None, {"name": "my-vm"}),
            ("vm_start", "my-vm", {}),
            ("vm_exec", "my-vm", {"command": "echo hello"}),
        ]
        text = self.agent._explain_plan(plan)
        assert "Create VM 'my-vm'" in text
        assert "Start VM" in text
        assert "echo hello" in text

    def test_compliance_explanation(self):
        plan = [("compliance_report", None, {"framework": "soc2"})]
        text = self.agent._explain_plan(plan)
        assert "SOC2" in text

    def test_sandbox_explanation(self):
        plan = [("sandbox_run", None, {"code": "1+1", "language": "python"})]
        text = self.agent._explain_plan(plan)
        assert "sandboxed" in text.lower()


# ═══════════════════════════════════════════════════════════════════════════
# §5  State-aware planning
# ═══════════════════════════════════════════════════════════════════════════


class TestStateAware:
    def test_plan_with_existing_vm(self):
        """If a VM already exists and is running, agent can plan exec directly."""
        llm = _make_mock_llm(['[["vm_exec", "existing-vm", {"command": "ls"}]]'])
        agent = NLAgent(llm=llm)
        state = IDENTITY_STATE.with_vm("existing-vm", StateType.VM_RUNNING)
        result = agent.plan("list files on existing-vm", system_state=state)

        assert result.error is None
        assert result.validation.valid is True

    def test_plan_fails_for_wrong_state(self):
        """Plan to exec on a stopped VM is algebraically invalid."""
        llm = _make_mock_llm([
            '[["vm_exec", "stopped-vm", {"command": "ls"}]]',
            '[["vm_exec", "stopped-vm", {"command": "ls"}]]',
            '[["vm_exec", "stopped-vm", {"command": "ls"}]]',
        ])
        agent = NLAgent(llm=llm, max_retries=2)
        state = IDENTITY_STATE.with_vm("stopped-vm", StateType.VM_STOPPED)
        result = agent.plan("run ls on stopped-vm", system_state=state)

        # All retries should fail because VM is stopped
        assert result.validation is not None
        assert result.validation.valid is False


# Need json import for plan_json construction
import json
