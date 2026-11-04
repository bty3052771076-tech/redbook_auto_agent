"""Memory-aware admission for existing queues; no service is started on import."""
from __future__ import annotations

import atexit
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import ctypes
from dataclasses import asdict, dataclass
import json
from logging import Formatter, getLogger
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import threading
import time
from uuid import uuid4

MIB = 1024 * 1024
GIB = 1024 * MIB
_nested = ContextVar("memory_admission", default=frozenset())
_local_lock = threading.Lock()
_active = 0
_observer = None
_observer_stop = threading.Event()


def runtime_root() -> Path:
    return Path(os.getenv("REDBOOK_RUNTIME_ROOT") or Path(__file__).resolve().parents[2])


def coordination_root() -> Path:
    shared = Path("E:/AI/codex/redbook_runtime")
    return Path(os.getenv("REDBOOK_RESOURCE_ROOT") or (shared if shared.is_dir() else runtime_root()))

def process_tree() -> dict[int, int]:
    """Read PID/parent relationships without launching a diagnostic subprocess."""
    if os.name != "nt":
        result = {}
        for path in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = path.read_text().rsplit(")", 1)[1].split()
                result[int(path.parent.name)] = int(fields[1])
            except (OSError, ValueError, IndexError):
                continue
        return result
    from ctypes import wintypes
    class Entry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
                    ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG),
                    ("flags", wintypes.DWORD), ("exe", wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError("process snapshot unavailable")
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(entry)
        result = {}
        found = kernel.Process32FirstW(handle, ctypes.byref(entry))
        if not found:
            raise OSError("process enumeration failed")
        while found:
            result[int(entry.pid)] = int(entry.parent)
            found = kernel.Process32NextW(handle, ctypes.byref(entry))
        return result
    finally:
        kernel.CloseHandle(handle)


def process_image(pid: int) -> str | None:
    if os.name != "nt":
        try:
            return os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return None
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        return buffer.value if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) else None
    finally:
        kernel.CloseHandle(handle)


def process_identity(pid: int) -> str | None:
    """None is unknown/dead, never permission to terminate a process."""
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return None
        try:
            exit_code = wintypes.DWORD()
            if kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) and exit_code.value != 259:
                return None
            created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(system), ctypes.byref(user)):
                return None
            return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        fields = raw[raw.rfind(")") + 2:].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError):
        return None


def process_alive(pid: int, identity: str | None) -> bool:
    if int(pid) <= 0:
        return False
    current = process_identity(pid)
    if current is not None:
        return not identity or current == identity
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return ctypes.get_last_error() != 87
        try:
            status = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(status)) or status.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        if Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
            return False
    except (OSError, IndexError):
        pass
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False


