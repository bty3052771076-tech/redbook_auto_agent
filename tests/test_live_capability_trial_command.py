from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/verify_live_capability_trial.py'


def test_live_trial_help_has_no_runtime_side_effects(tmp_path):
    result = subprocess.run([sys.executable,str(SCRIPT),'--help','--runtime-root',str(tmp_path)],capture_output=True,text=True,timeout=15)
    assert result.returncode == 0
    assert '--execute-one-draft' in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_live_trial_requires_explicit_opt_in_before_loading_runtime(tmp_path):
    result = subprocess.run([sys.executable,str(SCRIPT),'--runtime-root',str(tmp_path)],capture_output=True,text=True,timeout=15)
    assert result.returncode == 2
    assert 'explicit --execute-one-draft is required' in result.stderr
    assert list(tmp_path.iterdir()) == []
