"""Cold-start outage tests use an owned socket, never stop production PostgreSQL."""
import socket
from uuid import uuid4

from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

from backend import app as module
from apps.web_service import Workbench
from src.agent.conversation_store import PostgresConversationStore
from src.knowledge.store import KnowledgeStore


def test_cold_start_without_postgres_stays_readonly_and_recovers(tmp_path, monkeypatch):
    knowledge = KnowledgeStore.from_env()
    namespace = 'cold_outage_' + uuid4().hex
    original_pools = knowledge._pool_by_role.copy()
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    config = knowledge._credentials()
    config.update(host='127.0.0.1', port=listener.getsockname()[1])
    pool = ConnectionPool(make_conninfo(**config, connect_timeout=1), min_size=0, max_size=1,
                          timeout=1, open=True)
    monkeypatch.setattr(KnowledgeStore, 'from_env', lambda **kwargs: knowledge)
    monkeypatch.setattr(module.app.state, 'service', None)
    monkeypatch.setattr(module.app.state, 'review_schema_ready', False, raising=False)
    monkeypatch.setattr(module, 'Workbench', lambda **kwargs: Workbench(tmp_path,
        conversation_store=PostgresConversationStore(knowledge, namespace=namespace)))
    monkeypatch.setattr(knowledge, '_pool_by_role', {'app':pool, 'migration':pool})
    try:
        with TestClient(module.app, base_url='http://127.0.0.1:8786') as test:
            assert test.post('/api/session', headers={'x-workbench':'1'}).status_code == 200
            result = test.get('/api/capabilities')
            assert result.status_code == 200 and result.json()['read_only'] is True
            blocked = test.post('/api/conversations', headers={'x-workbench':'1'}, json={'title':'do not save'})
            assert blocked.status_code == 503 and blocked.json()['code'] == 'POSTGRES_UNAVAILABLE'
            assert config['password'] not in blocked.text
            assert module.app.state.service is None
            monkeypatch.setattr(knowledge, '_pool_by_role', original_pools)
            assert test.get('/api/conversations').json() == {'rows':[]}
            assert module.app.state.review_schema_ready is True
            assert module.app.state.service.conversation_store.namespace == namespace
    finally:
        monkeypatch.setattr(knowledge, '_pool_by_role', original_pools)
        pool.close()
        listener.close()
