"""Cross-platform hypervisor abstraction layer.

Supports:
  - Linux:   QEMU/KVM
  - macOS:   QEMU with Hypervisor.framework (hvf)
  - Windows: QEMU with WHPX / Hyper-V
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import os
import platform
import shutil
import signal
import tempfile
from pathlib import Path
from typing import Any

from virtualize.core.models import (
    DiskFormat,
    GPUConfig,
    GPUMode,
    NetworkConfig,
    NetworkMode,
    VMConfig,
    VMInstance,
    VMStatus,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data directory
# ---------------------------------------------------------------------------

DEFAULT_DATA_DIR = Path.home() / ".virtualize"
VM_DIR = DEFAULT_DATA_DIR / "vms"
IMAGE_DIR = DEFAULT_DATA_DIR / "images"


def ensure_dirs() -> None:
    for d in (DEFAULT_DATA_DIR, VM_DIR, IMAGE_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class Hypervisor(abc.ABC):
    """Abstract hypervisor backend."""

    name: str

    @abc.abstractmethod
    async def create_disk(self, instance: VMInstance) -> Path:
        """Create or locate the VM disk image. Returns path."""

    @abc.abstractmethod
    async def start(self, instance: VMInstance) -> VMInstance:
        """Boot the VM. Returns updated instance with pid, ports, etc."""

    @abc.abstractmethod
    async def stop(self, instance: VMInstance, force: bool = False) -> VMInstance:
        """Gracefully (or forcefully) stop the VM."""

    @abc.abstractmethod
    async def destroy(self, instance: VMInstance) -> VMInstance:
        """Stop + remove all artifacts."""

    @abc.abstractmethod
    async def status(self, instance: VMInstance) -> VMStatus:
        """Query live status."""

    @abc.abstractmethod
    async def exec_command(self, instance: VMInstance, command: str, timeout: int = 60) -> tuple[int, str, str]:
        """Run a command inside the VM via guest agent / SSH. Returns (exit_code, stdout, stderr)."""

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Check if this hypervisor backend is usable on the current system."""


# ---------------------------------------------------------------------------
# QEMU backend (cross-platform, primary backend)
# ---------------------------------------------------------------------------


