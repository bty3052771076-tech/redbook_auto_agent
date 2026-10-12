from uuid import uuid4

import pytest

from src.knowledge.embeddings import index_pending_documents
from src.knowledge.models import KnowledgeDocument
from src.knowledge.service import knowledge_context, prepare_local_knowledge_snapshot
from src.knowledge.store import KnowledgeStore


class Embedder:
    def embed_query(self, value):
        return [1.0] + [0.0] * 383

    def embed_documents(self, values):
        return [self.embed_query(value) for value in values]


@pytest.fixture
def knowledge(monkeypatch):
    store = KnowledgeStore.from_env()
    store.ensure_schema()
    monkeypatch.setattr("src.knowledge.embeddings.get_embedding_model", lambda: Embedder())
    token = uuid4().hex
    store.upsert_document(KnowledgeDocument(record_id=token, record_type="post",
        title="Model release", body="Vendor released a new model", allowed_purposes=("duplicate_reference",)))
    index_pending_documents(store, account_namespace="local")
    foreign = "unindexed_" + token
    store.upsert_document(KnowledgeDocument(record_id=token, record_type="news",
        account_namespace=foreign, title="Unindexed test material", body="Pending vector"))
    try:
        yield store, token, foreign
    finally:
        with store.connection() as conn:
            conn.execute("DELETE FROM knowledge.documents WHERE record_id=%s", (token,))


def test_unindexed_other_namespace_does_not_block_task_context(knowledge):
    store, token, foreign = knowledge
    assert store.status()["index_ready"] is False
    result = knowledge_context(job_kind="daily_ai_digest", query="Model release", store_factory=lambda: store)
    assert result["knowledge_status"] == "ready"
    assert result["knowledge_documents"] == 1
    assert result["knowledge_hits"][0]["record_id"] == token


def test_snapshot_does_not_index_unrelated_namespace(knowledge, tmp_path):
    store, token, foreign = knowledge
    result = prepare_local_knowledge_snapshot(data_root=tmp_path, store=store)
    assert result["knowledge_status"] == "ready"
    assert store.index_progress(account_namespace=foreign)["pending_documents"] == 1


def test_task_reports_pending_index_without_claiming_database_is_offline(knowledge):
    store, token, foreign = knowledge
    store.upsert_document(KnowledgeDocument(record_id=token, record_type="post", title="Changed release", body="New version"))
    result = knowledge_context(job_kind="daily_ai_digest", store_factory=lambda: store)
    assert result["knowledge_status"] == "blocked"
    assert result["error_code"] == "KNOWLEDGE_INDEX_NOT_READY"
    assert "1" in result["knowledge_warning"]
    assert "启动本地数据库" not in result["knowledge_warning"]
