from pathlib import Path
import os
import pytest

from backend.settings import configure_runtime


@pytest.fixture(autouse=True)
def isolate_runtime_environment(monkeypatch):
    for key in ("REDBOOK_RUNTIME_ROOT", "KNOWLEDGE_DB_CREDENTIALS", "KNOWLEDGE_EMBEDDING_CACHE",
                "ALLOW_PAID_LLM_FALLBACK", "WORLDMONITOR_DIR", "RSSHUB_DIR", "GLOBAL_MAP_BASEMAP_PATH"):
        if key in os.environ:
            monkeypatch.setenv(key, os.environ[key])
        else:
            monkeypatch.delenv(key, raising=False)


def test_agent_binds_its_own_tool_copies(monkeypatch, tmp_path):
    credentials = tmp_path / "data/knowledge/postgresql-local/connection.json"
    credentials.parent.mkdir(parents=True)
    credentials.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("REDBOOK_RUNTIME_ROOT", str(tmp_path))
    for key in ("WORLDMONITOR_DIR", "RSSHUB_DIR", "GLOBAL_MAP_BASEMAP_PATH"):
        monkeypatch.delenv(key, raising=False)
    assert configure_runtime() == tmp_path.resolve()
    import os
    tools = Path(__file__).resolve().parents[1] / "tools"
    assert Path(os.environ["WORLDMONITOR_DIR"]) == tools / "worldmonitor"
    assert Path(os.environ["RSSHUB_DIR"]) == tools / "RSSHub"
    assert Path(os.environ["GLOBAL_MAP_BASEMAP_PATH"]) == tools / "worldmonitor/public/data/countries.geojson"


def test_explicit_tool_override_is_retained(monkeypatch, tmp_path):
    credentials = tmp_path / "data/knowledge/postgresql-local/connection.json"
    credentials.parent.mkdir(parents=True)
    credentials.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("REDBOOK_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("RSSHUB_DIR", str(tmp_path / "custom"))
    configure_runtime()
    import os
    assert os.environ["RSSHUB_DIR"] == str(tmp_path / "custom")
