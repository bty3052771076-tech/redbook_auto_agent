"""Explicit one-item diagnostics reuse the normal frozen execution path."""
from datetime import datetime, timezone
import re
import time
from uuid import uuid4

from .models import CapabilityError, digest, safe
from .registry import KIND_TO_TOOL


GENERATORS = {tool:kind for kind,tool in KIND_TO_TOOL.items()}
TITLES = {'daily_news':'每日新闻','daily_ai_digest':'每日AI讯息','daily_wool':'每日羊毛',
          'daily_wow':'每日我去','daily_global_map':'今日全球事件关注图'}
POST_OPERATIONS = {'builtin:xhs.drafts.save_batch':'run','builtin:xhs.drafts.read':'verify-draft',
                   'builtin:content.review':'validate'}


def trial_contract(identity):
    if identity in GENERATORS:
        return {'supported':True,'template':{'query':'','delivery':'generate_only'},
                'description':'完整生成流程，每次只生成一篇；可选择仅本地或保存草稿，不公开发布'}
    if identity in POST_OPERATIONS:
        return {'supported':True,'template':{'post_id':''},'description':'仅操作指定的一篇本地稿件，不公开发布'}
    return {'supported':False,'description':'此子步骤使用对应完整工作流进行验证，不接受任意脚本执行'}


