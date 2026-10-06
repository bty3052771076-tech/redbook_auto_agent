from fastapi.testclient import TestClient
from backend import app as module


class SourcesWorkbench:
    def __init__(self):
        self.calls = []
    def sources(self):
        return {'rows': [{'source_name': 'official', 'status': 'stale'}], 'check': None}
    def submit(self, request, key):
        self.calls.append((request, key))
        return {'id': 'a' * 32, 'kind': 'check-sources', 'status': 'queued'}
    def redact(self, value):
        return value


def test_source_check_is_authenticated_bounded_and_read_only(monkeypatch):
    current = SourcesWorkbench()
    monkeypatch.setattr(module.app.state, 'service', current)
    with TestClient(module.app, base_url='http://127.0.0.1:8786') as client:
        assert client.get('/api/sources').status_code == 403
        client.cookies.set('redbook_agent', module.app.state.token)
        assert client.get('/api/sources').json()['rows'][0]['status'] == 'stale'
        headers = {'X-Workbench': '1', 'Idempotency-Key': 'test-source-check-0001'}
        assert client.post('/api/sources/check', json={'collection': 'http://evil'}, headers=headers).status_code == 422
        assert client.post('/api/sources/check', json={'collection': 'all', 'max_age_days': 15}, headers=headers).status_code == 422
        assert client.post('/api/sources/check', json={'collection': 'all', 'url': 'http://evil'}, headers=headers).status_code == 422
        result = client.post('/api/sources/check', json={'collection': 'ai_digest', 'max_age_days': 2, 'keywords': '模型发布'}, headers=headers)
        assert result.status_code == 200
        assert current.calls == [({'kind': 'check-sources', 'title': '信源检测', 'collection': 'ai_digest',
                                   'max_age_days': 2, 'keywords': '模型发布'}, 'test-source-check-0001')]
