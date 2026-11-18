"""Real Windows sleep trees and disposable PostgreSQL; no model or platform work."""

from contextlib import ExitStack
import json
import os
import socket
import subprocess
import sys
from threading import enumerate as enumerate_threads
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.dialogue import DialogueService
from backend.dialogue_store import DialogueStore
from src.integrations.local_process import terminate_task_tree
from src.knowledge.store import KnowledgeStore
from src.workflow.resources import process_alive, process_identity, process_tree

if os.name == 'nt':
    import _winapi


pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Uses Windows child-process wait handles')

SLEEP = 'import time; time.sleep(120)'
# Bypass the Windows venv launcher so Popen.pid is the actual sleeping interpreter.
SLEEP_PYTHON = sys._base_executable
TREE = '''
import json
import os
from pathlib import Path
import subprocess
import sys

if sys.stdin.readline().strip() != 'start':
    sys.exit(2)
child = subprocess.Popen([sys.executable, '-B', '-c', sys.argv[3]],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
try:
    Path(sys.argv[1]).write_text(json.dumps({
        'token': sys.argv[2], 'parent': os.getpid(), 'child': child.pid}), encoding='utf-8')
    child.wait(timeout=120)
finally:
    if child.poll() is None:
        child.terminate()
    child.wait(timeout=5)
'''


@pytest.fixture
def owned_process_host(tmp_path, monkeypatch):
    assert tmp_path.resolve().drive.upper() == 'E:', 'Test artifacts must stay on E:'
    monkeypatch.setenv('REDBOOK_RESOURCE_ROOT', str(tmp_path))

    def forbidden_network(*args, **kwargs):
        pytest.fail('Owned-process acceptance must not open network connections')

    monkeypatch.setattr(socket.socket, 'connect', forbidden_network)
    monkeypatch.setattr(socket, 'create_connection', forbidden_network)
    store = DialogueStore(KnowledgeStore.from_env(), namespace='dialogue_owned_' + uuid4().hex)
    with store.knowledge.connection() as conn:
        database = conn.execute('SELECT current_database() AS name').fetchone()['name']
    assert database.startswith('redbook_agent_test_'), 'Refusing a non-disposable database'
    store.setup()
    cid = uuid4().hex
    store.conversations.save({'id': cid, 'messages': [], 'plans': [], 'runs': [], 'created_at': time.time()})
    current = SimpleNamespace(root=tmp_path, conversation_store=store.conversations, redact=lambda value: value)
    service = DialogueService(current, store=store, directory=tmp_path / 'dialogue')
    store.session(cid)
    return service, store, cid


def _wait_manifest(path, root, token):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        assert root.poll() is None, 'Harmless test parent exited before child registration'
        try:
            row = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            time.sleep(.02)
            continue
        assert row['token'] == token and row['parent'] == root.pid
        assert process_tree().get(row['child']) == root.pid
        return row['child']
    pytest.fail('Harmless test child did not register within five seconds')


def _wait_child(handle, timeout):
    result = _winapi.WaitForSingleObject(handle, max(0, int(timeout * 1000)))
    if result != _winapi.WAIT_OBJECT_0:
        raise subprocess.TimeoutExpired('owned test child handle', timeout)


def _reap_child(handle):
    try:
        if _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_TIMEOUT:
            _winapi.TerminateProcess(handle, 1)
        _wait_child(handle, 5)
    finally:
        _winapi.CloseHandle(handle)


def _reap_process(process, identity=None):
    try:
        if process.poll() is None and identity:
            terminate_task_tree(process.pid, identity)
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    finally:
        if process.stdin is not None:
            process.stdin.close()


def _join_stop_threads(previous):
    for thread in set(enumerate_threads()) - previous:
        if thread.name == 'dialogue-stop':
            thread.join(timeout=10)
            assert not thread.is_alive(), 'Dialogue stop thread survived test-process cleanup'


