"""OS-detecting bootstrap and setup system.

Detects the user's OS, distro, package manager, and hardware capabilities,
then provides interactive guided setup to install QEMU and dependencies.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class OS(str, Enum):
    LINUX = "linux"
    MACOS = "macos"
    WINDOWS = "windows"
    UNKNOWN = "unknown"


class PackageManager(str, Enum):
    APT = "apt"
    DNF = "dnf"
    YUM = "yum"
    PACMAN = "pacman"
    ZYPPER = "zypper"
    APK = "apk"
    BREW = "brew"
    CHOCO = "choco"
    WINGET = "winget"
    SCOOP = "scoop"
    UNKNOWN = "unknown"


@dataclass
class SystemInfo:
    """Detected system information."""
    os: OS
    os_name: str  # e.g. "Ubuntu 24.04", "macOS Sonoma", "Windows 11"
    arch: str  # e.g. "x86_64", "aarch64", "arm64"
    distro: str  # Linux distro ID e.g. "ubuntu", "fedora", "arch"
    distro_version: str
    package_manager: PackageManager
    has_sudo: bool
    has_qemu: bool
    qemu_version: str | None
    has_kvm: bool
    has_hvf: bool  # macOS Hypervisor.framework
    has_whpx: bool  # Windows Hypervisor Platform
    cpu_virt_extensions: bool  # VT-x / AMD-V
    gpu_devices: list[str] = field(default_factory=list)
    missing_deps: list[str] = field(default_factory=list)


def detect_system() -> SystemInfo:
    """Detect everything about the current system."""
    system = platform.system().lower()
    arch = platform.machine()

    os_type = {
        "linux": OS.LINUX,
        "darwin": OS.MACOS,
        "windows": OS.WINDOWS,
    }.get(system, OS.UNKNOWN)

    os_name = _detect_os_name(os_type)
    distro, distro_version = _detect_distro(os_type)
    pkg_mgr = _detect_package_manager(os_type, distro)
    has_sudo = _check_sudo(os_type)
    has_qemu, qemu_version = _check_qemu()
    has_kvm = _check_kvm(os_type)
    has_hvf = _check_hvf(os_type)
    has_whpx = _check_whpx(os_type)
    cpu_virt = _check_cpu_virt(os_type)
    gpus = _detect_gpus(os_type)

    missing = _find_missing_deps(os_type, has_qemu)

    return SystemInfo(
        os=os_type,
        os_name=os_name,
        arch=arch,
        distro=distro,
        distro_version=distro_version,
        package_manager=pkg_mgr,
        has_sudo=has_sudo,
        has_qemu=has_qemu,
        qemu_version=qemu_version,
        has_kvm=has_kvm,
        has_hvf=has_hvf,
        has_whpx=has_whpx,
        cpu_virt_extensions=cpu_virt,
        gpu_devices=gpus,
        missing_deps=missing,
    )


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    """Run a command and return (returncode, stdout)."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return -1, ""


def _detect_os_name(os_type: OS) -> str:
    if os_type == OS.LINUX:
        rc, out = _run(["lsb_release", "-ds"])
        if rc == 0 and out:
            return out.strip('"')
        # Fallback to /etc/os-release
        try:
            lines = Path("/etc/os-release").read_text().splitlines()
            for line in lines:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip('"')
        except FileNotFoundError:
            pass
        return "Linux"
    elif os_type == OS.MACOS:
        rc, out = _run(["sw_vers", "-productVersion"])
        if rc == 0:
            return f"macOS {out}"
        return "macOS"
    elif os_type == OS.WINDOWS:
        return platform.platform()
    return "Unknown"


def _detect_distro(os_type: OS) -> tuple[str, str]:
    if os_type != OS.LINUX:
        return ("", "")
    try:
        lines = Path("/etc/os-release").read_text().splitlines()
        info: dict[str, str] = {}
        for line in lines:
            if "=" in line:
                key, val = line.split("=", 1)
                info[key] = val.strip('"')
        return info.get("ID", "unknown"), info.get("VERSION_ID", "")
    except FileNotFoundError:
        return ("unknown", "")


