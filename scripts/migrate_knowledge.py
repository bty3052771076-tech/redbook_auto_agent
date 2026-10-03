"""One-time, non-destructive PostgreSQL knowledge-data copy from the old workspace."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import psycopg


RUNTIME = Path(r"E:\AI\codex\redbook_runtime")
BIN = RUNTIME / "data/runtime/postgresql/18.6/pgsql/bin"
TARGET = RUNTIME / "data/knowledge/postgresql-local/connection.json"


def summary(cfg: dict) -> dict[str, object]:
    with psycopg.connect(host=cfg["host"], port=cfg["port"], dbname=cfg["database"],
                         user=cfg["app_user"], password=cfg["app_password"]) as conn:
        row = conn.execute("""
            SELECT count(*), md5(coalesce(string_agg(record_id || ':' || content_hash, '|' ORDER BY record_id), ''))
            FROM knowledge.documents
        """).fetchone()
        chunks = conn.execute("SELECT count(*) FROM knowledge.chunks").fetchone()[0]
    return {"documents": row[0], "document_hash": row[1], "chunks": chunks}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-credentials", type=Path, required=True)
    args = parser.parse_args()
    source_path = args.source_credentials.resolve()
    if source_path == TARGET.resolve() or not source_path.is_file():
        raise RuntimeError("Source credentials must point to the existing, separate database")
    source = json.loads(source_path.read_text(encoding="utf-8"))
    target = json.loads(TARGET.read_text(encoding="utf-8"))
    before = summary(source)
    if summary(target)["documents"]:
        raise RuntimeError("Target already contains knowledge records; refusing an unsafe second import")
    backup_dir = RUNTIME / "data/knowledge/backups/postgresql"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"legacy-knowledge-{datetime.now():%Y%m%d-%H%M%S}.dump"
    source_env = {**os.environ, "PGPASSWORD": source["admin_password"]}
    target_env = {**os.environ, "PGPASSWORD": target["admin_password"]}
    subprocess.run([
        str(BIN / "pg_dump.exe"), "-Fc", "--data-only", "-n", "knowledge",
        "--exclude-table=knowledge.schema_migrations", "-h", source["host"],
        "-p", str(source["port"]), "-U", source["admin_user"], "-d", source["database"],
        "-f", str(backup),
    ], env=source_env, check=True)
    subprocess.run([
        str(BIN / "pg_restore.exe"), "--exit-on-error", "--single-transaction",
        "--no-owner", "--no-privileges", "--data-only", "-n", "knowledge",
        "-h", target["host"], "-p", str(target["port"]), "-U", target["admin_user"],
        "-d", target["database"], str(backup),
    ], env=target_env, check=True)
    after = summary(target)
    if before != after:
        raise RuntimeError(f"Knowledge copy mismatch: source={before}, target={after}")
    report = {"source": str(source_path), "target": str(TARGET), "backup": str(backup), "summary": after}
    (backup_dir / "knowledge-migration-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Knowledge import verified: documents={after['documents']} chunks={after['chunks']}")


if __name__ == "__main__":
    main()
