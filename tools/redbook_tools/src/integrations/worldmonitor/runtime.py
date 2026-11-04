from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .client import WorldMonitorClient, WorldMonitorError
from src.integrations.local_process import LocalServiceLease


def _npm_executable() -> str:
    return shutil.which("npm.cmd" if os.name == "nt" else "npm") or "npm"


def _node_executable() -> str:
    return shutil.which("node.exe" if os.name == "nt" else "node") or "node"


@dataclass(frozen=True)
class RuntimeProbe:
    ready: bool
    reused: bool = False
    started: bool = False
    error_code: str = ""
    message: str = ""


class WorldMonitorRuntime:
    """On-demand headless service; shared leases prevent cross-task termination."""

    def __init__(self, client: WorldMonitorClient, *, root: Path | None = None, auto_start: bool = False, port: int = 3000):
        self.client = client
        self.root = Path(root).resolve() if root else None
        self.auto_start = bool(auto_start)
        self.port = int(port)
        self._probe = None
        self._lease = None

    def ensure_ready(self) -> RuntimeProbe:
        if self._probe is not None and self._probe.ready:
            return RuntimeProbe(**{**self._probe.__dict__, "reused": True})
        first_error = None
        def healthy():
            nonlocal first_error
            try:
                self.client.fetch_digest(reuse_cycle=False)
                return True
            except WorldMonitorError as exc:
                first_error = exc
                return False

        launcher = Path(__file__).resolve().parents[3] / "scripts/worldmonitor_headless.mjs"
        can_start = bool(self.auto_start and self.root and
                         (self.root / "package.json").is_file() and launcher.is_file())
        lease = LocalServiceLease("worldmonitor", port=self.port)
        try:
            ready = lease.acquire(
                [_node_executable(), str(launcher), "--root", str(self.root), "--port", str(self.port)],
                cwd=self.root or Path.cwd(), health=healthy, timeout=20,
                env={**os.environ, "BROWSER": "none"}, start_allowed=can_start,
            )
            if ready:
                self._lease = lease
                self._probe = RuntimeProbe(True, started=lease.started, reused=not lease.started)
            else:
                lease.release()
                self._probe = RuntimeProbe(False, error_code=first_error.code if first_error else "WM_NOT_READY",
                                           message=str(first_error or "local service did not become ready"))
        except (OSError, RuntimeError) as exc:
            lease.release()
            self._probe = RuntimeProbe(False, error_code="WM_NOT_READY", message=str(exc))
        return self._probe

    def release(self) -> None:
        if self._lease is not None:
            self._lease.release()
            self._lease = None
        self._probe = None
