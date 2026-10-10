from __future__ import annotations

from pathlib import Path
import os
import stat
import subprocess
from uuid import uuid4
import zipfile

import pytest

from backend.settings import configure_runtime

configure_runtime()


@pytest.fixture
def skills(tmp_path):
    from src.agent.capabilities.store import CapabilityStore
    from src.agent.capabilities.skill_service import SkillService
    store=CapabilityStore(namespace='test_skills_'+uuid4().hex)
    store.ensure_schema()
    return SkillService(tmp_path,store)


def source(tmp_path, body='核验模型发布来源'):
    directory=tmp_path/'source'
    directory.mkdir(exist_ok=True)
    (directory/'SKILL.md').write_text('---\nname: release-check\ndescription: 每日AI讯息 模型发布核验\n---\n'+body,encoding='utf-8')
    (directory/'reference.md').write_text('核验发布时间与官方模型名称',encoding='utf-8')
    return directory


def test_skill_import_is_previewed_and_frozen_version_contains_resources(skills,tmp_path):
    path=source(tmp_path)
    preview=skills.preview(str(path))
    assert preview['valid'] and skills.list()['rows']==[]
    row=skills.commit(preview['preview_id'],preview['hash'])
    selected=skills.select('今日的每日AI讯息',mode='auto')
    assert selected[0]['name']=='release-check'
    assert '核验模型发布来源' in selected[0]['body']
    assert selected[0]['resources']['reference.md']=='核验发布时间与官方模型名称'
    assert row['version']==preview['hash']
    assert skills.select('每日AI讯息',mode='off')==[]


def test_oversized_body_import_remains_visible_but_cannot_be_silently_selected(skills,tmp_path):
    from src.agent.capabilities.models import CapabilityError
    path=source(tmp_path,'正文'*6500)
    preview=skills.preview(str(path))
    assert preview['valid']
    row=skills.commit(preview['preview_id'],preview['hash'])
    assert len(skills.get(row['id'])['body'])>12000
    with pytest.raises(CapabilityError,match='SKILL_BODY_TOO_LARGE'):
        skills.select('模型发布',mode='manual',names=[row['id']])


def test_import_source_changed_after_preview_is_rejected(skills,tmp_path):
    from src.agent.capabilities.models import CapabilityError
    path=source(tmp_path)
    preview=skills.preview(str(path))
    (path/'reference.md').write_text('changed',encoding='utf-8')
    with pytest.raises(CapabilityError,match='SKILL_PREVIEW_CHANGED'):
        skills.commit(preview['preview_id'],preview['hash'])
    assert skills.list()['rows']==[]


def test_zip_traversal_is_rejected_without_activation(skills,tmp_path):
    from src.agent.capabilities.models import CapabilityError
    archive=tmp_path/'bad.zip'
    with zipfile.ZipFile(archive,'w') as file:
        file.writestr('../escape/SKILL.md','unsafe')
    with pytest.raises(CapabilityError,match='SKILL_ARCHIVE_PATH'):
        skills.preview(str(archive))
    assert not (tmp_path.parent/'escape').exists()


def test_update_creates_new_version_and_disabled_skill_cannot_be_selected(skills,tmp_path):
    from src.agent.capabilities.models import CapabilityError
    path=source(tmp_path)
    preview=skills.preview(str(path))
    first=skills.commit(preview['preview_id'],preview['hash'])
    updated=skills.patch(first['id'],{'expected_revision':first['revision'],'body':'更新后的核验说明'})
    assert first['version']!=updated['version']
    assert skills.store.version(first['id'],first['revision'])['body']=='核验模型发布来源'
    skills.patch(first['id'],{'expected_revision':updated['revision'],'enabled':False})
    with pytest.raises(CapabilityError):
        skills.select('AI',mode='manual',names=[first['id']])


@pytest.mark.parametrize('column,description', [
    ('daily_news', '每日新闻'), ('daily_ai_digest', '每日AI讯息 模型发布'),
    ('daily_wool', 'AI福利 AI鸡蛋'), ('daily_wow', '每日我去 猎奇'),
    ('daily_global_map', '全球事件 地图'),
])
def test_chinese_column_auto_selection_and_manual_limit(skills, tmp_path, column, description):
    from src.agent.capabilities.models import CapabilityError
    path = source(tmp_path)
    (path/'SKILL.md').write_text(
        '---\nname: release-check\ndescription: '+description+'\n---\n核验原始信源', encoding='utf-8')
    preview = skills.preview(str(path))
    row = skills.commit(preview['preview_id'], preview['hash'])
    assert [item['id'] for item in skills.select(column, mode='auto')] == [row['id']]
    assert skills.select(column, mode='off') == []
    with pytest.raises(CapabilityError, match='SKILL_SELECTION_INVALID'):
        skills.select(column, mode='manual', names=[row['id']]*4)
    assert [item['id'] for item in skills.select(column, mode='manual', names=[row['id']])] == [row['id']]


@pytest.mark.parametrize('raw', [
    'name: missing-frontmatter\ninvalid',
    '---\nname: invalid\ndescription:\n  nested: unsupported\n---\ninvalid',
    '---\nname: ../outside\ndescription: invalid\n---\ninvalid',
])
def test_invalid_frontmatter_has_no_activated_or_partial_import(skills, tmp_path, raw):
    from src.agent.capabilities.models import CapabilityError
    path = source(tmp_path)
    (path/'SKILL.md').write_text(raw, encoding='utf-8')
    archive = tmp_path/'invalid.zip'
    with zipfile.ZipFile(archive, 'w') as file:
        for child in path.iterdir():
            file.write(child, child.name)
    with pytest.raises((CapabilityError, ValueError)):
        skills.preview(str(archive))
    assert skills.list()['rows'] == []
    assert not skills.destination.exists()
    assert not list(skills.staging.glob('*'))


