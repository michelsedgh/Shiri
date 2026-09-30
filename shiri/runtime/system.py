"""Bounded subprocesses, exact process ownership, and durable runtime state."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import stat
import tempfile
from typing import Any

log = logging.getLogger(__name__)


class RuntimeFailure(RuntimeError):
    """An actionable backend failure, suitable for a room's health status."""


def root_directory(path: Path, mode=0o700):
    """Refuse writable/symlink privilege boundaries before creating files."""
    path = path.absolute()
    for parent in reversed([path, *path.parents]):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            parent.mkdir(mode=mode)
            info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
            raise RuntimeFailure(f"Runtime directory must be an actual root-owned directory: {parent}")
        if info.st_mode & 0o022 and not (info.st_mode & stat.S_ISVTX and parent != path):
            raise RuntimeFailure(f"Runtime directory is writable by another user: {parent}")


def atomic_json(path: Path, value: Any, mode: int = 0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            json.dump(value, stream, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


def read_json(path: Path, default=None):
    try:
        with path.open() as stream:
            return json.load(stream)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise RuntimeFailure(f"Cannot read runtime ownership state {path}: {exc}") from exc


def process_birth(pid: int) -> str | None:
    if pid <= 1:
        return None
    try:
        # comm may contain spaces and parentheses. Field 22 is starttime.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def process_args(pid: int) -> list[str]:
    try:
        return [
            item.decode(errors="replace")
            for item in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if item
        ]
    except OSError:
        return []


def boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
    except OSError:
        return None


def process_executable(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class OwnedProcess:
    name: str
    process: asyncio.subprocess.Process
    birth: str | None
    log_task: asyncio.Task
    log_path: Path

    @property
    def alive(self):
        return (
            self.process.returncode is None
            and self.birth is not None
            and process_birth(self.process.pid) == self.birth
        )

    def identity(self):
        return {
            "name": self.name,
            "pid": self.process.pid,
            "birth": self.birth,
            "pgid": self.process.pid,
            "boot_id": boot_id(),
            "executable": process_executable(self.process.pid),
            "argv": process_args(self.process.pid),
            "log_path": str(self.log_path),
        }

    async def coherent_identity(self):
        """Wait through exec's transient empty /proc fields without losing ownership."""
        original_boot = boot_id()
        for _ in range(100):
            if not self.alive:
                raise RuntimeFailure(f"{self.name} exited while its process identity was being recorded")
            identity = self.identity()
            if (
                original_boot
                and identity["boot_id"] == original_boot
                and identity["executable"]
                and identity["argv"]
                and process_executable(self.process.pid) == identity["executable"]
                and process_args(self.process.pid) == identity["argv"]
                and process_birth(self.process.pid) == self.birth
            ):
                return identity
            await asyncio.sleep(0.02)
        raise RuntimeFailure(
            f"Could not capture a coherent process identity for {self.name} within two seconds"
        )

    async def stop(self, grace=4.0):
        if self.alive:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), timeout=grace)
            except asyncio.TimeoutError:
                if self.alive:
                    with suppress(ProcessLookupError):
                        os.killpg(self.process.pid, signal.SIGKILL)
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=2)
                except asyncio.TimeoutError as exc:
                    raise RuntimeFailure(
                        f"{self.name} did not terminate; its resources remain reserved"
                    ) from exc
        else:
            # Reap a child that already exited; never signal a reused PID.
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2)
            except asyncio.TimeoutError as exc:
                raise RuntimeFailure(
                    f"Cannot prove termination of {self.name}; resources remain reserved"
                ) from exc
        if not self.log_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.log_task), timeout=2)
            except asyncio.TimeoutError:
                raise RuntimeFailure(
                    f"{self.name} still has an open output stream; cleanup remains pending"
                ) from None
        with suppress(asyncio.CancelledError):
            await self.log_task


class Runner:
    async def run(self, args: list[str], *, timeout=12.0, check=True) -> CommandResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise RuntimeFailure(f"Cannot run {args[0]}: {exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            # A command such as dhclient can have a child. Only this new process
            # group is owned; namespace cleanup subsequently reaps DHCP daemons.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise RuntimeFailure(f"{args[0]} timed out after {timeout:g}s") from exc
        result = CommandResult(
            args, process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")
        )
        if check and result.returncode:
            detail = (result.stderr or result.stdout).strip()[-1500:]
            raise RuntimeFailure(f"{' '.join(args[:4])} failed: {detail or result.returncode}")
        return result

    async def json(self, args: list[str], *, check=True):
        result = await self.run(args, check=check)
        if result.returncode and not check:
            return []
        try:
            return json.loads(result.stdout or "[]")
        except ValueError as exc:
            raise RuntimeFailure(f"{args[0]} returned invalid JSON") from exc

    async def start(self, name: str, args: list[str], log_path: Path, *, env=None) -> OwnedProcess:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                env=env,
            )
        except OSError as exc:
            handler.close()
            raise RuntimeFailure(f"Cannot start {name}: {exc}") from exc

        async def copy_log():
            try:
                # Chunk reads avoid a daemon's unbounded line exceeding StreamReader limits.
                while chunk := await process.stdout.read(8192):
                    handler.emit(
                        logging.LogRecord(
                            name, logging.INFO, "", 0, chunk.decode(errors="replace").rstrip(), (), None
                        )
                    )
            finally:
                handler.close()

        owned = OwnedProcess(
            name, process, process_birth(process.pid), asyncio.create_task(copy_log()), log_path
        )
        if owned.birth is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            await owned.log_task
            raise RuntimeFailure(f"Cannot record the process identity for {name}")
        return owned

    async def stop_saved(self, entry: dict):
        pid, birth, pgid = entry.get("pid"), entry.get("birth"), entry.get("pgid")

        # PID, group and start ticks can repeat after reboot. A durable identity
        # also binds the boot, executable and exact command line; old/incomplete
        # entries are deliberately left unsignalled.
        def matches():
            return bool(
                isinstance(pid, int)
                and not isinstance(pid, bool)
                and pid > 1
                and pgid == pid
                and birth
                and process_birth(pid) == birth
                and entry.get("boot_id")
                and entry["boot_id"] == boot_id()
                and entry.get("executable")
                and entry["executable"] == process_executable(pid)
                and isinstance(entry.get("argv"), list)
                and entry["argv"]
                and entry["argv"] == process_args(pid)
            )

        if not matches():
            return
        try:
            if os.getpgid(pid) != pgid:
                return
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        for _ in range(20):
            if not matches():
                return
            await asyncio.sleep(0.1)
        if matches():
            with suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)
            for _ in range(20):
                if not matches():
                    return
                await asyncio.sleep(0.1)
            raise RuntimeFailure("A saved backend process did not terminate; its ownership remains reserved")
