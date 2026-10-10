"""Read-only reconciliation before retrying an uncertain draft save."""

import json
import time
from pathlib import Path
from uuid import uuid4


def absence_proven(evidence, action):
    if (evidence.get('post_id') != action.post_id or evidence.get('action_id') != action.action_id
            or evidence.get('content_version') != action.content_version
            or not str(evidence.get('title') or '').strip()):
        return False
    scans = evidence.get('scans') or []
    if len(scans) != 2 or len({scan.get('id') for scan in scans}) != 2:
        return False
    from .playwright_steps import _draft_title_matches_expected
    for scan in scans:
        if (not scan.get('complete') or not scan.get('enumeration_verified') or scan.get('errors')
                or not 0 <= time.time() - float(scan.get('observed_at') or 0) <= 300
                or Path(scan.get('profile_dir') or '').resolve() != Path(action.profile_key).resolve()):
            return False
        if any(_draft_title_matches_expected(str(item.get('title') or ''), evidence['title'])
               for item in scan.get('items', [])):
            return False
    return True


def reconcile_uncertain_draft(post, action, store, *, collect=None, verify=None, evidence_dir=None):
    from .playwright_steps import (
        _draft_title_matches_expected, run_collect_platform_drafts_sync, run_update_draft_sync,
    )
    if action.status not in {'submitting', 'uncertain'} or action.action != 'save_draft':
        return action
    collect = collect or run_collect_platform_drafts_sync
    verify = verify or run_update_draft_sync
    evidence = {'post_id': post.id, 'action_id': action.action_id, 'content_version': action.content_version,
                'title': post.title, 'scans': []}
    directory = Path(evidence_dir or f'data/posts/{post.id}/evidence/reconciliation')
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{uuid4().hex}.json'
    try:
        for _ in range(2):
            scan = dict(collect(headless=True, login_hold=0, wait_timeout_ms=60000))
            scan.update(id=uuid4().hex, observed_at=time.time())
            evidence['scans'].append(scan)
            path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
            if (not scan.get('complete') or scan.get('errors')
                    or Path(scan.get('profile_dir') or '').resolve() != Path(action.profile_key).resolve()):
                return action
            matches = [item for item in scan.get('items', [])
                       if _draft_title_matches_expected(str(item.get('title') or ''), post.title)]
            if matches:
                if len(matches) != 1:
                    return action
                execution = verify(post, dry_run=True, headless=True, login_hold=0, wait_timeout_ms=60000)
                if execution.result == 'verified_draft':
                    return store.record_observation(action.action_id, {
                        'stage': 'saved_draft', 'platform_id': execution.id, 'evidence_ref': str(path),
                    })
                return action
            if not scan.get('enumeration_verified'):
                return action
        if absence_proven(evidence, action):
            return store.allow_absent_draft_retry(action.action_id, expected_version=action.version,
                                                 evidence_ref=str(path))
    except Exception as exc:
        evidence['error'] = type(exc).__name__
        path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    return action
