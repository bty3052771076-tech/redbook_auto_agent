"""Run agent and embedded-tool tests in a disposable PostgreSQL database on E:."""
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

import psycopg
from psycopg import sql


def pytest_sessionstart(session):
    if getattr(session.config, "_agent_test_database", None) is not None:
        return
    runtime = Path(os.getenv("REDBOOK_RUNTIME_ROOT") or "E:/AI/codex/redbook_runtime")
    credentials = json.loads((runtime / "data/knowledge/postgresql-local/connection.json").read_text(encoding="utf-8"))
    database = "redbook_agent_test_" + uuid4().hex
    admin = dict(host=credentials["host"], port=credentials["port"],
                 dbname="postgres", user=credentials["admin_user"], password=credentials["admin_password"])
    scratch = runtime / "data/tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="pytest-agent-", dir=scratch))
    with psycopg.connect(**admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
            sql.Identifier(database), sql.Identifier(credentials["migration_user"])))
    session.config._agent_test_database = (admin, database, root)
    with psycopg.connect(**{**admin, "dbname": database}, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION vector WITH SCHEMA public")
        conn.execute("CREATE EXTENSION pg_trgm WITH SCHEMA public")
    path = root / "data/knowledge/postgresql-local/connection.json"
    path.parent.mkdir(parents=True)
    isolated = {**credentials, "database": database}
    # Local pg_hba permits the administrator to access disposable databases.
    for role in ("migration", "app"):
        isolated[role + "_user"] = credentials["admin_user"]
        isolated[role + "_password"] = credentials["admin_password"]
    path.write_text(json.dumps(isolated), encoding="utf-8")
    os.environ["REDBOOK_RUNTIME_ROOT"] = str(root)
    os.environ["KNOWLEDGE_DB_CREDENTIALS"] = str(path)
    os.environ.setdefault("KNOWLEDGE_EMBEDDING_CACHE", str(runtime / "data/models/fastembed"))
    (root / ".env.gui").write_text("MINIMAX_TOKEN_PLAN_API_KEY=offline-test-no-provider-call\n", encoding="utf-8")
    from src.knowledge.store import KnowledgeStore
    from src.agent.postgres_checkpoint import setup_postgres_checkpointer
    store = KnowledgeStore.from_env()
    store.ensure_schema()
    setup_postgres_checkpointer(store)


def pytest_sessionfinish(session, exitstatus):
    state = getattr(session.config, "_agent_test_database", None)
    if state is None:
        return
    del session.config._agent_test_database
    from src.knowledge.store import _POOLS

    admin, database, root = state
    for pool in _POOLS.values():
        pool.close()
    _POOLS.clear()
    with psycopg.connect(**admin, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
    (root / "data/knowledge/postgresql-local/connection.json").unlink(missing_ok=True)
