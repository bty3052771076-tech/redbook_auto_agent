from types import SimpleNamespace

import pytest

from src.publish.delivery_state import DeliveryStateStore


def test_uncertain_draft_requires_two_complete_same_profile_observations(tmp_path):
    from src.publish.draft_recovery import reconcile_uncertain_draft
    store = DeliveryStateStore.in_memory()
    action = store.prepare_action(dict(account_id='account', profile_key=str(tmp_path),
        post_id='p', content_version='v', action='save_draft', visibility='unknown'))
    action = store.mark_submitting(action.action_id, expected_version=action.version)
    post = SimpleNamespace(id='p', title='Expected title')
    calls = []

    def scan(**kw):
        calls.append('scan')
        return {'complete': True, 'enumeration_verified': True, 'items': [],
                'profile_dir': str(tmp_path), 'errors': []}

    outcome = reconcile_uncertain_draft(post, action, store, collect=scan,
        verify=lambda *a, **kw: pytest.fail('absent title cannot be opened'), evidence_dir=tmp_path)
    assert outcome.status == 'prepared'
    assert calls == ['scan', 'scan']
    assert outcome.attempts == 1
    assert outcome.evidence_ref


@pytest.mark.parametrize('change', [
    {'complete': False}, {'enumeration_verified': False}, {'errors': ['timeout']},
    {'profile_dir': 'E:/wrong-profile'},
])
def test_incomplete_inventory_never_allows_repeat_write(tmp_path, change):
    from src.publish.draft_recovery import reconcile_uncertain_draft
    store = DeliveryStateStore.in_memory()
    action = store.prepare_action(dict(account_id='account', profile_key=str(tmp_path),
        post_id='p', content_version='v', action='save_draft', visibility='unknown'))
    action = store.mark_submitting(action.action_id, expected_version=action.version)
    inventory = {'complete': True, 'enumeration_verified': True, 'items': [],
                 'profile_dir': str(tmp_path), 'errors': [], **change}
    result = reconcile_uncertain_draft(SimpleNamespace(id='p', title='Expected'), action, store,
        collect=lambda **kw: inventory, verify=lambda *a,**kw: pytest.fail('not found'), evidence_dir=tmp_path)
    assert result.status in {'submitting', 'uncertain'}


def test_existing_draft_is_verified_without_another_save(tmp_path):
    from src.publish.draft_recovery import reconcile_uncertain_draft
    store = DeliveryStateStore.in_memory()
    action = store.prepare_action(dict(account_id='account', profile_key=str(tmp_path),
        post_id='p', content_version='v', action='save_draft', visibility='unknown'))
    action = store.mark_submitting(action.action_id, expected_version=action.version)
    calls = []

    def verify(post, **kw):
        calls.append(kw)
        return SimpleNamespace(result='verified_draft', id='receipt', steps=[])

    outcome = reconcile_uncertain_draft(SimpleNamespace(id='p', title='Expected'), action, store,
        collect=lambda **kw: {'complete':True, 'items':[{'title':'Expected'}],
                             'profile_dir':str(tmp_path),'errors':[]},
        verify=verify, evidence_dir=tmp_path)
    assert outcome.status == 'saved_draft'
    assert calls[0]['dry_run'] is True
    assert calls[0]['headless'] is True