@contextmanager
def shared_lock(path: Path):
    """Small cross-process lock shared by independent installations."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        while True:
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)


def read_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class MemorySample:
    total: int | None = None
    available: int | None = None
    commit_available: int | None = None
    working_set: int | None = None
    private_bytes: int | None = None


def sample_memory() -> MemorySample:
    try:
        if os.name == "nt":
            from ctypes import wintypes
            class Status(ctypes.Structure):
                _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
                    (name, ctypes.c_ulonglong) for name in
                    ("total", "available", "page_total", "page_available", "virtual_total",
                     "virtual_available", "extended")]
            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                    (name, ctypes.c_size_t) for name in
                    ("peak_ws", "working", "peak_paged", "paged", "peak_nonpaged",
                     "nonpaged", "pagefile", "peak_pagefile", "private")]
            status = Status()
            status.length = ctypes.sizeof(status)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
                return MemorySample()
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            measured = psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(),
                                                   ctypes.byref(counters), counters.cb)
            return MemorySample(status.total, status.available, status.page_available,
                                counters.working if measured else None,
                                counters.private if measured else None)
        values = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            name, value = line.split(":", 1)
            values[name] = int(value.split()[0]) * 1024
        return MemorySample(total=values.get("MemTotal"), available=values.get("MemAvailable"))
    except (OSError, ValueError, AttributeError):
        return MemorySample()


def _pressure(state: dict, sample: MemorySample, now: float) -> str:
    if sample.available is None or not sample.total:
        return "unknown"
    previous = state.get("pressure", "normal" if sample.available >= 2 * GIB else "tight")
    since = now - float(state.get("sampled_at", 0))
    if 0 <= since < 5:
        return previous
    if since < 0:
        state.pop("healthy_since", None)
        state.pop("ramp_at", None)
    critical = sample.available < max(int(0.75 * GIB), int(sample.total * .05))
    tight = sample.available < max(int(1.5 * GIB), int(sample.total * .10))
    if sample.commit_available is not None:
        critical = critical or sample.commit_available < 256 * MIB
    state["low_samples"] = int(state.get("low_samples", 0)) + 1 if tight else 0
    state["critical_samples"] = int(state.get("critical_samples", 0)) + 1 if critical else 0
    if sample.available >= 2 * GIB and not critical:
        state.setdefault("healthy_since", now)
        if now - state["healthy_since"] >= 30:
            if previous != "normal":
                state["ramp_at"] = now
            previous = "normal"
    else:
        state.pop("healthy_since", None)
    if state["critical_samples"] >= 3 or sample.available < 256 * MIB:
        previous = "critical"
    elif state["low_samples"] >= 3:
        previous = "tight"
    state.update(pressure=previous, sampled_at=now)
    return previous


def resource_status() -> dict:
    sample = sample_memory()
    root = coordination_root() / "data/runs/resource-admission"
    with shared_lock(root / "state.lock"):
        state = read_state(root / "state.json")
        leases = {key: row for key, row in state.get("leases", {}).items()
                  if process_alive(row["pid"], row.get("identity"))}
        state["leases"] = leases
        pressure = _pressure(state, sample, time.time())
        write_state(root / "state.json", state)
    return {"pressure": pressure, "enabled": os.getenv("REDBOOK_MEMORY_ENABLED", "1").lower() not in {"0", "false", "off"}, **asdict(sample),
            "active": {kind: sum(row["kind"] == kind for row in leases.values())
                       for kind in {row["kind"] for row in leases.values()}}}


def embedding_batch_size() -> int:
    status = resource_status()
    return 16 if status["pressure"] == "normal" else 8


def _record(kind: str, status: dict) -> None:
    try:
        directory = runtime_root() / "data/logs/resources" / time.strftime("%Y-%m-%d")
        directory.mkdir(parents=True, exist_ok=True)
        logger = getLogger("redbook.resources")
        target = directory / f"{os.getpid()}.jsonl"
        with _local_lock:
            if not logger.handlers or Path(logger.handlers[0].baseFilename) != target.resolve():
                for previous in list(logger.handlers):
                    logger.removeHandler(previous)
                    previous.close()
                handler = RotatingFileHandler(target, maxBytes=5 * MIB, backupCount=2, encoding="utf-8")
                handler.setFormatter(Formatter("%(message)s"))
                logger.addHandler(handler)
                logger.setLevel(20)
                logger.propagate = False
        logger.info(json.dumps({"at": time.time(), "pid": os.getpid(),
                                "ppid": os.getppid(), "stage": kind, **status}))
    except OSError:
        pass


def _observe() -> None:
    while not _observer_stop.is_set():
        with _local_lock:
            if not _active:
                return
        try:
            _record("active", resource_status())
        except OSError:
            pass
        _observer_stop.wait(5)


@contextmanager
def memory_slot(kind: str, *, limit: int = 1, reserve_mb: int | None = None,
                stop_event=None, progress=None):
    """Reduce new admission, never cancel an already admitted remote request."""
    global _active, _observer
    if os.getenv("REDBOOK_MEMORY_ENABLED", "1").lower() in {"0", "false", "off"} or kind in _nested.get():
        yield
        return
    reserves = {"text": 16, "source": 32, "image": 256,
                "embedding": 128, "browser": 384, "service": 128}
    reserve = (reserve_mb if reserve_mb is not None else reserves.get(kind, 32)) * MIB
    root = coordination_root() / "data/runs/resource-admission"
    identifier, identity = uuid4().hex, process_identity(os.getpid())
    started, last_message = time.monotonic(), -30.0
    while True:
        if stop_event is not None and stop_event.is_set():
            from concurrent.futures import CancelledError
            raise CancelledError("stopped while waiting for memory admission")
        sample, now = sample_memory(), time.time()
        with shared_lock(root / "state.lock"):
            state = read_state(root / "state.json")
            leases = {key: row for key, row in state.get("leases", {}).items()
                      if process_alive(row["pid"], row.get("identity"))}
            pressure = _pressure(state, sample, now)
            cap = max(1, int(limit))
            if pressure != "normal" and not (kind == "text" and pressure == "tight"):
                cap = min(cap, 2 if kind in {"text", "source"} else 1)
            elif pressure == "normal" and state.get("ramp_at"):
                cap = min(cap, 1 + max(0, int((now - state["ramp_at"]) / 10)))
            same = [row for row in leases.values() if row["kind"] == kind]
            cap = min([cap] + [row["limit"] for row in same])
            # Only reserve not-yet-materialized startup costs, not all active RSS again.
            pending = sum(row["reserve"] for row in leases.values()
                          if 0 <= now - row["at"] < 10)
            room = (sample.available is None or kind == "text" or
                    sample.available - pending >= 256 * MIB + reserve)
            committed = (sample.commit_available is None or
                         sample.commit_available >= reserve + 128 * MIB)
            heavy = kind in {"image", "browser", "service"} or (kind == "embedding" and reserve >= 384 * MIB)
            admitted = (len(same) < cap and room and committed and
                        not (pressure == "critical" and heavy))
            if admitted:
                leases[identifier] = {"pid": os.getpid(), "identity": identity, "kind": kind,
                                      "at": now, "reserve": reserve, "limit": max(1, int(limit))}
            state["leases"] = leases
            write_state(root / "state.json", state)
        if admitted:
            break
        elapsed = time.monotonic() - started
        if elapsed - last_message >= 15:
            message = ("[auto] stage=资源等待 | in_progress | RESOURCE_MEMORY_WAIT "
                       f"kind={kind} pressure={pressure} waited_s={elapsed:.0f}")
            (progress or print)(message)
            _record(kind, {"pressure": pressure, "waiting_seconds": round(elapsed),
                           **asdict(sample)})
            last_message = elapsed
        if stop_event is not None:
            stop_event.wait(.5)
        else:
            time.sleep(.5)
    token = _nested.set(_nested.get() | {kind})
    admitted_at = time.monotonic()
    with _local_lock:
        _active += 1
        if _observer is None or not _observer.is_alive():
            _observer = threading.Thread(target=_observe, name="memory-observer", daemon=True)
            _observer.start()
    try:
        if stop_event is not None and stop_event.is_set():
            from concurrent.futures import CancelledError
            raise CancelledError("stopped before memory-admitted operation")
        if time.monotonic() - started >= 1:
            (progress or print)(f"[auto] stage=资源等待 | success | kind={kind} resumed")
            _record(kind, {"event": "admitted", "waited_seconds": round(admitted_at - started, 3),
                           "pressure": pressure, **asdict(sample)})
        yield
    finally:
        _nested.reset(token)
        with _local_lock:
            _active -= 1
        with shared_lock(root / "state.lock"):
            state = read_state(root / "state.json")
            state.get("leases", {}).pop(identifier, None)
            write_state(root / "state.json", state)


atexit.register(_observer_stop.set)


def memory_bound(kind: str, *, limit: int = 1):
    def decorate(function):
        @wraps(function)
        def invoke(*args, **kwargs):
            with memory_slot(kind, limit=limit):
                return function(*args, **kwargs)
        return invoke
    return decorate

