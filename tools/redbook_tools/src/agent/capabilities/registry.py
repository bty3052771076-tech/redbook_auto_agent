from __future__ import annotations
from copy import deepcopy


_JOB = {'type':'object','description':'已确认栏目、篇数、选题与评价设置','properties':{
    'kind':{'type':'string','description':'栏目类型'},
    'count':{'type':'integer','description':'稿件篇数'},
    'prompt':{'type':'string','description':'编译后的选题要求'},
    'evaluation_viewpoint':{'type':'string','description':'评价视角'},
    'lookback_days':{'description':'已冻结的日期窗口策略'}}}
_CONTEXT = {'type':'object','description':'本次冻结上下文与阶段结果，由工作流提供'}
_POST = {'type':'object','properties':{
    'id':{'type':'string','description':'本地稿件编号'},
    'title':{'type':'string','description':'标题'},
    'body':{'type':'string','description':'正文'},
    'assets':{'type':'array','description':'图片及素材引用'}}}
_POSTS = {'type':'array','description':'本地稿件列表','items':_POST}
_JOB_INPUT = {'type':'object','properties':{'job':_JOB,'context':_CONTEXT}}
_COLLECTION = {'type':'array','description':'候选列表及采集元信息','prefixItems':[
    {'type':'array','description':'有来源和日期的候选材料'},
    {'type':'object','description':'信源、日期窗口与采集状态'}]}
_CONTRACTS = {
    'account.sync':({'type':'object','properties':{'job':_JOB}},
                    {'type':'object','description':'实际账号历史、读者偏好与同步结果'}),
    'news.search':({'type':'object','properties':{
        'prompt_hint':{'type':'string','description':'检索主题'},
        'search_days':{'type':'integer','description':'采集日期窗口'},
        'max_records':{'type':'integer','description':'原始候选上限'},
        'timeout_s':{'type':'number','description':'采集请求时限'}}},_COLLECTION),
    'ai.search':({'type':'object','properties':{
        'target_count':{'type':'integer','description':'候选条目目标，不是稿件篇数'},
        'min_official_count':{'type':'integer','description':'官方候选目标'},
        'max_age_days':{'type':'integer','description':'新鲜度窗口'},
        'sources':{'type':'array','description':'已配置AI信源'}}},_COLLECTION),
    'wool.search':({'type':'object','properties':{
        'max_age_days':{'type':'integer','description':'活动新鲜度窗口'},
        'sources':{'type':'array','description':'已配置活动信源'},
        'now':{'description':'本次北京时间日期基准'}}},_COLLECTION),
    'writer.generate':({'type':'object','properties':{
        'cfg':{'description':'冻结的写稿模型配置；凭据不展示'},
        'system_prompt':{'type':'string','description':'系统约束'},
        'user_prompt':{'type':'string','description':'当前材料与生成要求'},
        'max_tokens':{'type':'integer','description':'单次输出上限'}}},
        {'type':'object','description':'当前请求约定的写稿或结构化结果','properties':{
            'title':{'type':'string','description':'标题（写稿请求）'},
            'body':{'type':'string','description':'正文（写稿请求）'}}}),
    'image.generate':({'type':'object','properties':{
        'post_id':{'type':'string','description':'所属稿件'},
        'prompt':{'type':'string','description':'图像生成要求'},
        'dest_dir':{'type':'string','description':'运行区产物目录'},
        'reference_paths':{'type':'array','description':'参考原图与人设图（支持编辑的适配器）'}}},
        {'description':'图片路径及实际供应商元信息，具体结构由生图适配器提供'}),
    'image.review':({'type':'object','properties':{
        'config':{'description':'冻结的视觉审核模型配置'},
        'prompt':{'type':'string','description':'图文审核要求'},
        'image_path':{'type':'string','description':'待审核本地图片'}}},
        {'description':'视觉模型返回的审核文本或结构化结论'}),
    'content.review':({'type':'object','properties':{'job':_JOB,'posts':_POSTS,'context':_CONTEXT}},
        {'description':'未通过的稿件编号列表，或结构化审核结果；不表示发布成功'}),
    'xhs.drafts.save_batch':({'type':'object','properties':{'job':_JOB,'posts':_POSTS,'context':_CONTEXT}},
        {'type':'object','description':'每个稿件编号对应保存结果与说明'}),
    'xhs.drafts.read':({'type':'object','properties':{
        'post_id':{'type':'string','description':'唯一一篇本地稿件编号'}}},
        {'type':'object','description':'实际平台草稿读回证据','properties':{
            'actual_title':{'type':'string','description':'实际标题'},
            'actual_body':{'type':'string','description':'实际完整正文'},
            'actual_image_count':{'type':'integer','description':'实际图片数'}}}),
    'artifacts.read':({'type':'object','properties':{
        'post_ids':{'type':'array','description':'已保留稿件的编号列表','items':{'type':'string'}}}},_POSTS),
    'controller.plan':({'type':'object','properties':{
        'jobs':{'type':'array','description':'已确认栏目任务','items':_JOB},'context':_CONTEXT}},
        {'type':'object','properties':{'job_order':{'type':'array','description':'执行顺序'},
            'tool_calls':{'type':'array','description':'当前阶段获准工具的选择'}}}),
}
for _key in ('news.generate','ai.generate','wow.generate','wool.generate','global_map.generate'):
    _CONTRACTS[_key] = (_JOB_INPUT,_POSTS)


