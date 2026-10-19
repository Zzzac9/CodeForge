"""Shell execution backends.

The default backend creates one restricted Docker container per invocation.
``unsafe-host`` exists only as an explicit compatibility escape hatch and is
never selected as a fallback when Docker is unavailable.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


class SandboxUnavailableError(RuntimeError):
    pass


@dataclass
class ShellSandboxConfig:
    backend: str = "docker"
    image: str = "python:3.13-slim"
    workspace_mode: str = "read-only"
    network: str = "none"
    cpus: float = 1.0
    memory: str = "512m"
    pids_limit: int = 128
    output_limit: int = 65536
    startup_timeout: int = 30
    auto_start_docker_desktop: bool = True

    def __post_init__(self):
        if self.backend not in {"docker", "disabled", "unsafe-host"}:
            raise ValueError(f"unsupported shell backend: {self.backend}")
        if self.workspace_mode not in {"read-only", "read-write"}:
            raise ValueError(f"unsupported workspace mode: {self.workspace_mode}")
        if self.network != "none":
            raise ValueError("the Docker shell network must remain disabled")
        if float(self.cpus) <= 0 or int(self.pids_limit) <= 0 or int(self.output_limit) <= 0:
            raise ValueError("shell resource limits must be positive")
        if int(self.startup_timeout) < 1 or int(self.startup_timeout) > 300:
            raise ValueError("Docker startup timeout must be in [1, 300]")


@dataclass
class SandboxStatus:
    backend: str
    state: str
    detail: str = ""
    docker_desktop_started: bool = False
    image_available: bool = False

    def to_dict(self):
        return asdict(self)


@dataclass
class ShellResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    sandbox_status: str
    backend: str
    container_name: str = ""
    cleanup_succeeded: bool = True
    output_truncated: bool = False

    def to_dict(self):
        return asdict(self)

    def render(self):
        timeout_line = "timed_out: true\n" if self.timed_out else ""
        truncated_line = "output_truncated: true\n" if self.output_truncated else ""
        return (
            f"exit_code: {self.exit_code}\n"
            f"sandbox_backend: {self.backend}\n"
            f"sandbox_status: {self.sandbox_status}\n"
            f"{timeout_line}{truncated_line}"
            "stdout:\n"
            f"{self.stdout.strip() or '(empty)'}\n"
            "stderr:\n"
            f"{self.stderr.strip() or '(empty)'}"
        )


class _BoundedPipeReader:
    def __init__(self, stream, limit):
        self.stream = stream
        self.limit = max(0, int(limit))
        self.chunks = []
        self.size = 0
        self.truncated = False
        self.thread = threading.Thread(target=self._read, daemon=True)

    def start(self):
        self.thread.start()

    def join(self):
        self.thread.join(timeout=5)

    def _read(self):
        while True:
            chunk = self.stream.read(8192)
            if not chunk:
                return
            remaining = self.limit - self.size
            if remaining > 0:
                kept = chunk[:remaining]
                self.chunks.append(kept)
                self.size += len(kept)
            if len(chunk) > max(remaining, 0):
                self.truncated = True

    def text(self):
        data = b"".join(self.chunks)
        return data.decode("utf-8", errors="replace")


def _popen_flags():
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    return kwargs


class _WindowsKillJob:
    """A Windows Job Object that kills the complete process tree on close."""

    def __init__(self, process):
        self.handle = None
        if os.name != "nt":
            return

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_ulong),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_ulong),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_ulong),
                ("SchedulingClass", ctypes.c_ulong),
            ]

        class EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(handle)
            return
        if not kernel32.AssignProcessToJobObject(handle, ctypes.c_void_p(int(process._handle))):
            kernel32.CloseHandle(handle)
            return
        self.handle = handle
        self._kernel32 = kernel32

    def close(self):
        if self.handle:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


def _terminate_process_tree(process, job=None):
    if process.poll() is not None:
        return
    if job is not None and job.handle:
        job.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        try:
            os.killpg(process.pid, 15)
            process.wait(timeout=3)
        except Exception:
            try:
                os.killpg(process.pid, 9)
            except ProcessLookupError:
                pass


def _run_bounded(argv, *, cwd, env, timeout, output_limit, shell=False, executable=None):
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=shell,
        executable=executable,
        **_popen_flags(),
    )
    job = _WindowsKillJob(process) if os.name == "nt" else None
    stdout_reader = _BoundedPipeReader(process.stdout, output_limit)
    stderr_reader = _BoundedPipeReader(process.stderr, output_limit)
    stdout_reader.start()
    stderr_reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_tree(process, job)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    except BaseException:
        _terminate_process_tree(process, job)
        raise
    finally:
        if job is not None:
            job.close()
        stdout_reader.join()
        stderr_reader.join()
    return (
        -1 if timed_out else int(process.returncode),
        stdout_reader.text(),
        stderr_reader.text(),
        timed_out,
        stdout_reader.truncated or stderr_reader.truncated,
    )


class DockerSandbox:
    def __init__(self, workspace_root, config=None, *, owner_id=None):
        self.workspace_root = Path(workspace_root).resolve()
        if not self.workspace_root.is_dir():
            raise ValueError(f"workspace root is not a directory: {workspace_root}")
        self.config = config or ShellSandboxConfig()
        self.owner_id = owner_id or ("codeforge-" + uuid.uuid4().hex[:12])
        self.status = SandboxStatus(self.config.backend, "not_checked")
        self._containers = set()

    def initialize(self):
        if self.config.backend == "disabled":
            self.status = SandboxStatus("disabled", "disabled", "shell execution is disabled")
            return self.status
        if self.config.backend == "unsafe-host":
            self.status = SandboxStatus("unsafe-host", "ready", "explicit non-sandboxed host execution")
            return self.status
        docker = shutil.which("docker")
        if not docker:
            self.status = SandboxStatus("docker", "unavailable", "Docker CLI was not found")
            return self.status

        ready, detail = self._probe_engine(docker)
        desktop_started = False
        if not ready and platform.system() == "Windows" and self.config.auto_start_docker_desktop:
            desktop = self._find_docker_desktop()
            if desktop:
                try:
                    subprocess.Popen(
                        [os.fspath(desktop)],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    desktop_started = True
                    deadline = time.monotonic() + max(1, self.config.startup_timeout)
                    while time.monotonic() < deadline:
                        ready, detail = self._probe_engine(docker)
                        if ready:
                            break
                        time.sleep(0.5)
                except Exception as exc:
                    detail = f"failed to start Docker Desktop: {exc}"
        if not ready:
            self.status = SandboxStatus("docker", "unavailable", detail, desktop_started, False)
            return self.status

        image = subprocess.run(
            [docker, "image", "inspect", self.config.image],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        if image.returncode != 0:
            self.status = SandboxStatus(
                "docker",
                "image_missing",
                f"trusted image is not local: {self.config.image}; pull it explicitly",
                desktop_started,
                False,
            )
            return self.status
        self.status = SandboxStatus("docker", "ready", detail, desktop_started, True)
        return self.status

    def _probe_engine(self, docker):
        try:
            result = subprocess.run(
                [docker, "info", "--format", "{{.ServerVersion}}"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except Exception as exc:
            return False, f"Docker Engine probe failed: {exc}"
        if result.returncode == 0 and result.stdout.strip():
            return True, f"Docker Engine {result.stdout.strip()} is ready"
        detail = result.stderr.strip() or result.stdout.strip() or "Docker Engine is not ready"
        return False, detail

    @staticmethod
    def _find_docker_desktop():
        candidates = []
        for env_name in ("ProgramFiles", "LOCALAPPDATA"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(Path(base) / "Docker" / "Docker" / "Docker Desktop.exe")
        return next((path for path in candidates if path.is_file()), None)

    def run(self, command, timeout, env):
        if self.config.backend == "unsafe-host":
            return self._run_host(command, timeout, env)
        if self.status.state == "not_checked":
            self.initialize()
        if self.status.state != "ready":
            raise SandboxUnavailableError(self.status.detail or f"Docker sandbox is {self.status.state}")

        docker = shutil.which("docker")
        if not docker:
            raise SandboxUnavailableError("Docker CLI disappeared after startup check")
        name = f"codeforge-{uuid.uuid4().hex[:16]}"
        self._containers.add(name)
        mount_mode = "ro" if self.config.workspace_mode == "read-only" else "rw"
        if os.name != "nt" and hasattr(os, "getuid") and os.getuid() != 0:
            container_user = f"{os.getuid()}:{os.getgid()}"
        else:
            container_user = "65534:65534"
        argv = [
            docker, "run", "--rm", "--name", name,
            "--label", f"com.codeforge.owner={self.owner_id}",
            "--network", self.config.network,
            "--user", container_user,
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--pids-limit", str(self.config.pids_limit),
            "--cpus", str(self.config.cpus),
            "--memory", self.config.memory,
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--volume", f"{self.workspace_root}:/workspace:{mount_mode}",
            "--workdir", "/workspace",
            "--env", "HOME=/tmp",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
        ]
        host_only_names = {
            "PWD", "HOME", "PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR",
            "TMP", "TEMP", "TMPDIR", "USER", "LOGNAME", "SHELL",
        }
        for key, value in sorted(env.items()):
            if key.upper() in host_only_names:
                continue
            argv.extend(["--env", f"{key}={value}"])
        argv.extend([self.config.image, "sh", "-lc", command])

        cleanup_succeeded = True
        try:
            code, stdout, stderr, timed_out, truncated = _run_bounded(
                argv,
                cwd=self.workspace_root,
                env=self._docker_cli_env(),
                timeout=timeout,
                output_limit=self.config.output_limit,
            )
        finally:
            cleanup_succeeded = self._remove_container(name)
            self._containers.discard(name)
        status = "timed_out" if timed_out else ("exited" if code == 0 else "failed")
        return ShellResult(code, stdout, stderr, timed_out, status, "docker", name, cleanup_succeeded, truncated)

    def _run_host(self, command, timeout, env):
        # This path is intentionally explicit and visibly marked unsafe.  It is
        # never called as a Docker failure fallback.
        if os.name == "nt":
            argv = command
            use_shell = True
            executable = os.fspath(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe")
        else:
            argv = ["/bin/sh", "-lc", command]
            use_shell = False
            executable = None
        code, stdout, stderr, timed_out, truncated = _run_bounded(
            argv,
            cwd=self.workspace_root,
            env=env,
            timeout=timeout,
            output_limit=self.config.output_limit,
            shell=use_shell,
            executable=executable,
        )
        status = "timed_out" if timed_out else ("exited" if code == 0 else "failed")
        return ShellResult(code, stdout, stderr, timed_out, status, "unsafe-host", output_truncated=truncated)

    @staticmethod
    def _docker_cli_env():
        # Docker CLI needs its own host-side connection variables, but none are
        # copied into the container.
        allowed = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "DOCKER_HOST", "DOCKER_CONTEXT")
        return {key: os.environ[key] for key in allowed if key in os.environ}

    def _remove_container(self, name):
        docker = shutil.which("docker")
        if not docker:
            return False
        try:
            result = subprocess.run(
                [docker, "rm", "-f", name],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
            # --rm may already have removed a normally exited container.
            stderr = result.stderr.decode("utf-8", errors="replace").lower()
            return result.returncode == 0 or "no such container" in stderr
        except Exception:
            return False

    def close(self):
        results = []
        for name in list(self._containers):
            results.append(self._remove_container(name))
            self._containers.discard(name)
        return all(results) if results else True
