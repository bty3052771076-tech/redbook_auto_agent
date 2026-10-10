from src.agent.compaction import compact_conversation, estimate_tokens
import pytest


class Store:
    def __init__(self):
        self.messages = [{'seq':i,'role':'user','content':'待办事项 '+('材料'*200)} for i in range(1,21)]
        self.active = {'version':1,'through_seq':10,'summary':'旧摘要','constraints':['旧约束']}
        self.saved = None
    def get(self, cid): return {'_revision':2}
    def context_messages(self, cid): return self.messages
    def active_snapshot(self, cid): return self.active
    def save_snapshot(self, cid, *, expected_revision, snapshot):
        self.saved = snapshot
        return {**snapshot,'version':2}


def test_compaction_compares_the_same_active_range_and_drops_superseded_constraints():
    from src.agent.compaction import comparison_context
    store = Store()
    before = comparison_context(store.active, store.messages[10:])
    response = compact_conversation(store, 'test', soft_limit_tokens=256, keep_recent_messages=3,
                                    summarize=lambda source: {'summary':'新摘要','constraints':['新约束']})
    assert response['saved'] is True
    after = comparison_context(store.saved, store.messages[17:])
    assert response['input_tokens'] == estimate_tokens(before)
    assert response['output_tokens'] == estimate_tokens(after)
    assert store.saved['constraints'] == ['新约束']
    assert response['output_tokens'] < response['input_tokens']


def test_compaction_with_no_overall_savings_keeps_old_snapshot():
    store = Store()
    store.messages = [{'seq':i,'role':'user','content':'短句'} for i in range(1,21)]
    store.active = {'version':1,'through_seq':10,'summary':'很长的旧摘要'*100,'constraints':[]}
    response = compact_conversation(store, 'test', soft_limit_tokens=256, keep_recent_messages=3,
        summarize=lambda source: {'summary':'长摘要'*300,'constraints':[]})
    assert response['status'] == 'no_savings'
    assert store.saved is None


@pytest.mark.parametrize('constraints', [['完整约束' + str(i) for i in range(101)], ['完整约束' + '字' * 501]])
def test_oversized_constraints_are_rejected_instead_of_silently_truncated(constraints):
    store = Store()
    result = compact_conversation(store, 'test', soft_limit_tokens=256, keep_recent_messages=3,
        summarize=lambda source: {'summary': '只保留可验证事项', 'constraints': constraints})
    assert result['status'] == 'blocked'
    assert store.saved is None
    assert result['attempts'] == 2