def builtin_catalog() -> list[dict]:
    definitions = [
        ('account.sync', '同步已发布数据', '读取专用账号历史与读者偏好', '账号与历史', 'preparation', ['profile'], ['platform_read']),
        ('news.search', '检索新闻材料', '从统一信源收集有日期的候选材料', '选题与材料', 'preparation', [], ['network_read']),
        ('ai.search', '检索厂商 AI 动态', '收集模型发布与可核验的厂商动态', '选题与材料', 'preparation', [], ['network_read']),
        ('wool.search', '检索 AI 福利', '核查可领取额度与活动有效期', '选题与材料', 'preparation', [], ['network_read']),
        ('news.generate', '生成每日新闻', '按材料生成新闻内容及配图', '内容生产', 'generate', ['builtin:writer.generate', 'builtin:image.generate', 'builtin:news.search'], ['model', 'local_write']),
        ('ai.generate', '生成每日 AI 讯息', '整理新鲜且不重复的模型与厂商消息', '内容生产', 'generate', ['builtin:writer.generate', 'builtin:ai.search'], ['model', 'local_write']),
        ('wow.generate', '生成每日我去', '生成日期核验后的猎奇新闻', '内容生产', 'generate', ['builtin:writer.generate', 'builtin:image.generate', 'builtin:news.search'], ['model', 'local_write']),
        ('wool.generate', '生成 AI 鸡蛋', '生成经过有效期核验的 AI 福利及参考图描改', '内容生产', 'generate', ['builtin:writer.generate', 'builtin:image.generate', 'builtin:wool.search'], ['model', 'local_write']),
        ('global_map.generate', '生成全球事件关注图', '生成含真实底图与核验事件的关注图', '内容生产', 'generate', ['builtin:news.search'], ['local_write']),
        ('writer.generate', '内容写稿', '使用已绑定写稿模型总结材料', '图文生成', 'generate', ['writer_model'], ['model']),
        ('image.generate', '生成或编辑图片', '使用已绑定生图模型生成或双图编辑', '图文生成', 'generate', ['image_model'], ['model']),
        ('image.review', '检查图片', '检查图像与材料的一致性', '审稿与历史匹配', 'evidence', [], ['model']),
        ('content.review', '审稿与查重', '校验事实、文本完整性、日期与重复事件', '审稿与历史匹配', 'review', ['database'], ['local_read']),
        ('xhs.drafts.save_batch', '批量保存平台草稿', '使用共享专用 profile 串行保存已审核稿件', '平台草稿', 'upload', ['profile', 'builtin:content.review'], ['platform_write']),
        ('xhs.drafts.read', '读取平台草稿', '读取平台保存结果与交付凭据', '平台草稿', 'evidence', ['profile'], ['platform_read']),
        ('artifacts.read', '读取保留产物', '读取已完成稿件供断点续跑复用', '状态管理', 'recovery', ['database'], ['local_read']),
        ('controller.plan', '智能体任务编排', '使用当前主控模型安排领域任务', '状态管理', 'preparation', ['agent_model'], ['model']),
    ]
    return [{
        'id': 'builtin:' + key, 'name': name, 'description': description,
        'kind': 'builtin', 'group': group, 'enabled': True, 'revision': 0,
        'version': '2', 'health': {'status': 'unknown', 'observed_at': None, 'probe': 'configuration'},
        'binding': 'workflow', 'stages': [stage,'evidence'] if key=='controller.plan' else [stage], 'dependencies': dependencies,
        'effects': effects, 'input_schema': deepcopy(_CONTRACTS[key][0]), 'output_schema': deepcopy(_CONTRACTS[key][1]),
        'concurrency': 1 if key.startswith('xhs.') else None,
        'concurrency_policy': '同一专用 profile 串行执行（并发1）' if key.startswith('xhs.') else '由工作流并发队列控制',
        'idempotency': '冻结任务与保留产物控制；提交不确定时先核对原运行',
        'recovery': '继续原断点；已完成产物保留，不把读回失败当作重新上传依据',
        'timeout_seconds': None, 'max_timeout_seconds': 600,
        'timeout_configurable': key in {'writer.generate','controller.plan'},
        'required': key in {'content.review', 'artifacts.read'},
        'active_runs': [],
    } for key, name, description, group, stage, dependencies, effects in definitions]


KIND_TO_TOOL = {'daily_news': 'builtin:news.generate', 'daily_ai_digest': 'builtin:ai.generate',
                'daily_wow': 'builtin:wow.generate', 'daily_wool': 'builtin:wool.generate',
                'daily_global_map': 'builtin:global_map.generate'}