def _detect_package_manager(os_type: OS, distro: str) -> PackageManager:
    if os_type == OS.MACOS:
        if shutil.which("brew"):
            return PackageManager.BREW
        return PackageManager.UNKNOWN
    elif os_type == OS.WINDOWS:
        if shutil.which("winget"):
            return PackageManager.WINGET
        if shutil.which("choco"):
            return PackageManager.CHOCO
        if shutil.which("scoop"):
            return PackageManager.SCOOP
        return PackageManager.UNKNOWN
    elif os_type == OS.LINUX:
        # Check by distro first, then fall back to binary detection
        distro_map = {
            "ubuntu": PackageManager.APT,
            "debian": PackageManager.APT,
            "linuxmint": PackageManager.APT,
            "pop": PackageManager.APT,
            "elementary": PackageManager.APT,
            "zorin": PackageManager.APT,
            "kali": PackageManager.APT,
            "fedora": PackageManager.DNF,
            "rhel": PackageManager.DNF,
            "centos": PackageManager.DNF,
            "rocky": PackageManager.DNF,
            "alma": PackageManager.DNF,
            "ol": PackageManager.YUM,
            "arch": PackageManager.PACMAN,
            "manjaro": PackageManager.PACMAN,
            "endeavouros": PackageManager.PACMAN,
            "opensuse": PackageManager.ZYPPER,
            "sles": PackageManager.ZYPPER,
            "alpine": PackageManager.APK,
        }
        if distro in distro_map:
            return distro_map[distro]
        # Binary fallback
        for mgr, binary in [
            (PackageManager.APT, "apt-get"),
            (PackageManager.DNF, "dnf"),
            (PackageManager.YUM, "yum"),
            (PackageManager.PACMAN, "pacman"),
            (PackageManager.ZYPPER, "zypper"),
            (PackageManager.APK, "apk"),
        ]:
            if shutil.which(binary):
                return mgr
    return PackageManager.UNKNOWN


def _check_sudo(os_type: OS) -> bool:
    if os_type == OS.WINDOWS:
        return False
    if os.geteuid() == 0:
        return True
    return shutil.which("sudo") is not None


def _check_qemu() -> tuple[bool, str | None]:
    qemu_bin = shutil.which("qemu-system-x86_64") or shutil.which("qemu-system-aarch64")
    if not qemu_bin:
        return False, None
    rc, out = _run([qemu_bin, "--version"])
    if rc == 0:
        # Extract version from "QEMU emulator version X.Y.Z"
        for part in out.split():
            if part[0].isdigit():
                return True, part
    return True, None


def _check_kvm(os_type: OS) -> bool:
    if os_type != OS.LINUX:
        return False
    return Path("/dev/kvm").exists()


def _check_hvf(os_type: OS) -> bool:
    if os_type != OS.MACOS:
        return False
    rc, out = _run(["sysctl", "-n", "kern.hv_support"])
    return rc == 0 and out.strip() == "1"


def _check_whpx(os_type: OS) -> bool:
    if os_type != OS.WINDOWS:
        return False
    # Check if Hyper-V / Windows Hypervisor Platform is enabled
    rc, out = _run(["powershell", "-Command",
                     "(Get-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform).State"])
    return rc == 0 and "Enabled" in out


def _check_cpu_virt(os_type: OS) -> bool:
    if os_type == OS.LINUX:
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text()
            return "vmx" in cpuinfo or "svm" in cpuinfo
        except FileNotFoundError:
            return False
    elif os_type == OS.MACOS:
        rc, out = _run(["sysctl", "-n", "machdep.cpu.features"])
        return rc == 0 and "VMX" in out.upper()
    return False


def _detect_gpus(os_type: OS) -> list[str]:
    gpus: list[str] = []
    if os_type == OS.LINUX:
        rc, out = _run(["lspci", "-nn"])
        if rc == 0:
            for line in out.splitlines():
                lower = line.lower()
                if "vga" in lower or "3d" in lower or "display" in lower:
                    gpus.append(line.strip())
    elif os_type == OS.MACOS:
        rc, out = _run(["system_profiler", "SPDisplaysDataType"])
        if rc == 0:
            for line in out.splitlines():
                if "Chipset Model" in line:
                    gpus.append(line.split(":", 1)[-1].strip())
    return gpus


def _find_missing_deps(os_type: OS, has_qemu: bool) -> list[str]:
    missing: list[str] = []
    if not has_qemu:
        missing.append("qemu")
    if os_type == OS.LINUX:
        if not shutil.which("qemu-img"):
            missing.append("qemu-img")
    return missing


# ---------------------------------------------------------------------------
# Install commands per OS / package manager
# ---------------------------------------------------------------------------


@dataclass
class InstallStep:
    """A single step in the setup process."""
    description: str
    command: str | None  # None = manual step
    requires_sudo: bool = False
    optional: bool = False