class QEMUHypervisor(Hypervisor):
    """QEMU-based hypervisor with platform-specific acceleration."""

    name = "qemu"

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or VM_DIR
        ensure_dirs()
        self._system = platform.system().lower()

    # -- helpers --

    def _qemu_binary(self, arch: str = "x86_64") -> str:
        return f"qemu-system-{arch}"

    def _accel_flag(self) -> str:
        if self._system == "linux":
            if Path("/dev/kvm").exists():
                return "kvm"
            return "tcg"
        elif self._system == "darwin":
            return "hvf"
        elif self._system == "windows":
            return "whpx"
        return "tcg"

    def _vm_dir(self, instance: VMInstance) -> Path:
        p = self.data_dir / instance.id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _disk_path(self, instance: VMInstance) -> Path:
        ext = instance.config.disk.format.value
        return self._vm_dir(instance) / f"disk.{ext}"

    def _pid_file(self, instance: VMInstance) -> Path:
        return self._vm_dir(instance) / "qemu.pid"

    def _monitor_socket(self, instance: VMInstance) -> Path:
        return self._vm_dir(instance) / "monitor.sock"

    def _guest_agent_socket(self, instance: VMInstance) -> Path:
        return self._vm_dir(instance) / "agent.sock"

    def _state_file(self, instance: VMInstance) -> Path:
        return self._vm_dir(instance) / "state.json"

    def _save_state(self, instance: VMInstance) -> None:
        self._state_file(instance).write_text(instance.model_dump_json(indent=2))

    def _find_free_port(self) -> int:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    # -- GPU flags --

    def _gpu_flags(self, gpu: GPUConfig) -> list[str]:
        if gpu.mode == GPUMode.NONE:
            return ["-display", "none"]
        if gpu.mode == GPUMode.VIRTUAL:
            return ["-device", "virtio-vga", "-display", "none"]
        if gpu.mode == GPUMode.PASSTHROUGH:
            if not gpu.device_id:
                raise ValueError("GPU passthrough requires device_id (PCI address)")
            return [
                "-device", f"vfio-pci,host={gpu.device_id}",
                "-display", "none",
            ]
        return ["-display", "none"]

    # -- Network flags --

    def _net_flags(self, net: NetworkConfig, ssh_port: int) -> list[str]:
        flags: list[str] = []
        if net.mode == NetworkMode.NAT:
            hostfwd = f"hostfwd=tcp::{ssh_port}-:22"
            for host_port, guest_port in net.ports.items():
                hostfwd += f",hostfwd=tcp::{host_port}-:{guest_port}"
            flags += ["-netdev", f"user,id=net0,{hostfwd}", "-device", "virtio-net-pci,netdev=net0"]
        elif net.mode == NetworkMode.BRIDGE:
            flags += ["-netdev", "bridge,id=net0,br=virbr0", "-device", "virtio-net-pci,netdev=net0"]
        elif net.mode == NetworkMode.ISOLATED:
            flags += ["-nic", "none"]
        elif net.mode == NetworkMode.HOST:
            flags += ["-netdev", "user,id=net0", "-device", "virtio-net-pci,netdev=net0"]
        if net.mac_address:
            flags += ["-device", f"virtio-net-pci,mac={net.mac_address}"]
        return flags

    # -- cloud-init --

    async def _create_cloud_init_iso(self, instance: VMInstance) -> Path | None:
        ci = instance.config.cloud_init
        if not ci:
            return None
        vm_dir = self._vm_dir(instance)
        ci_dir = vm_dir / "cloud-init"
        ci_dir.mkdir(exist_ok=True)

        meta_data = ci.get("meta_data", {"instance-id": instance.id, "local-hostname": instance.config.name})
        user_data = ci.get("user_data", {})

        (ci_dir / "meta-data").write_text(json.dumps(meta_data))

        if isinstance(user_data, dict):
            import yaml  # optional dep
            (ci_dir / "user-data").write_text("#cloud-config\n" + yaml.dump(user_data))
        else:
            (ci_dir / "user-data").write_text(str(user_data))

        iso_path = vm_dir / "cloud-init.iso"
        # Use genisoimage / mkisofs / xorriso depending on platform
        for tool in ("genisoimage", "mkisofs", "xorriso"):
            if shutil.which(tool):
                if tool == "xorriso":
                    cmd = [tool, "-as", "genisoimage", "-output", str(iso_path),
                           "-volid", "cidata", "-joliet", "-rock", str(ci_dir)]
                else:
                    cmd = [tool, "-output", str(iso_path), "-volid", "cidata",
                           "-joliet", "-rock", str(ci_dir)]
                proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                await proc.communicate()
                if proc.returncode == 0:
                    return iso_path
                break
        logger.warning("cloud-init ISO creation failed — no suitable tool found")
        return None

    # -- interface implementation --

    def is_available(self) -> bool:
        return shutil.which(self._qemu_binary()) is not None

    async def create_disk(self, instance: VMInstance) -> Path:
        disk_path = self._disk_path(instance)
        if disk_path.exists():
            return disk_path

        cfg = instance.config.disk

        if instance.config.base_image:
            base = Path(instance.config.base_image)
            if not base.exists():
                base = IMAGE_DIR / instance.config.base_image
            if base.exists():
                proc = await asyncio.create_subprocess_exec(
                    "qemu-img", "create", "-f", cfg.format.value,
                    "-b", str(base), "-F", cfg.format.value,
                    str(disk_path), f"{cfg.size_gb}G",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0:
                    raise RuntimeError(f"qemu-img backing file failed: {stderr.decode()}")
                return disk_path

        proc = await asyncio.create_subprocess_exec(
            "qemu-img", "create", "-f", cfg.format.value,
            str(disk_path), f"{cfg.size_gb}G",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"qemu-img create failed: {stderr.decode()}")
        return disk_path

    async def start(self, instance: VMInstance) -> VMInstance:
        if not self.is_available():
            raise RuntimeError("QEMU is not installed or not in PATH")

        disk_path = await self.create_disk(instance)
        ssh_port = self._find_free_port()
        vnc_port = self._find_free_port()
        cfg = instance.config

        cmd: list[str] = [
            self._qemu_binary(),
            "-name", cfg.name,
            "-m", str(cfg.memory_mb),
            "-smp", str(cfg.vcpus),
            "-accel", self._accel_flag(),
            "-drive", f"file={disk_path},format={cfg.disk.format.value},if=virtio",
            "-pidfile", str(self._pid_file(instance)),
            "-monitor", f"unix:{self._monitor_socket(instance)},server,nowait",
            "-chardev", f"socket,path={self._guest_agent_socket(instance)},server=on,wait=off,id=qga0",
            "-device", "virtio-serial",
            "-device", "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
            "-daemonize",
        ]

        # Boot media
        if cfg.iso_path:
            cmd += ["-cdrom", cfg.iso_path]

        # Cloud-init
        ci_iso = await self._create_cloud_init_iso(instance)
        if ci_iso:
            cmd += ["-drive", f"file={ci_iso},format=raw,if=virtio"]

        # Network
        cmd += self._net_flags(cfg.network, ssh_port)

        # GPU
        cmd += self._gpu_flags(cfg.gpu)

        # Resource limits via cgroups on Linux
        wrapper: list[str] = []
        if self._system == "linux" and cfg.resource_limits.max_cpu_percent < 100:
            cpu_quota = int(cfg.resource_limits.max_cpu_percent * 1000)
            wrapper = ["systemd-run", "--scope", f"-p", f"CPUQuota={cpu_quota}%", "--"]

        full_cmd = wrapper + cmd
        logger.info("Starting VM %s: %s", instance.id, " ".join(full_cmd))

        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            instance.status = VMStatus.ERROR
            instance.error = stderr.decode().strip()
            self._save_state(instance)
            raise RuntimeError(f"QEMU start failed: {instance.error}")

        # Read PID
        pid_file = self._pid_file(instance)
        for _ in range(10):
            if pid_file.exists():
                try:
                    instance.pid = int(pid_file.read_text().strip())
                    break
                except ValueError:
                    pass
            await asyncio.sleep(0.3)

        from datetime import datetime, timezone
        instance.status = VMStatus.RUNNING
        instance.started_at = datetime.now(timezone.utc)
        instance.ssh_port = ssh_port
        instance.vnc_port = vnc_port
        instance.ip_address = "127.0.0.1"
        instance.hypervisor = self.name
        self._save_state(instance)
        return instance

    async def stop(self, instance: VMInstance, force: bool = False) -> VMInstance:
        instance.status = VMStatus.STOPPING
        if instance.pid:
            try:
                sig = signal.SIGKILL if force else signal.SIGTERM
                os.kill(instance.pid, sig)
                # Wait for process to exit
                for _ in range(30):
                    try:
                        os.kill(instance.pid, 0)
                        await asyncio.sleep(0.5)
                    except OSError:
                        break
            except OSError:
                pass
        from datetime import datetime, timezone
        instance.status = VMStatus.STOPPED
        instance.stopped_at = datetime.now(timezone.utc)
        instance.pid = None
        self._save_state(instance)
        return instance

    async def destroy(self, instance: VMInstance) -> VMInstance:
        if instance.status == VMStatus.RUNNING:
            await self.stop(instance, force=True)
        vm_dir = self._vm_dir(instance)
        if vm_dir.exists():
            shutil.rmtree(vm_dir)
        instance.status = VMStatus.DESTROYED
        return instance

    async def status(self, instance: VMInstance) -> VMStatus:
        if instance.pid:
            try:
                os.kill(instance.pid, 0)
                return VMStatus.RUNNING
            except OSError:
                return VMStatus.STOPPED
        return VMStatus.STOPPED

    async def exec_command(self, instance: VMInstance, command: str, timeout: int = 60) -> tuple[int, str, str]:
        """Execute a command in the VM via SSH (primary) or QEMU Guest Agent (fallback)."""
        if instance.status != VMStatus.RUNNING:
            raise RuntimeError(f"VM {instance.id} is not running (status={instance.status})")

        # Try SSH first
        if instance.ssh_port and instance.ip_address:
            return await self._exec_via_ssh(instance, command, timeout)

        # Fallback: QEMU Guest Agent
        return await self._exec_via_guest_agent(instance, command, timeout)

    async def _exec_via_ssh(self, instance: VMInstance, command: str, timeout: int) -> tuple[int, str, str]:
        ssh_cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", f"ConnectTimeout={min(timeout, 10)}",
            "-p", str(instance.ssh_port),
            f"root@{instance.ip_address}",
            command,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode or 0, stdout.decode(), stderr.decode()
        except asyncio.TimeoutError:
            return -1, "", "Command timed out"

    async def _exec_via_guest_agent(self, instance: VMInstance, command: str, timeout: int) -> tuple[int, str, str]:
        sock_path = self._guest_agent_socket(instance)
        if not sock_path.exists():
            raise RuntimeError("Guest agent socket not available")

        import base64

        ga_exec = json.dumps({
            "execute": "guest-exec",
            "arguments": {
                "path": "/bin/sh",
                "arg": ["-c", command],
                "capture-output": True,
            }
        })

        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        writer.write((ga_exec + "\n").encode())
        await writer.drain()

        resp_raw = await asyncio.wait_for(reader.readline(), timeout=10)
        resp = json.loads(resp_raw)
        pid = resp.get("return", {}).get("pid")
        if pid is None:
            writer.close()
            return -1, "", "Failed to start process via guest agent"

        # Poll for completion
        for _ in range(timeout * 2):
            status_cmd = json.dumps({"execute": "guest-exec-status", "arguments": {"pid": pid}})
            writer.write((status_cmd + "\n").encode())
            await writer.drain()
            status_raw = await asyncio.wait_for(reader.readline(), timeout=10)
            status_resp = json.loads(status_raw).get("return", {})
            if status_resp.get("exited"):
                stdout = base64.b64decode(status_resp.get("out-data", "")).decode(errors="replace")
                stderr = base64.b64decode(status_resp.get("err-data", "")).decode(errors="replace")
                writer.close()
                return status_resp.get("exitcode", -1), stdout, stderr
            await asyncio.sleep(0.5)

        writer.close()
        return -1, "", "Command timed out via guest agent"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def detect_hypervisor(data_dir: Path | None = None, allow_mock: bool = True) -> Hypervisor:
    """Auto-detect the best available hypervisor for this platform.

    If no real hypervisor is found and allow_mock is True, falls back to a
    mock hypervisor suitable for development/testing.
    """
    qemu = QEMUHypervisor(data_dir=data_dir)
    if qemu.is_available():
        return qemu

    if allow_mock:
        from virtualize.core.mock_hypervisor import MockHypervisor
        logger.warning("No real hypervisor found — using MockHypervisor (install QEMU for real VMs)")
        return MockHypervisor(data_dir=data_dir)

    raise RuntimeError(
        "No supported hypervisor found. Please install QEMU:\n"
        "  Linux:   sudo apt install qemu-system-x86 qemu-utils\n"
        "  macOS:   brew install qemu\n"
        "  Windows: choco install qemu  (or download from qemu.org)"
    )
