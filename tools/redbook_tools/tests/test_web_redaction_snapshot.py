from pathlib import Path

from apps.web_service import Workbench, gui


def workbench():
    instance = Workbench.__new__(Workbench)
    instance.root = Path('unused-local-root')
    return instance


def test_redaction_reads_credentials_once_for_large_nested_payload(monkeypatch):
    reads = []
    secret = 'fixture-local-key-abcdefghijk'

    def load(path):
        reads.append(path)
        return {'TEST_API_KEY': secret}

    monkeypatch.setattr(gui, 'load_env_file', load)
    payload = {'posts': [{'body': f'public text {secret}', 'score': 85} for _ in range(200)],
               'api_key': secret, 'other': ['keep', {'cookie': secret, 'body': secret}]}
    result = workbench().redact(payload)
    assert len(reads) == 1
    assert len(result['posts']) == 200
    assert all(row == {'body': 'public text [已隐藏]', 'score': 85} for row in result['posts'])
    assert result['other'] == ['keep', {'body': '[已隐藏]'}]
    assert 'api_key' not in result
    assert payload['posts'][0]['body'] == f'public text {secret}'


def test_redaction_reloads_rotated_credentials_on_next_request(monkeypatch):
    secrets = iter(('fixture-first-key-abcdef', 'fixture-second-key-ghijkl'))
    monkeypatch.setattr(gui, 'load_env_file', lambda _: {'TEST_API_KEY': next(secrets)})
    instance = workbench()
    assert instance.redact({'body': 'fixture-first-key-abcdef'}) == {'body': '[已隐藏]'}
    assert instance.redact({'body': 'fixture-second-key-ghijkl'}) == {'body': '[已隐藏]'}


def test_redaction_keeps_environment_and_inline_secret_filters(monkeypatch):
    monkeypatch.setattr(gui, 'load_env_file', lambda _: {})
    monkeypatch.setenv('TEST_ROTATING_SECRET', 'fixture-process-secret-abcdef')
    result = workbench().redact({
        'raw_text': 'private payload', 'Authorization': 'credential',
        'rows': ['fixture-process-secret-abcdef', 'sk-abcdefghijklmno', 'access_token=example-token'],
        'public': 3,
    })
    assert result == {'rows': ['[已隐藏]', '[已隐藏]', 'access_token=[已隐藏]'], 'public': 3}