class CapabilityTrials:
    def __init__(self, capabilities):
        self.capabilities = capabilities
        self.store = capabilities.store
        self.current = capabilities.workbench

    def preview(self, identity, data):
        row = self.capabilities.detail(identity)
        if not trial_contract(identity)['supported']:
            raise CapabilityError('TRIAL_UNSUPPORTED','此子步骤请通过对应完整工作流试运行')
        if type(data.get('expected_revision')) is not int or data['expected_revision'] != row['revision'] or not row['enabled']:
            raise CapabilityError('TRIAL_CONFIGURATION_CHANGED','工具已更新或停用，请刷新后再试运行',status=409)
        if set(data)-{'expected_revision','query','delivery','post_id'}:
            raise CapabilityError('TRIAL_INPUT_INVALID','试运行不接收额外脚本、批量数量或发布参数')
        identity_id = uuid4().hex
        payload = {'resource_id':identity,'resource_revision':row['revision'],'origin':'diagnostic',
                   'created_at':datetime.now(timezone.utc).isoformat(),'expires_at':time.time()+600}
        if identity in GENERATORS:
            query, delivery = data.get('query',''),data.get('delivery','generate_only')
            if not isinstance(query,str) or len(query)>120 or delivery not in {'generate_only','save_draft'} or data.get('post_id'):
                raise CapabilityError('TRIAL_INPUT_INVALID','关键词最多120字，交付仅支持本地或保存草稿')
            from backend.plan_service import PlanService
            kind = GENERATORS[identity]
            with self.current.lock:
                cid = self.current.create_agent_conversation('工具试运行：'+row['name'])['id']
                initial = self.current.append_agent_message(cid,'生成1条'+TITLES[kind]+'，只生成本地稿，不上传')['plan']
                saved = self.current._read_agent_conversation(cid)
                plan = PlanService(self.current).save(cid,initial['id'],{
                    'base_plan_version':initial['version'],'conversation_revision':saved['_revision'],
                    'editable_fields':{'jobs':[{'kind':kind,'count':1,'search_keywords':[query.strip()] if query.strip() else []}],
                                       'delivery':delivery,'platform':'xhs','skill_mode':'off','skill_names':[]}},identity_id)['plan']
                saved = self.current._read_agent_conversation(cid)
                saved['plans'][-1]['diagnostic_origin'] = 'diagnostic'
                self.current._write_agent_conversation(saved)
                plan = PlanService(self.current).current_plan(cid)
            payload.update(conversation_id=cid,plan_id=plan['id'],plan_version=plan['version'],
                plan_semantic_hash=plan['semantic_hash'],configuration_fingerprint=plan['configuration_fingerprint'],
                input={'kind':kind,'count':1,'query':query.strip(),'delivery':delivery},
                effects=['model','local_write']+(['platform_write'] if delivery=='save_draft' else []),
                models=plan['model_roles'])
        else:
            post_id = data.get('post_id')
            if not isinstance(post_id,str) or not re.fullmatch(r'[a-f0-9]{32}',post_id) or data.get('query') or data.get('delivery'):
                raise CapabilityError('TRIAL_INPUT_INVALID','请输入唯一一篇本地稿件的32位编号')
            post = self.current.post(post_id)
            if identity == 'builtin:xhs.drafts.save_batch' and (post.get('uploaded') or post.get('status') in {'saved_as_draft','publishing','published'} or post.get('readback')=='verified'):
                raise CapabilityError('TRIAL_POST_ALREADY_UPLOADED','此稿已有上传或发布记录，请使用更新草稿入口',status=409)
            payload.update(input={'kind':POST_OPERATIONS[identity],'post_id':post_id,'platform':'xhs'},
                           post_hash=digest(post),effects=row['effects'])
        env = self.current.environment()
        payload['profile'] = {'user_data_dir':env.get('XHS_CHROME_USER_DATA_DIR',''),
                              'profile_directory':env.get('XHS_CHROME_PROFILE') or 'Default','headless':True}
        payload['profile_fingerprint'] = digest({key:env.get(key,'') for key in
            ('XHS_CHROME_USER_DATA_DIR','XHS_CHROME_PROFILE','XHS_BROWSER_CHANNEL','XHS_CDP_URL')})
        payload['preview_hash'] = digest(payload)
        self.store.put('trial_preview:'+identity_id,'trial_preview',payload,expected_revision=0,reason='预览单次工具试运行，未执行')
        return safe({**payload,'preview_id':identity_id})

    def confirm(self, preview_id, data):
        if set(data)-{'preview_hash','acknowledge_effects'} or data.get('acknowledge_effects') is not True:
            raise CapabilityError('TRIAL_CONFIRM_REQUIRED','请先核对精确输入与额度、文件及平台写入影响')
        preview = self.store.get('trial_preview:'+preview_id)
        if not preview or preview.get('kind') != 'trial_preview':
            raise CapabilityError('TRIAL_PREVIEW_MISSING','试运行预览不存在，请重新预览',status=404)
        if data.get('preview_hash') != preview['preview_hash']:
            raise CapabilityError('TRIAL_PREVIEW_CHANGED','试运行输入已改变，请重新预览',status=409)
        operation_id = digest([self.store.namespace,preview_id])
        try:
            return self.capabilities.operations.get(operation_id)
        except CapabilityError as exc:
            if exc.code != 'OPERATION_NOT_FOUND':
                raise
        self._validate(preview)
        return self.capabilities.operations.submit(lambda:self._execute(preview,preview_id),
            title='单次工具试运行',key=preview_id,request={'preview_hash':preview['preview_hash'],'origin':'diagnostic',
                'trial_preview_id':preview_id,'resource_id':preview['resource_id']})

    def _validate(self, preview):
        row = self.capabilities.detail(preview['resource_id'])
        if time.time()>preview['expires_at'] or row['revision'] != preview['resource_revision'] or not row['enabled']:
            raise CapabilityError('TRIAL_CONFIGURATION_CHANGED','预览已过期或工具已改变，请重新核对',status=409)
        if 'post_hash' in preview and digest(self.current.post(preview['input']['post_id'])) != preview['post_hash']:
            raise CapabilityError('TRIAL_POST_CHANGED','稿件已改变，请重新预览',status=409)
        env = self.current.environment()
        if digest({key:env.get(key,'') for key in
                ('XHS_CHROME_USER_DATA_DIR','XHS_CHROME_PROFILE','XHS_BROWSER_CHANNEL','XHS_CDP_URL')}) != preview['profile_fingerprint']:
            raise CapabilityError('TRIAL_CONFIGURATION_CHANGED','专用浏览器配置已改变，请重新预览',status=409)

    def _execute(self, preview, key):
        self._validate(preview)
        call = self.store.start_call({'resource_id':preview['resource_id'],'resource_name':'单次工具试运行',
            'status':'running','origin':'diagnostic','stage':'trial','input_summary':preview['input'],
            'operation_key':key,'queue_ms':0,'retries':0,'version':preview['resource_revision']})
        started = time.monotonic()
        run = None
        try:
            if preview['resource_id'] in GENERATORS:
                from backend.plan_service import PlanService
                body = {'version':preview['plan_version'],'semantic_hash':preview['plan_semantic_hash'],
                        'configuration_fingerprint':preview['configuration_fingerprint'],'skill_mode':'off','skill_names':[]}
                run = PlanService(self.current).confirm(preview['conversation_id'],preview['plan_id'],body,key)
            else:
                run = self.current.submit(preview['input'],key)
            self.store.attach_call_run(call,run['id'])
            if run.get('status') == 'submission_uncertain':
                raise CapabilityError('TRIAL_WRITE_UNCERTAIN','原请求的启动结果待核对，不会重复提交',status=409)
            while run['status'] in {'queued','running','pending'}:
                time.sleep(.25)
                run = self.current.job_detail(run['id'])
            if preview['resource_id'] in GENERATORS and run['status']=='completed' and len(run.get('post_ids',[])) != 1:
                raise CapabilityError('TRIAL_RESULT_INCOMPLETE','原运行未提供唯一一篇稿件的证据，请核对运行记录',
                                      next_action='运行编号：'+run['id'])
            self.store.finish_call(call,'succeeded' if run['status']=='completed' else 'failed',
                {'run_id':run['id'],'execution_ms':(time.monotonic()-started)*1000,
                 'result_summary':{'status':run['status'],'post_ids':run.get('post_ids',[]),'message':run.get('message','')}})
            if run['status'] != 'completed':
                raise CapabilityError('TRIAL_FAILED','试运行未完成；已生成产物保留，请查看原运行记录',
                                      next_action='运行编号：'+run['id'])
            return {'origin':'diagnostic','run_id':run['id'],'conversation_id':preview.get('conversation_id'),
                    'post_ids':run.get('post_ids',[]),'status':run['status']}
        except Exception as exc:
            if run is not None and getattr(exc,'code','') not in {'TRIAL_FAILED','TRIAL_RESULT_INCOMPLETE','TRIAL_WRITE_UNCERTAIN'}:
                exc = CapabilityError('TRIAL_RESULT_UNCERTAIN','任务已提交，但结果记录或读取未完成；不会再次提交',
                                      status=409,next_action='先核对原运行编号：'+run['id']+'；不要直接重新生成或上传')
            elapsed = (time.monotonic()-started)*1000
            self.store.finish_call(call,'uncertain' if 'UNCERTAIN' in getattr(exc,'code','') else 'failed',
                {'execution_ms':elapsed,'wall_ms':elapsed,'error':safe(str(exc)),
                 'submitted_run_id':run['id'] if run is not None else None})
            if isinstance(exc,CapabilityError):
                raise exc from None
            raise
