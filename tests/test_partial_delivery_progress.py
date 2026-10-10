from backend.progress import build_activity


def test_current_issue_uses_checkpoint_not_resolved_review_failure():
    checkpoint = {
        'jobs': [{'kind': 'daily_news', 'title': 'News', 'count': 10}], 'job_index': 1,
        'failed_jobs': [0], 'job_states': {'0': {
            'review_complete': True, 'status': 'blocked',
            'post_ids': [str(i) for i in range(10)], 'reviewed_post_ids': [str(i) for i in range(10)],
            'last_failure': 'XHS_WRITE_UNCERTAIN: no save receipt'}},
        'events': [
            {'id': 1, 'at': 1, 'node': 'review', 'status': 'failed', 'detail': 'TARGET_DEFICIT approved=9/10'},
            {'id': 2, 'at': 2, 'node': 'review', 'status': 'success', 'detail': 'posts=10'},
            {'id': 3, 'at': 3, 'node': 'upload', 'status': 'failed', 'detail': 'XHS_WRITE_UNCERTAIN'},
        ],
    }
    activity = build_activity({'status': 'partial_success'}, checkpoint, now=4)
    assert len(activity['issues']) == 1
    assert 'TARGET_DEFICIT' not in str(activity['issues'])
    assert activity['counts']['retained'] == 10


def test_rejected_count_is_reported_separately_from_upload_failure():
    checkpoint = {
        'jobs': [{'kind': 'daily_news', 'title': 'News', 'count': 10}], 'job_index': 1,
        'failed_jobs': [0], 'job_states': {'0': {
            'status': 'blocked', 'review_complete': False,
            'post_ids': [str(i) for i in range(10)], 'reviewed_post_ids': [str(i) for i in range(9)],
            'review_summary': {'requested': 10, 'approved': 9, 'rejected': 1, 'pending': 0, 'missing': 1},
            'last_failure': 'one invalid source'}},
    }
    activity = build_activity({'status': 'partial_success'}, checkpoint, now=4)
    assert activity['counts']['rejected'] == 1
    assert activity['jobs'][0]['rejected'] == 1
    assert '1' in activity['summary'] and '未通过审查' in activity['summary']