def get_install_steps(info: SystemInfo) -> list[InstallStep]:
    """Generate the correct install steps for the detected system."""
    steps: list[InstallStep] = []

    if info.has_qemu:
        return steps  # Nothing to install

    # -- QEMU install --
    if info.os == OS.LINUX:
        steps.extend(_linux_install_steps(info))
    elif info.os == OS.MACOS:
        steps.extend(_macos_install_steps(info))
    elif info.os == OS.WINDOWS:
        steps.extend(_windows_install_steps(info))
    else:
        steps.append(InstallStep(
            description="Download QEMU from https://www.qemu.org/download/",
            command=None,
        ))

    # -- KVM / accelerator setup --
    if info.os == OS.LINUX and not info.has_kvm and info.cpu_virt_extensions:
        steps.append(InstallStep(
            description="Load KVM kernel module",
            command="sudo modprobe kvm && sudo modprobe kvm_intel || sudo modprobe kvm_amd",
            requires_sudo=True,
            optional=True,
        ))
        steps.append(InstallStep(
            description="Add your user to the kvm group for hardware acceleration",
            command=f"sudo usermod -aG kvm {os.environ.get('USER', 'your-user')}",
            requires_sudo=True,
        ))

    return steps


def _linux_install_steps(info: SystemInfo) -> list[InstallStep]:
    steps: list[InstallStep] = []

    if info.package_manager == PackageManager.APT:
        steps.append(InstallStep(
            description="Update package lists",
            command="sudo apt-get update",
            requires_sudo=True,
        ))
        steps.append(InstallStep(
            description="Install QEMU, KVM, and utilities",
            command="sudo apt-get install -y qemu-system-x86 qemu-utils qemu-block-extra ovmf",
            requires_sudo=True,
        ))

    elif info.package_manager == PackageManager.DNF:
        steps.append(InstallStep(
            description="Install QEMU and KVM",
            command="sudo dnf install -y qemu-kvm qemu-img",
            requires_sudo=True,
        ))

    elif info.package_manager == PackageManager.YUM:
        steps.append(InstallStep(
            description="Install QEMU and KVM",
            command="sudo yum install -y qemu-kvm qemu-img",
            requires_sudo=True,
        ))

    elif info.package_manager == PackageManager.PACMAN:
        steps.append(InstallStep(
            description="Install QEMU",
            command="sudo pacman -S --noconfirm qemu-full",
            requires_sudo=True,
        ))

    elif info.package_manager == PackageManager.ZYPPER:
        steps.append(InstallStep(
            description="Install QEMU and KVM",
            command="sudo zypper install -y qemu-kvm qemu-tools",
            requires_sudo=True,
        ))

    elif info.package_manager == PackageManager.APK:
        steps.append(InstallStep(
            description="Install QEMU",
            command="sudo apk add qemu-system-x86_64 qemu-img",
            requires_sudo=True,
        ))

    else:
        steps.append(InstallStep(
            description="Install QEMU using your package manager (could not auto-detect). Visit https://www.qemu.org/download/",
            command=None,
        ))

    return steps


def _macos_install_steps(info: SystemInfo) -> list[InstallStep]:
    steps: list[InstallStep] = []

    if info.package_manager == PackageManager.BREW:
        steps.append(InstallStep(
            description="Install QEMU via Homebrew",
            command="brew install qemu",
        ))
    else:
        steps.append(InstallStep(
            description="Install Homebrew first (required for QEMU on macOS)",
            command='/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"',
        ))
        steps.append(InstallStep(
            description="Install QEMU via Homebrew",
            command="brew install qemu",
        ))

    return steps


def _windows_install_steps(info: SystemInfo) -> list[InstallStep]:
    steps: list[InstallStep] = []

    if info.package_manager == PackageManager.WINGET:
        steps.append(InstallStep(
            description="Install QEMU via winget",
            command="winget install --id SoftwareFreedomConservancy.QEMU -e",
        ))
    elif info.package_manager == PackageManager.CHOCO:
        steps.append(InstallStep(
            description="Install QEMU via Chocolatey",
            command="choco install qemu -y",
        ))
    elif info.package_manager == PackageManager.SCOOP:
        steps.append(InstallStep(
            description="Install QEMU via Scoop",
            command="scoop install qemu",
        ))
    else:
        steps.append(InstallStep(
            description="Download QEMU installer from https://qemu.weilnetz.de/w64/",
            command=None,
        ))

    if not info.has_whpx:
        steps.append(InstallStep(
            description="Enable Windows Hypervisor Platform for acceleration",
            command='powershell -Command "Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -NoRestart"',
            requires_sudo=True,
            optional=True,
        ))

    return steps


def run_install_command(step: InstallStep) -> tuple[bool, str]:
    """Execute an install step. Returns (success, output)."""
    if step.command is None:
        return False, "Manual step — no command to run"

    try:
        result = subprocess.run(
            step.command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = result.stdout + result.stderr
        return result.returncode == 0, output.strip()
    except subprocess.TimeoutExpired:
        return False, "Command timed out after 5 minutes"
    except Exception as e:
        return False, str(e)
