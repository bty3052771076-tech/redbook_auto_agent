import os
from uuid import uuid4

import pytest

from src.knowledge.embeddings import index_pending_documents
from src.knowledge.models import KnowledgeDocument
from src.knowledge.store import KnowledgeStore


@pytest.mark.skipif(os.getenv("REDBOOK_TEST_POSTGRES") != "1", reason="explicit local PostgreSQL integration test")
def test_empty_audit_record_is_preserved_but_does_not_block_index_readiness():
    store = KnowledgeStore.from_env()
    namespace = "test-empty-" + uuid4().hex
    doc = KnowledgeDocument(record_id="empty", record_type="post", account_namespace=namespace,
                            title=" \t", body="\r\n", status="failed", allowed_purposes=("operational_case",))
    try:
        before = store.status()
        before_progress = store.index_progress()
        store.upsert_documents([doc])
        assert not any(row["account_namespace"] == namespace
                       for row in store.pending_documents(limit=before["documents"] + 1))
        status = store.status()
        assert status["documents"] == before["documents"] + 1
        assert status["empty_documents"] == before["empty_documents"] + 1
        assert status["indexed_documents"] == before["indexed_documents"]
        assert status["index_ready"] == before["index_ready"]
        progress = store.index_progress()
        assert progress["pending_documents"] == before_progress["pending_documents"]
        assert progress["pending_documents"] == progress["documents"] - progress["indexed_documents"] - progress["empty_documents"]
        assert store.get("empty", account_namespace=namespace)["status"] == "failed"
        with store.connection() as conn:
            assert conn.execute("SELECT count(*) AS n FROM knowledge.chunks c JOIN knowledge.documents d ON d.id=c.document_id WHERE d.account_namespace=%s", (namespace,)).fetchone()["n"] == 0
    finally:
        with store.connection() as conn:
            conn.execute("DELETE FROM knowledge.documents WHERE account_namespace=%s", (namespace,))


@pytest.mark.parametrize("body", ["", "indexable text"])
def test_index_worker_detects_a_non_advancing_version_instead_of_spinning(body):
    document = {"record_id": "stuck", "account_namespace": "test", "content_hash": "version", "title": "", "body": body}

    class Store:
        def __init__(self):
            self.calls = 0

        def pending_documents(self, **kwargs):
            self.calls += 1
            if self.calls > 2:
                raise AssertionError("worker spun on the same uncommitted version")
            return [document]

        def upsert_chunks(self, *args, **kwargs):
            return 0

    class Embedder:
        def embed_documents(self, texts):
            return [[0.0] * 384 for _ in texts]

    with pytest.raises(RuntimeError, match="KNOWLEDGE_INDEX_NOT_READY.*no progress"):
        index_pending_documents(Store(), embedder=Embedder())
