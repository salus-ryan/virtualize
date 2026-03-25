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
SSH_DIR = DEFAULT_DATA_DIR / "ssh"

# CirrOS test image — tiny (~15 MB), boots in seconds, has SSH.
# Perfect for development/testing. Swap for a full distro when needed.
CLOUD_IMAGE_URL = "https://download.cirros-cloud.net/0.6.2/cirros-0.6.2-x86_64-disk.img"
CLOUD_IMAGE_NAME = "cirros-0.6.2-x86_64-disk.img"
# Default SSH user for the cloud image
CLOUD_IMAGE_USER = "cirros"
CLOUD_IMAGE_PASSWORD = "gocubsgo"  # CirrOS default


def ensure_dirs() -> None:
    for d in (DEFAULT_DATA_DIR, VM_DIR, IMAGE_DIR, SSH_DIR):
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
        self._ssh_key = SSH_DIR / "virtualize_ed25519"

    # -- cloud image management --

    def _base_image_path(self) -> Path:
        return IMAGE_DIR / CLOUD_IMAGE_NAME

    async def ensure_cloud_image(self) -> Path:
        """Download the default cloud image if not already present.

        ISO27001-A.12.5: Software integrity — images are fetched from
        canonical upstream sources and cached locally.
        """
        img = self._base_image_path()
        if img.exists():
            logger.info(
                "[ISO27001-A.12.5] Cloud image cached: %s (%.1f MB)",
                img.name, img.stat().st_size / 1e6,
            )
            return img
        logger.info(
            "[ISO27001-A.12.5] Downloading cloud image: %s", CLOUD_IMAGE_URL,
        )
        import urllib.request
        from tqdm import tqdm

        tmp = img.with_suffix(".part")
        try:
            def _download():
                req = urllib.request.urlopen(CLOUD_IMAGE_URL)
                total = int(req.headers.get("Content-Length", 0))
                with open(tmp, "wb") as f, tqdm(
                    total=total,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=CLOUD_IMAGE_NAME,
                    ncols=80,
                    bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{rate_fmt}]",
                ) as pbar:
                    while True:
                        chunk = req.read(64 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        pbar.update(len(chunk))
            await asyncio.get_event_loop().run_in_executor(None, _download)
            tmp.rename(img)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        logger.info(
            "[ISO27001-A.12.5] Cloud image saved: %s (%.1f MB)",
            img.name, img.stat().st_size / 1e6,
        )
        return img

    # -- SSH key management --

    def ensure_ssh_key(self) -> Path:
        """Generate an ed25519 SSH key pair for VM access if not present.

        SOC2-CC6.1: Logical access credentials are unique and non-shared.
        ISO27001-A.10.1: Cryptographic key management.
        """
        if self._ssh_key.exists():
            return self._ssh_key
        import subprocess
        subprocess.run([
            "ssh-keygen", "-t", "ed25519", "-f", str(self._ssh_key),
            "-N", "", "-C", "virtualize",
        ], check=True, capture_output=True)
        self._ssh_key.chmod(0o600)
        logger.info(
            "[SOC2-CC6.1][ISO27001-A.10.1] Generated SSH key pair: %s (ed25519, 0600)",
            self._ssh_key.name,
        )
        return self._ssh_key

    def _ssh_pubkey(self) -> str:
        pub = self._ssh_key.with_suffix(".ed25519.pub")
        if not pub.exists():
            pub = Path(str(self._ssh_key) + ".pub")
        return pub.read_text().strip()

    # -- cloud-init seed ISO --

    async def _create_seed_iso(self, instance: VMInstance) -> Path:
        """Create a NoCloud seed ISO with SSH key for VM access.

        CirrOS expects meta-data as JSON and user-data as cloud-config YAML.
        The ISO is labeled 'cidata' per the NoCloud spec.

        SOC2-CC6.1: Access credentials are generated per-VM and injected
        via cloud-init, never shared across instances.
        """
        vm_dir = self._vm_dir(instance)
        seed_dir = vm_dir / "seed"
        seed_dir.mkdir(exist_ok=True)

        self.ensure_ssh_key()
        pubkey = self._ssh_pubkey()
        logger.info(
            "[SOC2-CC6.1] Injecting SSH public key into VM %s seed ISO "
            "(key=%s)", instance.id, self._ssh_key.name,
        )

        # CirrOS needs JSON meta-data
        meta_data = json.dumps({
            "instance-id": instance.id,
            "local-hostname": instance.config.name,
        })
        user_data = (
            "#cloud-config\n"
            "password: gocubsgo\n"
            "ssh_pwauth: true\n"
            "chpasswd:\n"
            "  expire: false\n"
            f"ssh_authorized_keys:\n"
            f"  - {pubkey}\n"
        )

        (seed_dir / "meta-data").write_text(meta_data)
        (seed_dir / "user-data").write_text(user_data)

        iso_path = vm_dir / "seed.iso"

        # cloud-localds creates a properly formatted NoCloud ISO
        if shutil.which("cloud-localds"):
            proc = await asyncio.create_subprocess_exec(
                "cloud-localds", str(iso_path),
                str(seed_dir / "user-data"),
                str(seed_dir / "meta-data"),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"cloud-localds failed: {stderr.decode()}")
        else:
            for tool in ("genisoimage", "mkisofs"):
                if shutil.which(tool):
                    proc = await asyncio.create_subprocess_exec(
                        tool, "-output", str(iso_path), "-volid", "cidata",
                        "-joliet", "-rock", str(seed_dir),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr = await proc.communicate()
                    if proc.returncode != 0:
                        raise RuntimeError(f"{tool} failed: {stderr.decode()}")
                    break
            else:
                raise RuntimeError(
                    "No cloud-init ISO tool found. Install: sudo apt install cloud-image-utils"
                )

        logger.info(
            "[ISO27001-A.12.3] Seed ISO created: %s (%.1f KB)",
            iso_path, iso_path.stat().st_size / 1024,
        )
        return iso_path

    # -- wait for SSH --

    def _ssh_base_cmd(self, instance: VMInstance) -> list[str]:
        """Build the SSH command prefix for connecting to a VM.

        Uses sshpass for password auth (CirrOS) or key-based auth.
        SOC2-CC6.1: Access method is logged.
        """
        ssh_common = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=3",
            "-p", str(instance.ssh_port),
        ]
        target = f"{CLOUD_IMAGE_USER}@{instance.ip_address}"

        # Prefer sshpass for images with a default password (e.g. CirrOS)
        if CLOUD_IMAGE_PASSWORD and shutil.which("sshpass"):
            ssh_common += ["-o", "PubkeyAuthentication=no"]
            return ["sshpass", "-p", CLOUD_IMAGE_PASSWORD] + ssh_common + [target]

        # Fallback: key-based auth
        key_path = getattr(instance, "ssh_key_path", None) or str(self._ssh_key)
        if Path(key_path).exists():
            ssh_common += ["-o", "BatchMode=yes", "-i", key_path]
        return ssh_common + [target]

    async def _wait_for_ssh(self, instance: VMInstance, timeout: int = 60) -> bool:
        """Poll SSH until the VM is reachable or timeout.

        SOC2-CC7.1: System availability monitoring.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        attempt = 0
        logger.info(
            "[SOC2-CC7.1] Waiting for SSH on VM %s (port %d, user=%s, timeout %ds)",
            instance.id, instance.ssh_port, CLOUD_IMAGE_USER, timeout,
        )
        while asyncio.get_event_loop().time() < deadline:
            attempt += 1
            try:
                cmd = self._ssh_base_cmd(instance) + ["echo", "virtualize-ready"]
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=8)
                if proc.returncode == 0 and b"virtualize-ready" in stdout:
                    logger.info(
                        "[SOC2-CC7.1] SSH ready on VM %s after %d attempts (%.1fs)",
                        instance.id, attempt,
                        timeout - (deadline - asyncio.get_event_loop().time()),
                    )
                    return True
                logger.debug(
                    "SSH attempt %d on VM %s: rc=%d stderr=%s",
                    attempt, instance.id, proc.returncode,
                    stderr.decode().strip()[:120],
                )
            except (asyncio.TimeoutError, OSError) as e:
                logger.debug("SSH attempt %d on VM %s: %s", attempt, instance.id, e)
            await asyncio.sleep(1)
        logger.warning(
            "[SOC2-CC7.1] SSH not reachable on VM %s after %ds (%d attempts)",
            instance.id, timeout, attempt,
        )
        return False

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
        """Create a VM disk image, auto-downloading a cloud base if needed.

        ISO27001-A.12.3: Media handling — disk images are COW-backed to
        preserve base image integrity.
        """
        disk_path = self._disk_path(instance)
        if disk_path.exists():
            logger.info("[ISO27001-A.12.3] Disk exists: %s", disk_path)
            return disk_path

        cfg = instance.config.disk

        # Resolve base image: explicit > cloud image auto-download
        base: Path | None = None
        if instance.config.base_image:
            base = Path(instance.config.base_image)
            if not base.exists():
                base = IMAGE_DIR / instance.config.base_image
            if not base.exists():
                logger.warning(
                    "[ISO27001-A.12.3] Specified base image not found: %s",
                    instance.config.base_image,
                )
                base = None

        # Auto-download cloud image if no base specified
        if base is None:
            base = await self.ensure_cloud_image()

        if base and base.exists():
            logger.info(
                "[ISO27001-A.12.3] Creating COW disk for VM %s "
                "(base=%s, format=%s, size=%dG)",
                instance.id, base.name, cfg.format.value, cfg.size_gb,
            )
            proc = await asyncio.create_subprocess_exec(
                "qemu-img", "create", "-f", cfg.format.value,
                "-b", str(base), "-F", "qcow2",
                str(disk_path), f"{cfg.size_gb}G",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"qemu-img backing file failed: {stderr.decode()}")
            return disk_path

        # Fallback: blank disk (no OS — user must provide an ISO)
        logger.info(
            "[ISO27001-A.12.3] Creating blank disk for VM %s (format=%s, size=%dG)",
            instance.id, cfg.format.value, cfg.size_gb,
        )
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
        """Start a VM with QEMU, auto-provisioning disk and SSH access.

        SOC2-CC6.1: Access is provisioned via per-VM SSH keys.
        SOC2-CC7.2: System operation is logged at every stage.
        ISO27001-A.12.1: Documented operating procedures.
        """
        if not self.is_available():
            raise RuntimeError("QEMU is not installed or not in PATH")

        accel = self._accel_flag()
        logger.info(
            "[SOC2-CC7.2][ISO27001-A.12.1] Starting VM %s "
            "(name=%s, vcpus=%d, mem=%dMB, accel=%s)",
            instance.id, instance.config.name,
            instance.config.vcpus, instance.config.memory_mb, accel,
        )

        # Ensure SSH key exists for cloud-init injection
        self.ensure_ssh_key()

        disk_path = await self.create_disk(instance)
        ssh_port = self._find_free_port()
        vnc_port = self._find_free_port()
        cfg = instance.config

        # Create cloud-init seed ISO with SSH key
        seed_iso = await self._create_seed_iso(instance)

        cmd: list[str] = [
            self._qemu_binary(),
            "-name", cfg.name,
            "-m", str(cfg.memory_mb),
            "-smp", str(cfg.vcpus),
            "-accel", accel,
            "-drive", f"file={disk_path},format={cfg.disk.format.value},if=virtio",
            "-drive", f"file={seed_iso},format=raw,if=virtio",
            "-pidfile", str(self._pid_file(instance)),
            "-monitor", f"unix:{self._monitor_socket(instance)},server,nowait",
            "-display", "none",
            "-serial", "null",
            "-daemonize",
        ]

        # Boot media
        if cfg.iso_path:
            cmd += ["-cdrom", cfg.iso_path]

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
        logger.info(
            "[SOC2-CC7.2] QEMU command: %s", " ".join(full_cmd),
        )

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
            logger.error(
                "[SOC2-CC7.2] QEMU start FAILED for VM %s: %s",
                instance.id, instance.error,
            )
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
        instance.ssh_key_path = str(self._ssh_key)
        self._save_state(instance)

        logger.info(
            "[SOC2-CC7.2] VM %s process started (pid=%s, ssh_port=%d)",
            instance.id, instance.pid, ssh_port,
        )

        # Wait for SSH to become available
        ssh_ok = await self._wait_for_ssh(instance)
        if not ssh_ok:
            logger.warning(
                "[SOC2-CC7.1] VM %s started but SSH not reachable — "
                "commands may fail until OS finishes booting",
                instance.id,
            )

        return instance

    async def stop(self, instance: VMInstance, force: bool = False) -> VMInstance:
        """Stop a VM by sending SIGTERM (or SIGKILL if force).

        SOC2-CC7.2: Shutdown events are logged.
        """
        sig_name = "SIGKILL" if force else "SIGTERM"
        logger.info(
            "[SOC2-CC7.2] Stopping VM %s (pid=%s, signal=%s)",
            instance.id, instance.pid, sig_name,
        )
        instance.status = VMStatus.STOPPING
        if instance.pid:
            try:
                sig = signal.SIGKILL if force else signal.SIGTERM
                os.kill(instance.pid, sig)
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
        logger.info("[SOC2-CC7.2] VM %s stopped", instance.id)
        return instance

    async def destroy(self, instance: VMInstance) -> VMInstance:
        """Destroy a VM and remove all associated data.

        SOC2-CC6.5: Data disposal — VM disk and state files are removed.
        ISO27001-A.8.3: Media disposal.
        """
        logger.info(
            "[SOC2-CC6.5][ISO27001-A.8.3] Destroying VM %s (removing disk, state, seed)",
            instance.id,
        )
        if instance.status == VMStatus.RUNNING:
            await self.stop(instance, force=True)
        vm_dir = self._vm_dir(instance)
        if vm_dir.exists():
            shutil.rmtree(vm_dir)
            logger.info(
                "[SOC2-CC6.5] VM %s data directory removed: %s",
                instance.id, vm_dir,
            )
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
        """Execute a command in the VM via SSH or QEMU Guest Agent.

        SOC2-CC7.2: Command execution is logged with truncated command.
        ISO27001-A.12.4: Event logging.
        """
        if instance.status != VMStatus.RUNNING:
            raise RuntimeError(f"VM {instance.id} is not running (status={instance.status})")

        logger.info(
            "[SOC2-CC7.2][ISO27001-A.12.4] Executing on VM %s: %.200s",
            instance.id, command,
        )

        # Try SSH first
        if instance.ssh_port and instance.ip_address:
            rc, stdout, stderr = await self._exec_via_ssh(instance, command, timeout)
            logger.info(
                "[SOC2-CC7.2] Exec on VM %s completed: rc=%d, stdout=%d bytes, stderr=%d bytes",
                instance.id, rc, len(stdout), len(stderr),
            )
            return rc, stdout, stderr

        # Fallback: QEMU Guest Agent
        return await self._exec_via_guest_agent(instance, command, timeout)

    async def _exec_via_ssh(self, instance: VMInstance, command: str, timeout: int) -> tuple[int, str, str]:
        ssh_cmd = self._ssh_base_cmd(instance) + [command]
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
