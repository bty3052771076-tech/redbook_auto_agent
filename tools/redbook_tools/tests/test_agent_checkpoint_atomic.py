import ctypes
import json
import os
from pathlib import Path

import pytest

from src.agent import editorial_agent as agent


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_checkpoint_retries_transient_windows_replace(monkeypatch, tmp_path, winerror):
    real_replace = Path.replace
    calls, delays = [], []

    def replace(source, target):
        calls.append(target)
        if len(calls) < 3:
            error = PermissionError("reader still holds the checkpoint")
            error.winerror = winerror
            raise error
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(agent.time, "sleep", delays.append)
    checkpoint = agent._save_checkpoint({"run_id": "atomic-test", "status": "paused"}, tmp_path)
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["status"] == "paused"
    assert len(calls) == 3
    assert delays and sum(delays) < 1


def test_checkpoint_permanent_replace_error_preserves_old_file(monkeypatch, tmp_path):
    target = tmp_path / "checkpoint.json"
    target.write_text('{"status":"old"}', encoding="utf-8")
    calls, delays = [], []

    def denied(source, destination):
        calls.append(destination)
        error = PermissionError("permanent permission failure")
        error.winerror = 5
        raise error

    monkeypatch.setattr(Path, "replace", denied)
    monkeypatch.setattr(agent.time, "sleep", delays.append)
    with pytest.raises(PermissionError, match="permanent"):
        agent._save_checkpoint({"run_id": "atomic-test", "status": "new"}, tmp_path)
    assert 1 < len(calls) <= 8
    assert sum(delays) < 1
    assert json.loads(target.read_text(encoding="utf-8"))["status"] == "old"


def test_checkpoint_other_filesystem_errors_are_not_retried(monkeypatch, tmp_path):
    delays = []

    def denied(source, destination):
        raise PermissionError("non-sharing failure")

    monkeypatch.setattr(Path, "replace", denied)
    monkeypatch.setattr(agent.time, "sleep", delays.append)
    with pytest.raises(PermissionError, match="non-sharing"):
        agent._save_checkpoint({"run_id": "atomic-test"}, tmp_path)
    assert delays == []


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows file handle")
def test_checkpoint_survives_real_windows_reader_handle(monkeypatch, tmp_path):
    from ctypes import wintypes

    target = tmp_path / "checkpoint.json"
    target.write_text('{"status":"old"}', encoding="utf-8")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(target), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value, ctypes.get_last_error()
    real_replace, calls = Path.replace, []

    def replace(source, destination):
        nonlocal handle
        calls.append(destination)
        try:
            return real_replace(source, destination)
        except PermissionError:
            assert kernel.CloseHandle(handle)
            handle = None
            raise

    monkeypatch.setattr(Path, "replace", replace)
    try:
        checkpoint = agent._save_checkpoint({"run_id": "atomic-test", "status": "new"}, tmp_path)
        assert len(calls) == 2
        assert json.loads(checkpoint.read_text(encoding="utf-8"))["status"] == "new"
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)
