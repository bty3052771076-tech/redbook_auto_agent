from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
from uuid import uuid4
import zipfile

from src.agent.skills import _frontmatter, _SAFE_NAME
from .models import CapabilityError, digest


class SkillService:
    MAX_TOTAL=20*1024*1024
    MAX_FILES=500

    def __init__(self, root: Path, store):
        self.root, self.store=Path(root),store
        self.destination=self.root/'data/agent/skills/_managed'
        self.staging=self.root/'data/tmp/skill-imports'

    def list(self) -> dict:
        setting=self.store.get('policy:skills') or {}
        return {'rows':[{k:v for k,v in row.items() if k not in {'body','resources','file_contents'}}
                       for row in self.store.resources('skill') if not row.get('retired')],
                'default_mode':setting.get('mode','off'),'policy_revision':setting.get('revision',0),
                'directories':[str(self.destination),str(Path(__file__).resolve().parents[5]/'skills/runtime')]}

    def get(self, identity: str) -> dict:
        row=self.store.get(identity)
        if not row or row.get('kind')!='skill':
            raise CapabilityError('SKILL_NOT_FOUND','技能不存在',status=404,resource_id=identity)
        return {**row,'versions':self.store.versions(identity),'recent_calls':self.store.calls(resource_id=identity)['rows']}

    @staticmethod
    def _reject_link(path: Path) -> None:
        if path.is_symlink() or getattr(path.lstat(),'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',1024):
            raise CapabilityError('SKILL_LINK_REJECTED','技能不能包含符号链接或重解析点')

    def _read_directory(self, directory: Path) -> dict:
        self._reject_link(directory)
        files,resources,hashes,total=[],{}, {},0
        for path in sorted(directory.rglob('*')):
            self._reject_link(path)
            if not path.is_file():
                continue
            if not path.resolve().is_relative_to(directory.resolve()):
                raise CapabilityError('SKILL_PATH_OUTSIDE_ROOT','文件超出技能目录')
            name=path.relative_to(directory).as_posix()
            size=path.stat().st_size
            if size>(1024*1024 if name=='SKILL.md' else 256*1024):
                raise CapabilityError('SKILL_FILE_TOO_LARGE',f'文件超出上限：{name}')
            total+=size
            if total>self.MAX_TOTAL or len(files)>=self.MAX_FILES:
                raise CapabilityError('SKILL_IMPORT_LIMIT','技能最多20MiB、500个文件')
            content=path.read_bytes()
            hashes[name]=hashlib.sha256(content).hexdigest()
            files.append({'path':name,'bytes':size,'hash':hashes[name],'script':name.startswith('scripts/')})
            if name!='SKILL.md':
                try:
                    resources[name]=content.decode('utf-8')
                except UnicodeDecodeError:
                    pass
        if 'SKILL.md' not in hashes:
            raise CapabilityError('SKILL_FILE_MISSING','目录根部需要 SKILL.md')
        raw=(directory/'SKILL.md').read_text(encoding='utf-8')
        metadata,body=_frontmatter(raw)
        # Keep the declared flat frontmatter contract rather than silently ignoring nested YAML.
        header=raw.split('---',2)[1]
        if any(line.startswith((' ','\t')) or line.rstrip().endswith((':','|','>')) for line in header.splitlines() if line.strip() and not line.lstrip().startswith('#')):
            raise CapabilityError('SKILL_FRONTMATTER_UNSUPPORTED','目前只支持平铺 key: value 格式')
        if not _SAFE_NAME.fullmatch(metadata.get('name','')) or not metadata.get('description','').strip():
            raise CapabilityError('SKILL_METADATA_INVALID','名称格式或描述无效')
        return {'name':metadata['name'],'description':metadata['description'],'body':body,'resources':resources,
                'files':files,'hash':digest(hashes),'total_bytes':total}

    def _extract(self, source: Path, destination: Path) -> Path:
        with zipfile.ZipFile(source) as archive:
            infos=archive.infolist()
            if len(infos)>self.MAX_FILES or sum(i.file_size for i in infos)>self.MAX_TOTAL:
                raise CapabilityError('SKILL_ARCHIVE_LIMIT','ZIP超出20MiB或500文件')
            for info in infos:
                name=PurePosixPath(info.filename.replace('\\','/'))
                if name.is_absolute() or '..' in name.parts or ':' in info.filename or stat.S_ISLNK(info.external_attr>>16):
                    raise CapabilityError('SKILL_ARCHIVE_PATH','ZIP包含越界路径或链接')
                if info.file_size>1024*1024:
                    raise CapabilityError('SKILL_FILE_TOO_LARGE','ZIP中的文件超出上限')
            destination.mkdir(parents=True,exist_ok=True)
            archive.extractall(destination)
        roots=[p.parent for p in destination.rglob('SKILL.md')]
        if len(roots)!=1:
            raise CapabilityError('SKILL_ARCHIVE_INVALID','ZIP必须只包含一个技能')
        return roots[0]

    def preview(self, source_path: str) -> dict:
        source=Path(source_path).resolve(strict=True)
        self._reject_link(Path(source_path))
        identity=uuid4().hex
        extracted=None
        try:
            directory=source
            if source.is_file():
                if source.suffix.lower()!='.zip':
                    raise CapabilityError('SKILL_IMPORT_FORMAT','请选择文件夹或ZIP')
                extracted=self.staging/identity
                directory=self._extract(source,extracted)
            content=self._read_directory(directory)
            collisions=[r for r in self.store.resources('skill') if r.get('name')==content['name'] and not r.get('retired')]
            payload={**content,'preview_id':identity,'source_path':str(source),'directory':str(directory),
                     'source_archive_hash':hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else '',
                     'valid':True,'issues':[],'collision':collisions[0]['id'] if collisions else None,
                     'created_at':datetime.now(timezone.utc).isoformat()}
            self.store.put('skill_preview:'+identity,'skill_preview',payload,expected_revision=0,reason='技能导入预览')
            return {k:v for k,v in payload.items() if k not in {'body','resources','directory','source_archive_hash'}}
        except Exception:
            if extracted and extracted.resolve().is_relative_to(self.staging.resolve()):
                shutil.rmtree(extracted,ignore_errors=True)
            raise

    def commit(self, preview_id: str, expected_hash: str, *, allow_new_version: bool=False) -> dict:
        preview=self.store.get('skill_preview:'+preview_id)
        if not preview:
            raise CapabilityError('SKILL_PREVIEW_MISSING','导入预览不存在',status=404)
        source=Path(preview['source_path'])
        if preview.get('source_archive_hash') and hashlib.sha256(source.read_bytes()).hexdigest()!=preview['source_archive_hash']:
            raise CapabilityError('SKILL_PREVIEW_CHANGED','ZIP已改变，请重新预览',status=409)
        content=self._read_directory(Path(preview['directory']))
        if expected_hash!=preview['hash'] or content['hash']!=preview['hash']:
            raise CapabilityError('SKILL_PREVIEW_CHANGED','源文件已改变，请重新预览',status=409)
        old=self.store.get(preview.get('collision','')) if preview.get('collision') else None
        if old and not allow_new_version:
            raise CapabilityError('SKILL_NAME_COLLISION','同名技能已存在，请明确创建新版本',status=409)
        identity=old['id'] if old else 'skill_'+uuid4().hex
        target=self.destination/identity/content['hash']
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():
            temporary=target.parent/('import-'+uuid4().hex)
            shutil.copytree(preview['directory'],temporary)
            try:
                if self._read_directory(temporary)['hash']!=content['hash']:
                    raise CapabilityError('SKILL_PREVIEW_CHANGED','复制时文件已改变',status=409)
                temporary.rename(target)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        result=self.store.put(identity,'skill',{**content,'version':content['hash'],'version_hash':content['hash'],
             'path':str(target),'source':'user_import','enabled':True,'execution_allowed':False,'trusted_instructions':False},
             expected_revision=old['revision'] if old else 0,reason='导入技能版本')
        return {k:v for k,v in result.items() if k not in {'body','resources'}}

    def patch(self, identity: str, data: dict) -> dict:
        if identity=='defaults':
            mode=data.get('default_mode')
            if mode not in {'off','auto','manual'}:
                raise CapabilityError('SKILL_MODE_INVALID','请选择关闭、自动或手动')
            return self.store.put('policy:skills','policy',{'mode':mode},expected_revision=int(data['expected_revision']),reason='修改技能默认模式')
        current=self.get(identity)
        if current.get('source') == 'builtin' and 'body' in data:
            raise CapabilityError('SKILL_BUILTIN_READ_ONLY','内置技能不能原地编辑，请创建个人副本',
                                  status=409,next_action='在技能说明中选择创建个人副本')
        value={k:v for k,v in current.items() if k not in {'versions','recent_calls'}}
        if 'version_revision' in data:
            revision = data['version_revision']
            if type(revision) is not int or revision<1:
                raise CapabilityError('SKILL_VERSION_MISSING','请选择有效的技能修订版本')
            value = self.store.version(identity, revision)
            if value.get('kind') != 'skill':
                raise CapabilityError('SKILL_VERSION_MISSING','技能版本不存在')
        elif 'version' in data:
            candidates=[self.store.version(identity,r['revision']) for r in current['versions']]
            value=next((r for r in candidates if r.get('version')==data['version']),None)
            if value is None:
                raise CapabilityError('SKILL_VERSION_MISSING','技能版本不存在')
        if 'body' in data:
            if not isinstance(data['body'],str) or len(data['body'].encode())>1024*1024:
                raise CapabilityError('SKILL_BODY_INVALID','正文必须为不超过1MiB的文本')
            value['body']=data['body']
            value['version']=digest({'body':value['body'],'resources':value['resources']})
            value['version_hash']=value['version']
            # Immutable edited versions are persisted in PG; original files stay untouched.
        if 'enabled' in data:
            value['enabled']=bool(data['enabled'])
        return self.store.put(identity,'skill',value,expected_revision=int(data['expected_revision']),reason='修改技能版本或状态')

    def copy(self, identity: str, data: dict) -> dict:
        if set(data)-{'expected_revision','name'}:
            raise CapabilityError('SKILL_COPY_INVALID','副本只接收来源修订和新名称')
        current = self.get(identity)
        revision = data.get('expected_revision')
        if type(revision) is not int or revision != current['revision']:
            raise CapabilityError('SKILL_SOURCE_CONFLICT','来源技能已更新，请刷新后再创建副本',status=409)
        name = data.get('name')
        if not isinstance(name,str) or not _SAFE_NAME.fullmatch(name):
            raise CapabilityError('SKILL_COPY_NAME_INVALID','名称须为1至80位小写字母、数字、点、下划线或连字符')
        if any(row.get('name') == name and not row.get('retired') for row in self.store.resources('skill')):
            raise CapabilityError('SKILL_NAME_COLLISION','同名技能已存在，请使用新的名称',status=409)
        root = self.root.resolve()
        if root.drive.upper() != 'E:' or not self.destination.resolve().is_relative_to(root) or not self.staging.resolve().is_relative_to(root):
            raise CapabilityError('SKILL_PATH_OUTSIDE_ROOT','副本目录必须位于本程序的E盘运行区')
        description = str(current.get('description') or '').replace('\r',' ').replace('\n',' ').strip()
        body = current.get('body')
        resources = current.get('resources')
        if not description or not isinstance(body,str) or not isinstance(resources,dict) or not current.get('version'):
            raise CapabilityError('SKILL_COPY_SOURCE_INVALID','来源正文或附件不完整，请重新导入技能')
        contents = {'SKILL.md':f'---\nname: {name}\ndescription: {description}\n---\n{body}'.encode('utf-8')}
        if len(contents['SKILL.md']) > 1024*1024:
            raise CapabilityError('SKILL_FILE_TOO_LARGE','文件超出上限：SKILL.md')
        for path, content in resources.items():
            relative = PurePosixPath(path) if isinstance(path,str) else PurePosixPath('/')
            if (relative.is_absolute() or '..' in relative.parts or ':' in str(path) or '\\' in str(path)
                    or path == 'SKILL.md' or not isinstance(content,str)):
                raise CapabilityError('SKILL_RESOURCE_INVALID','来源附件路径或文本格式无效')
            contents[path] = content.encode('utf-8')
            if len(contents[path]) > 256*1024:
                raise CapabilityError('SKILL_FILE_TOO_LARGE',f'文件超出上限：{path}')
        # Non-text attachments are copied only from the recorded, hash-verified version.
        source = Path(current.get('path') or '')
        files = current.get('files') or []
        if not isinstance(files,list) or len(files) > self.MAX_FILES:
            raise CapabilityError('SKILL_COPY_SOURCE_INVALID','来源附件目录无效，请重新导入技能')
        for item in files:
            if not isinstance(item,dict) or not isinstance(item.get('path'),str):
                raise CapabilityError('SKILL_COPY_SOURCE_INVALID','来源附件记录无效，请重新导入技能')
            path = item['path']
            if path in contents:
                continue
            relative = PurePosixPath(path)
            candidate = source/path
            if (relative.is_absolute() or '..' in relative.parts or ':' in path or '\\' in path
                    or not current.get('path') or not candidate.resolve().is_relative_to(source.resolve())):
                raise CapabilityError('SKILL_RESOURCE_INVALID','来源附件路径无效')
            self._reject_link(source)
            self._reject_link(candidate)
            if candidate.stat().st_size > 256*1024:
                raise CapabilityError('SKILL_FILE_TOO_LARGE',f'文件超出上限：{path}')
            value = candidate.read_bytes()
            if hashlib.sha256(value).hexdigest() != item.get('hash'):
                raise CapabilityError('SKILL_COPY_SOURCE_CHANGED','来源附件已改变，请重新导入',status=409)
            contents[path] = value
        if len(contents)>self.MAX_FILES or sum(map(len,contents.values()))>self.MAX_TOTAL:
            raise CapabilityError('SKILL_IMPORT_LIMIT','技能最多20MiB、500个文件')
        scratch = self.staging/('copy-'+uuid4().hex)
        scratch.mkdir(parents=True)
        try:
            for path,value in contents.items():
                file = scratch/path
                file.parent.mkdir(parents=True,exist_ok=True)
                file.write_bytes(value)
            content = self._read_directory(scratch)
            new_id = 'skill_'+uuid4().hex
            target = self.destination/new_id/content['hash']
            target.parent.mkdir(parents=True)
            scratch.rename(target)
            return self.store.put(new_id,'skill',{**content,'path':str(target),'source':'user_copy',
                'version':content['hash'],'version_hash':content['hash'],'enabled':False,
                'execution_allowed':False,'trusted_instructions':False,
                'copied_from':{'id':identity,'revision':revision,'version':current['version']}},
                expected_revision=0,reason='创建个人技能副本')
        finally:
            if scratch.exists() and scratch.resolve().is_relative_to(self.staging.resolve()):
                shutil.rmtree(scratch)

    def retire(self, identity: str, revision: int) -> dict:
        row=self.get(identity)
        return self.store.put(identity,'skill',{**row,'enabled':False,'retired':True},expected_revision=revision,reason='退役技能')

    def select(self, query: str, *, mode: str='off', names: list[str]|None=None) -> list[dict]:
        def checked(selected):
            for row in selected:
                if len(row.get('body',''))>12000:
                    raise CapabilityError('SKILL_BODY_TOO_LARGE','正文超过运行时12000字符限制，请将长材料拆分为按需附件',
                                          resource_id=row['id'],status=409)
            return selected
        if mode=='off':
            return []
        rows=[r for r in self.store.resources('skill') if r.get('enabled') and not r.get('retired')]
        if mode=='manual':
            if not names or len(names)>3 or len(set(names))!=len(names):
                raise CapabilityError('SKILL_SELECTION_INVALID','手动选择1至3项不同的技能')
            selected=[]
            for name in names:
                row=next((r for r in rows if r['id']==name or r['name']==name),None)
                if row is None:
                    raise CapabilityError('SKILL_DISABLED','技能已停用或不存在',resource_id=name)
                selected.append(row)
            return checked(selected)
        if mode!='auto':
            raise CapabilityError('SKILL_MODE_INVALID','请选择关闭、自动或手动')
        aliases={'daily_news':'每日新闻','daily_ai_digest':'每日AI讯息 模型发布','daily_wool':'AI福利 AI鸡蛋','daily_wow':'每日我去 猎奇','daily_global_map':'全球事件 地图'}
        for key,value in aliases.items():
            query=query.replace(key,value)
        terms=set(re.findall(r'[a-zA-Z0-9_.-]{2,}|[\u3400-\u9fff]{2,}',query.casefold()))
        terms.update(query[i:i+2].casefold() for i in range(len(query)-1) if all('\u3400'<=c<='\u9fff' for c in query[i:i+2]))
        ranked=[(sum(term in f"{r['name']} {r['description']}".casefold() for term in terms),r) for r in rows]
        return checked([r for score,r in sorted(ranked,key=lambda pair:(-pair[0],pair[1]['name'])) if score][:3])

    def resource(self, identity: str, relative_path: str) -> dict:
        row=self.get(identity)
        path=PurePosixPath(relative_path.replace('\\','/'))
        if path.is_absolute() or '..' in path.parts or relative_path not in row.get('resources',{}):
            raise CapabilityError('SKILL_RESOURCE_INVALID','引用资源不存在或路径越界')
        return {'content':row['resources'][relative_path],'version':row['version']}
