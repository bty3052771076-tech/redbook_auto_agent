import json
import os
from pathlib import Path


def test_embedded_tests_use_runtime_drive_scratch_and_disposable_postgresql(tmp_path):
    root = Path(os.environ['REDBOOK_RUNTIME_ROOT'])
    assert tmp_path.drive.upper() == root.drive.upper()
    credentials = Path(os.environ['KNOWLEDGE_DB_CREDENTIALS'])
    assert credentials.is_relative_to(root)
    assert json.loads(credentials.read_text(encoding='utf-8'))['database'].startswith('redbook_agent_test_')
