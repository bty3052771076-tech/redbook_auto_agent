"""Start RSSHub only on demand and release owned service leases on task exit."""
import os
import socket
import sys
from http.client import HTTPConnection, HTTPException
from pathlib import Path

from src.integrations.local_process import LocalServiceLease

ROOT = Path(__file__).resolve().parent
_CODE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = _CODE_ROOT.parent.parent if _CODE_ROOT.name == "redbook_tools" and _CODE_ROOT.parent.name == "tools" else _CODE_ROOT
RSSHUB_DIR = Path(os.getenv("RSSHUB_DIR") or PROJECT_ROOT / "tools/RSSHub")
RSSHUB_PORT = int(os.getenv("RSSHUB_PORT", "1200"))
RSSHUB_URL = f"http://127.0.0.1:{RSSHUB_PORT}"
_lease = None


def rsshub_alive(timeout_s: float = 1.0) -> bool:
    connection = HTTPConnection("127.0.0.1", RSSHUB_PORT, timeout=timeout_s)
    try:
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        return response.status == 200 and response.read(3) == b"ok"
    except (OSError, HTTPException):
        return False
    finally:
        connection.close()


def start_rsshub_if_needed() -> bool:
    global _lease
    alive = rsshub_alive()
    if not alive:
        if not (RSSHUB_DIR / "dist/index.mjs").is_file():
            return False
        try:
            with socket.create_connection(("127.0.0.1", RSSHUB_PORT), timeout=.2):
                print("[rsshub] local port occupied but unhealthy; skipping optional source")
                return False
        except OSError:
            pass
    log_path = Path(os.getenv("REDBOOK_RUNTIME_ROOT") or PROJECT_ROOT) / "data/logs/rsshub/stdout.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lease = _lease or LocalServiceLease("rsshub", port=RSSHUB_PORT)
    try:
        with log_path.open("ab") as log:
            ready = lease.acquire(
                ["node", "dist/index.mjs"], cwd=RSSHUB_DIR, health=rsshub_alive,
                timeout=15, stdout=log,
                env={**os.environ, "NODE_ENV": "production", "LISTEN_INADDR_ANY": "0",
                     "PORT": str(RSSHUB_PORT)},
            )
        if ready:
            _lease = lease
            os.environ.setdefault("AI_DIGEST_RSSHUB_BASE_URL", RSSHUB_URL)
        return ready
    except (OSError, RuntimeError):
        lease.release()
        return False


def stop_rsshub_if_owned() -> None:
    global _lease
    if _lease is not None:
        _lease.release()
        _lease = None


if __name__ == "__main__":
    ok = start_rsshub_if_needed()
    print(f"[rsshub] local service: {'ready' if ok else 'unavailable'} at {RSSHUB_URL}")
    sys.exit(0)