@pytest.mark.parametrize('action,terminal', [('pause', 'paused'), ('cancel', 'cancelled')])
def test_control_really_stops_owned_sleep_tree_not_unrelated_sleep(owned_process_host, tmp_path, action, terminal):
    service, store, cid = owned_process_host
    cleanup = ExitStack()
    cleanup.callback(_join_stop_threads, set(enumerate_threads()))
    try:
        root = subprocess.Popen([SLEEP_PYTHON, '-B', '-c', TREE,
            str(tmp_path / 'child.json'), cid, SLEEP], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        root_identity = None
        cleanup.callback(lambda: _reap_process(root, root_identity))
        root_identity = process_identity(root.pid)
        assert root_identity is not None
        # Child creation is admitted only after the parent birth identity is captured.
        root.stdin.write(b'start\n')
        root.stdin.flush()
        root.stdin.close()
        child_pid = _wait_manifest(tmp_path / 'child.json', root, cid)
        child_identity = process_identity(child_pid)
        assert child_identity is not None
        # Keep a handle to the original child, even after its PID disappears or is reused.
        child_handle = _winapi.OpenProcess(_winapi.SYNCHRONIZE | 0x0001, False, child_pid)
        if process_identity(child_pid) != child_identity:
            _winapi.CloseHandle(child_handle)
            pytest.fail('Test child identity changed before acquiring its wait handle')
        cleanup.callback(_reap_child, child_handle)

        unrelated = subprocess.Popen([SLEEP_PYTHON, '-B', '-c', SLEEP], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        cleanup.callback(_reap_process, unrelated)
        unrelated_identity = process_identity(unrelated.pid)
        assert unrelated_identity is not None
        assert unrelated.pid not in {root.pid, child_pid}
        assert process_tree().get(unrelated.pid) == os.getpid()
        execution = uuid4().hex
        service.processes[cid] = (execution, root, root_identity)
        store.transition(cid, state='running', execution_id=execution, pid=root.pid,
                         process_identity=root_identity, permissions={'fixture_sleep': {'remaining': 1}})
        running, admitted = store.claim_action(cid, execution, 'sleep-call', 'fixture_sleep', {})
        assert admitted
        retained, admitted = store.claim_action(cid, execution, 'retained-call', 'fixture_local_artifact', {})
        assert admitted
        retained = store.finish_action(retained['id'], 'completed', {'artifact_id': 'isolated-test-artifact'})

        started = time.monotonic()
        response = service.control(cid, action, execution)
        control_seconds = time.monotonic() - started
        assert control_seconds < 1, f'Control blocked for {control_seconds:.3f}s'
        assert response['state'] in {'stopping', terminal}
        assert response['execution_id'] == execution
        root.wait(timeout=max(.01, 5 - (time.monotonic() - started)))
        _wait_child(child_handle, max(.01, 5 - (time.monotonic() - started)))
        tree_seconds = time.monotonic() - started
        assert tree_seconds < 5, f'Owned process tree took {tree_seconds:.3f}s to exit'
        assert not process_alive(root.pid, root_identity)
        assert not process_alive(child_pid, child_identity)

        deadline = time.monotonic() + 3
        while store.session(cid)['state'] != terminal and time.monotonic() < deadline:
            time.sleep(.02)
        final = store.session(cid)
        terminal_seconds = time.monotonic() - started
        assert final['state'] == terminal, store.snapshot(cid)
        assert final['pid'] is None and final['process_identity'] is None
        assert final['control_epoch'] == 1
        assert final['permissions'] == ({} if action == 'cancel' else {'fixture_sleep': {'remaining': 1}})
        actions = {row['id']: row for row in store.actions(cid)}
        assert actions[running['id']]['status'] == 'uncertain'
        assert actions[retained['id']] == retained
        assert unrelated.poll() is None and process_identity(unrelated.pid) == unrelated_identity

        repeated = time.monotonic()
        again = service.control(cid, action, execution)
        repeat_seconds = time.monotonic() - repeated
        assert repeat_seconds < 1
        assert again['state'] == terminal and again['execution_id'] == execution
        assert store.session(cid)['control_epoch'] == final['control_epoch']
        assert unrelated.poll() is None and process_identity(unrelated.pid) == unrelated_identity
        print(f'REAL_OWNED_PROCESS action={action} control_s={control_seconds:.4f} '
              f'tree_exit_s={tree_seconds:.4f} terminal_s={terminal_seconds:.4f} '
              f'repeat_s={repeat_seconds:.4f} unrelated_alive=True', flush=True)
    finally:
        cleanup.close()
