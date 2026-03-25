"""Compliance policy definitions and validation.

Maps regulatory requirements to enforceable technical controls.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ComplianceFramework(str, Enum):
    SOC1 = "soc1"
    SOC2 = "soc2"
    SOC3 = "soc3"
    HIPAA = "hipaa"
    ISO27001 = "iso27001"


class PolicyControl(BaseModel):
    """A single enforceable control."""
    id: str
    framework: ComplianceFramework
    title: str
    description: str
    technical_control: str  # what Virtualize enforces
    enabled: bool = True


# Pre-built policy catalog
POLICY_CATALOG: list[PolicyControl] = [
    # --- SOC 1 (ICFR — Internal Control over Financial Reporting) ---
    PolicyControl(
        id="SOC1-CC1.1",
        framework=ComplianceFramework.SOC1,
        title="Control Environment",
        description="Demonstrate commitment to integrity and ethical values in processing",
        technical_control="Immutable audit trail on all VM operations; role-based access enforced",
    ),
    PolicyControl(
        id="SOC1-CC2.1",
        framework=ComplianceFramework.SOC1,
        title="Information and Communication",
        description="Obtain or generate relevant, quality information to support internal control",
        technical_control="Structured audit logs with timestamps, actors, and outcomes; queryable event store",
    ),
    PolicyControl(
        id="SOC1-CC3.1",
        framework=ComplianceFramework.SOC1,
        title="Risk Assessment",
        description="Identify and analyze risks to achieving objectives",
        technical_control="Resource limits on VMs; network isolation modes; timeout enforcement on execution",
    ),
    PolicyControl(
        id="SOC1-CC5.1",
        framework=ComplianceFramework.SOC1,
        title="Monitoring Activities",
        description="Select, develop, and perform ongoing evaluations",
        technical_control="Audit log integrity verification; real-time VM status monitoring; compliance reports",
    ),

    # --- SOC 2 Trust Services Criteria ---
    PolicyControl(
        id="SOC2-CC6.1",
        framework=ComplianceFramework.SOC2,
        title="Logical and Physical Access Controls",
        description="Restrict access to VMs and data to authorized users",
        technical_control="API authentication required; RBAC enforced; SSH key-only access to VMs",
    ),
    PolicyControl(
        id="SOC2-CC6.3",
        framework=ComplianceFramework.SOC2,
        title="Role-Based Access",
        description="Access based on least-privilege roles",
        technical_control="Role-based API tokens; per-VM access grants; no shared credentials",
    ),
    PolicyControl(
        id="SOC2-CC7.2",
        framework=ComplianceFramework.SOC2,
        title="System Monitoring",
        description="Monitor system components for anomalies",
        technical_control="Audit log on all VM operations; integrity-chained logs; resource usage monitoring",
    ),
    PolicyControl(
        id="SOC2-CC8.1",
        framework=ComplianceFramework.SOC2,
        title="Change Management",
        description="Manage changes to infrastructure in a controlled manner",
        technical_control="VM config changes are audited; immutable base images; snapshot before changes",
    ),

    # --- HIPAA ---
    PolicyControl(
        id="HIPAA-164.312-a1",
        framework=ComplianceFramework.HIPAA,
        title="Access Control",
        description="Implement technical policies to allow access only to authorized persons",
        technical_control="Token-based auth; encrypted VM disks; network isolation options",
    ),
    PolicyControl(
        id="HIPAA-164.312-b",
        framework=ComplianceFramework.HIPAA,
        title="Audit Controls",
        description="Implement mechanisms to record and examine activity",
        technical_control="Immutable audit log with HMAC integrity chain; all exec/access logged",
    ),
    PolicyControl(
        id="HIPAA-164.312-c1",
        framework=ComplianceFramework.HIPAA,
        title="Integrity Controls",
        description="Protect ePHI from improper alteration or destruction",
        technical_control="Disk encryption; integrity-verified audit logs; snapshot/restore capabilities",
    ),
    PolicyControl(
        id="HIPAA-164.312-e1",
        framework=ComplianceFramework.HIPAA,
        title="Transmission Security",
        description="Guard against unauthorized access during transmission",
        technical_control="TLS for API; SSH for VM access; encrypted guest agent communication",
    ),

    # --- ISO 27001 ---
    PolicyControl(
        id="ISO27001-A.9.2",
        framework=ComplianceFramework.ISO27001,
        title="User Access Management",
        description="Ensure authorized user access and prevent unauthorized access",
        technical_control="API key management; access review via audit logs; automatic token expiry",
    ),
    PolicyControl(
        id="ISO27001-A.12.4",
        framework=ComplianceFramework.ISO27001,
        title="Logging and Monitoring",
        description="Record events and generate evidence",
        technical_control="Structured JSON audit logs; integrity chain; configurable retention",
    ),
    PolicyControl(
        id="ISO27001-A.13.1",
        framework=ComplianceFramework.ISO27001,
        title="Network Security Management",
        description="Ensure protection of information in networks",
        technical_control="VM network isolation modes (NAT/bridge/isolated); firewall rules per VM",
    ),
    PolicyControl(
        id="ISO27001-A.14.2",
        framework=ComplianceFramework.ISO27001,
        title="Security in Development",
        description="Ensure security is designed into information systems",
        technical_control="Sandboxed code execution; resource limits; timeout enforcement",
    ),

    # --- SOC 3 (General Use Report — public-facing subset of SOC 2) ---
    PolicyControl(
        id="SOC3-TSC-SEC",
        framework=ComplianceFramework.SOC3,
        title="Security Principle",
        description="System is protected against unauthorized access (logical and physical)",
        technical_control="API auth required; VM network isolation; SSH key-only access; encrypted disks",
    ),
    PolicyControl(
        id="SOC3-TSC-AVL",
        framework=ComplianceFramework.SOC3,
        title="Availability Principle",
        description="System is available for operation and use as committed",
        technical_control="VM health monitoring; automatic status tracking; resource limit enforcement",
    ),
    PolicyControl(
        id="SOC3-TSC-PI",
        framework=ComplianceFramework.SOC3,
        title="Processing Integrity Principle",
        description="System processing is complete, valid, accurate, and authorized",
        technical_control="Audit log integrity chain; execution result capture; command timeout enforcement",
    ),
    PolicyControl(
        id="SOC3-TSC-CONF",
        framework=ComplianceFramework.SOC3,
        title="Confidentiality Principle",
        description="Information designated as confidential is protected as committed",
        technical_control="Encrypted audit logs at rest; network isolation modes; VM-level sandboxing",
    ),
]


class ComplianceReport(BaseModel):
    """Compliance posture report."""
    framework: ComplianceFramework
    total_controls: int
    enabled_controls: int
    disabled_controls: int
    controls: list[PolicyControl]
    compliant: bool


def get_controls(framework: ComplianceFramework | None = None) -> list[PolicyControl]:
    """Get all controls, optionally filtered by framework."""
    if framework is None:
        return POLICY_CATALOG
    return [c for c in POLICY_CATALOG if c.framework == framework]


def generate_report(framework: ComplianceFramework) -> ComplianceReport:
    """Generate a compliance posture report for a given framework."""
    controls = get_controls(framework)
    enabled = [c for c in controls if c.enabled]
    disabled = [c for c in controls if not c.enabled]
    return ComplianceReport(
        framework=framework,
        total_controls=len(controls),
        enabled_controls=len(enabled),
        disabled_controls=len(disabled),
        controls=controls,
        compliant=len(disabled) == 0,
    )
