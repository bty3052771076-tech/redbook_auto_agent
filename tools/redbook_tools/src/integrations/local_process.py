"""Task leases for optional local services; borrowed processes are never stopped."""
from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import subprocess
import time
from uuid import uuid4

from src.workflow.resources import (
    coordination_root, memory_slot, process_alive, process_identity,
    process_image, read_state, shared_lock, write_state,
)


def terminate_owned(record: dict) -> bool:
    pid, identity = record.get("pid"), record.get("identity")
    if not pid or not identity or process_identity(pid) != identity:
        return False
    image = process_image(pid)
    if not image or os.path.normcase(image) != os.path.normcase(record.get("executable", "")):
        return False
    if os.name == "nt":
        result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10)
        if result.returncode:
            return False
    else:
        # Only our own process group is recorded and signalled.
        import signal
        if record.get("group") != pid:
            return False
        os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while process_alive(pid, identity) and time.monotonic() < deadline:
        time.sleep(.1)
    return not process_alive(pid, identity)


class LocalServiceLease:
    def __init__(self, name: str, *, port: int):
        if not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError("invalid service name")
        self.path = coordination_root() / "data/runs/local-services" / f"{name}-{int(port)}.json"
        self.key = uuid4().hex
        self.member = {"pid": os.getpid(), "identity": process_identity(os.getpid())}
        self.registered = False
        self.started = False

    def _reuse(self, health) -> bool:
        if not health():
            return False
        record = read_state(self.path)
        if record.get("identity") and process_identity(record.get("pid", 0)) == record["identity"]:
            members = {key: row for key, row in record.get("members", {}).items()
                       if process_alive(row["pid"], row.get("identity"))}
            members[self.key] = self.member
            record["members"] = members
            write_state(self.path, record)
            if not self.registered:
                atexit.register(self.release)
            self.registered = True
        return True

    def acquire(self, command: list[str], *, cwd: Path, health, timeout: float = 20,
                env: dict | None = None, stdout=None, start_allowed: bool = True) -> bool:
        with shared_lock(self.path.with_suffix(".lock")):
            if self._reuse(health):
                return True
            if not start_allowed:
                return False
        with memory_slot("service"), shared_lock(self.path.with_suffix(".lock")):
            if self._reuse(health):
                return True
            previous = read_state(self.path)
            if previous.get("pid") and process_alive(previous["pid"], previous.get("identity")):
                return False
            executable = shutil.which(command[0]) or str(Path(command[0]).resolve())
            process = subprocess.Popen(
                [executable, *command[1:]], cwd=str(cwd), env=env,
                stdin=subprocess.DEVNULL, stdout=stdout if stdout is not None else subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                start_new_session=os.name != "nt",
            )
            record = {"pid": process.pid, "identity": process_identity(process.pid),
                      "executable": process_image(process.pid) or executable,
                      "group": process.pid if os.name != "nt" else None,
                      "members": {self.key: self.member}}
            write_state(self.path, record)
            self.registered, self.started = True, True
            atexit.register(self.release)
            deadline = time.monotonic() + timeout
            while process.poll() is None and time.monotonic() < deadline:
                if health():
                    return True
                time.sleep(.25)
            # Still inside the owner lock; never recurse through release().
            stopped = process.poll() is not None or terminate_owned(record)
            if stopped:
                process.wait(timeout=5)
                self.registered = False
                atexit.unregister(self.release)
                write_state(self.path, {})
            else:
                write_state(self.path, record)
            return False

    def release(self) -> None:
        if not self.registered:
            return
        try:
            with shared_lock(self.path.with_suffix(".lock")):
                record = read_state(self.path)
                members = {key: row for key, row in record.get("members", {}).items()
                           if key != self.key and process_alive(row["pid"], row.get("identity"))}
                record["members"] = members
                if not members:
                    if terminate_owned(record) or not process_alive(record.get("pid", 0), record.get("identity")):
                        record = {}
                write_state(self.path, record)
                self.registered = False
                atexit.unregister(self.release)
        except (OSError, subprocess.SubprocessError):
            # Preserve the journal when ownership/termination cannot be confirmed.
            pass


def release_process_services(pid: int, identity: str | None) -> None:
    if not identity:
        return
    directory = coordination_root() / "data/runs/local-services"
    for path in directory.glob("*.json"):
        with shared_lock(path.with_suffix(".lock")):
            record = read_state(path)
            members = record.get("members", {})
            if not any(row.get("pid") == pid and row.get("identity") == identity for row in members.values()):
                continue
            record["members"] = {key: row for key, row in members.items()
                                  if not (row.get("pid") == pid and row.get("identity") == identity)
                                  and process_alive(row["pid"], row.get("identity"))}
            if not record["members"] and terminate_owned(record):
                record = {}
            write_state(path, record)

def terminate_task_tree(pid: int, identity: str | None) -> None:
    """Stop a requested task, preserving services leased by another live task."""
    from contextlib import ExitStack
    from src.workflow.resources import process_tree

    if not identity or process_identity(pid) != identity:
        return
    parents = process_tree()
    descendants = {pid}
    identities = {pid: identity}
    while True:
        found = set()
        for child, parent in parents.items():
            if parent not in descendants or child in descendants:
                continue
            birth = process_identity(child)
            parent_birth = identities.get(parent)
            if birth and parent_birth and int(birth) >= int(parent_birth):
                identities[child] = birth
                found.add(child)
        if found <= descendants:
            break
        descendants.update(found)
    with ExitStack() as stack:
        protected = set()
        for path in sorted((coordination_root() / "data/runs/local-services").glob("*.json")):
            stack.enter_context(shared_lock(path.with_suffix(".lock")))
            record = read_state(path)
            if record.get("pid") in descendants and any(
                not (row.get("pid") == pid and row.get("identity") == identity)
                and process_alive(row["pid"], row.get("identity"))
                for row in record.get("members", {}).values()
            ):
                protected.add(record["pid"])
        while True:
            found = {child for child, parent in parents.items() if parent in protected}
            if found <= protected:
                break
            protected.update(found)
        # Stop the root first so no new children are admitted during cancellation.
        for child in [pid, *sorted(descendants - {pid}, reverse=True)]:
            birth = identities.get(child)
            if child in protected or not birth or process_identity(child) != birth:
                continue
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(child), "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10)
            else:
                import signal
                os.kill(child, signal.SIGTERM)

