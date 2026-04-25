"""Dev-server lifecycle. Owns the Vite child so it always dies with us.

Windows: we create a Job Object with `KILL_ON_JOB_CLOSE` and assign the
Vite process to it. When our process exits — clean, crash, Ctrl+C in
PowerShell, kill -9 — the OS terminates everything in the job. No more
leaked node.exe holding the dev port.

Posix: child runs in its own session; we SIGTERM the process group on
shutdown.

Use:
    proc, job = dev_server.start(FRONTEND, port=5273)
    try:
        ...
    finally:
        dev_server.stop(proc, job)
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

IS_WIN = os.name == "nt"


# --- Windows Job Object (kills children when parent dies) -------------

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9
    PROCESS_TERMINATE = 0x0001
    PROCESS_SET_QUOTA = 0x0100

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _k32 = ctypes.windll.kernel32
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.CloseHandle.restype = wintypes.BOOL


    class JobObject:
        def __init__(self) -> None:
            self.handle = _k32.CreateJobObjectW(None, None)
            if not self.handle:
                raise ctypes.WinError()
            info = _EXTENDED_LIMIT()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = _k32.SetInformationJobObject(
                self.handle, JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info),
            )
            if not ok:
                err = ctypes.WinError()
                _k32.CloseHandle(self.handle)
                self.handle = None
                raise err

        def assign(self, pid: int) -> None:
            ph = _k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
            if not ph:
                raise ctypes.WinError()
            try:
                if not _k32.AssignProcessToJobObject(self.handle, ph):
                    raise ctypes.WinError()
            finally:
                _k32.CloseHandle(ph)

        def close(self) -> None:
            if self.handle:
                _k32.CloseHandle(self.handle)
                self.handle = None

        def __del__(self):
            try:
                self.close()
            except Exception:
                pass

else:
    class JobObject:  # no-op placeholder on posix
        def __init__(self): pass
        def assign(self, pid: int): pass
        def close(self): pass


# --- Port introspection -----------------------------------------------

@dataclass
class PortHolder:
    pid: int
    name: str = ""


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def who_holds(port: int) -> Optional[PortHolder]:
    """Best-effort identification of the process listening on `port`."""
    if IS_WIN:
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, check=False,
            ).stdout
        except OSError:
            return None
        needle = f":{port} "
        pid: Optional[int] = None
        for line in out.splitlines():
            if needle in line and "LISTENING" in line:
                try:
                    pid = int(line.split()[-1])
                    break
                except (ValueError, IndexError):
                    continue
        if pid is None:
            return None
        name = ""
        try:
            tl = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, check=False,
            ).stdout.strip()
            if tl:
                name = tl.split(",")[0].strip('"')
        except OSError:
            pass
        return PortHolder(pid=pid, name=name)
    # posix: skip; rare for our use case
    return None


def kill_pid(pid: int) -> None:
    if IS_WIN:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass


# --- Server lifecycle -------------------------------------------------

def _npm() -> str:
    exe = shutil.which("npm.cmd" if IS_WIN else "npm")
    if not exe:
        sys.exit("npm not found on PATH")
    return exe


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        sys.exit("node not found on PATH")
    return exe


def start(cwd: Path, *, port: int) -> tuple[subprocess.Popen, JobObject]:
    """Spawn vite in `cwd`, bound to a Job Object on Windows.

    We invoke `node ./node_modules/vite/bin/vite.js` directly rather than
    going through `npm run dev`. The npm.cmd shim adds a layer of process
    indirection (cmd → npm → node) that has been observed to spawn vite
    twice on Windows under certain conditions. Direct node makes vite our
    immediate child — clean single process, easy to track and kill.
    """
    vite_js = cwd / "node_modules" / "vite" / "bin" / "vite.js"
    if not vite_js.exists():
        sys.exit(
            f"vite not found at {vite_js}\n"
            f"  run `npm install` in {cwd} first",
        )
    job = JobObject()
    cmd = [_node(), str(vite_js), "--port", str(port), "--strictPort"]
    sys.stdout.write(f"[dev_server] spawn: node vite.js --port {port} --strictPort\n")
    sys.stdout.flush()
    proc = subprocess.Popen(cmd, cwd=str(cwd))
    sys.stdout.write(f"[dev_server] vite pid={proc.pid} (job-bound)\n")
    sys.stdout.flush()
    try:
        job.assign(proc.pid)
    except OSError as e:
        sys.stderr.write(f"[dev_server] warning: could not assign vite to job: {e}\n")
    return proc, job


def stop(proc: subprocess.Popen, job: JobObject) -> None:
    if proc.poll() is None:
        kill_pid(proc.pid)
    job.close()  # also kills anything still in the job (Windows)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