def test_reparse_point_is_rejected_before_reading_or_importing(skills, tmp_path, monkeypatch):
    import stat
    from types import SimpleNamespace
    from src.agent.capabilities.models import CapabilityError
    path = source(tmp_path)
    original = Path.lstat
    target = path/'reference.md'

    def reparse(candidate, *args, **kwargs):
        result = original(candidate, *args, **kwargs)
        if candidate == target:
            return SimpleNamespace(st_mode=result.st_mode,
                st_file_attributes=getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024))
        return result

    monkeypatch.setattr(Path, 'lstat', reparse)
    with pytest.raises(CapabilityError, match='SKILL_LINK_REJECTED'):
        skills.preview(str(path))
    assert skills.list()['rows'] == []
    assert not skills.destination.exists()


@pytest.mark.skipif(os.name != 'nt', reason='requires a real Windows directory junction')
def test_actual_windows_junction_is_rejected_without_activation(skills, tmp_path):
    from src.agent.capabilities.models import CapabilityError
    path = source(tmp_path)
    target = tmp_path/'junction-target'
    target.mkdir()
    (target/'outside.md').write_text('must not be imported', encoding='utf-8')
    link = path/'linked-resource'
    assert target.resolve().is_relative_to(tmp_path.resolve())
    assert link.absolute().is_relative_to(tmp_path.resolve())
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
        'New-Item -ItemType Junction -Path $env:TEST_JUNCTION_LINK -Target $env:TEST_JUNCTION_TARGET | Out-Null'],
        env={**os.environ, 'TEST_JUNCTION_LINK': str(link), 'TEST_JUNCTION_TARGET': str(target)},
        capture_output=True, text=True, timeout=15)
    try:
        assert result.returncode == 0, result.stderr
        assert link.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        with pytest.raises(CapabilityError, match='SKILL_LINK_REJECTED'):
            skills.preview(str(path))
        assert skills.list()['rows'] == [] and not skills.destination.exists()
        assert (target/'outside.md').read_text(encoding='utf-8') == 'must not be imported'
    finally:
        if link.exists():
            link.rmdir()


def builtin(skills, tmp_path):
    path = source(tmp_path)
    (path/'diagram.bin').write_bytes(b'\xff\x00\x81')
    preview = skills.preview(str(path))
    imported = skills.commit(preview['preview_id'], preview['hash'])
    return skills.store.put(imported['id'], 'skill', {**skills.get(imported['id']), 'source':'builtin'},
                            expected_revision=imported['revision'])


def test_builtin_copy_has_new_identity_editable_version_and_preserved_resources(skills, tmp_path):
    original = builtin(skills, tmp_path)
    before = skills.get(original['id'])
    copy = skills.copy(original['id'], {'expected_revision':original['revision'], 'name':'personal-release'})
    assert copy['id'] != original['id'] and copy['source'] == 'user_copy' and copy['enabled'] is False
    assert copy['copied_from'] == {'id':original['id'], 'revision':original['revision'], 'version':original['version']}
    assert copy['body'] == original['body'] and copy['resources'] == original['resources']
    assert (Path(copy['path'])/'diagram.bin').read_bytes() == b'\xff\x00\x81'
    assert Path(copy['path']).resolve().is_relative_to(skills.destination.resolve())
    edited = skills.patch(copy['id'], {'expected_revision':copy['revision'], 'body':'人工修改的副本'})
    assert edited['body'] == '人工修改的副本' and edited['version'] != copy['version']
    assert skills.get(original['id']) == before
    assert skills.list()['default_mode'] == 'off'


def test_builtin_body_requires_copy_and_stale_source_does_not_create_resource(skills, tmp_path):
    from src.agent.capabilities.models import CapabilityError
    original = builtin(skills, tmp_path)
    count = len(skills.list()['rows'])
    with pytest.raises(CapabilityError, match='SKILL_BUILTIN_READ_ONLY'):
        skills.patch(original['id'], {'expected_revision':original['revision'], 'body':'do not change'})
    with pytest.raises(CapabilityError, match='SKILL_SOURCE_CONFLICT'):
        skills.copy(original['id'], {'expected_revision':original['revision']-1, 'name':'personal-release'})
    assert len(skills.list()['rows']) == count


@pytest.mark.parametrize('name', ['../outside', 'bad name', '', 'x'*81])
def test_skill_copy_rejects_unsafe_names(skills, tmp_path, name):
    from src.agent.capabilities.models import CapabilityError
    original = builtin(skills, tmp_path)
    with pytest.raises(CapabilityError, match='SKILL_COPY_NAME_INVALID'):
        skills.copy(original['id'], {'expected_revision':original['revision'], 'name':name})
    assert len(skills.list()['rows']) == 1


def test_skill_copy_rejects_oversized_attachment_before_read(skills, tmp_path, monkeypatch):
    from src.agent.capabilities.models import CapabilityError
    original = builtin(skills, tmp_path)
    attachment = Path(original['path'])/'diagram.bin'
    attachment.write_bytes(b'x'*(256*1024+1))
    read = Path.read_bytes

    def bounded_read(path):
        assert path != attachment, 'oversized attachment was read before checking its size'
        return read(path)

    monkeypatch.setattr(Path, 'read_bytes', bounded_read)
    with pytest.raises(CapabilityError, match='SKILL_FILE_TOO_LARGE'):
        skills.copy(original['id'], {'expected_revision':original['revision'], 'name':'personal-release'})
    assert len(skills.list()['rows']) == 1
