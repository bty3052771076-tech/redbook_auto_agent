import { useEffect, useRef, useState } from 'react';
import { BrainCircuit, Pencil, Plus, Save, Trash2, X, Minus } from 'lucide-react';
import { api, requestTaskCalibration, type Conversation, type Model, type Plan, type PlanJob } from './api';
import { RolePicker } from './ModelPlatforms';
import './plan-editor.css';

const columns: Record<string, string> = { daily_news: '每日新闻', daily_ai_digest: '每日AI讯息', daily_wool: 'AI鸡蛋', daily_wow: '每日我去', daily_global_map: '今日全球事件关注图' };
const topicKinds = new Set(['daily_news', 'daily_ai_digest', 'daily_wow']);
const jobFields = ['count', 'search_keywords', 'topic_preferences', 'topic_brief', 'topic_brief_strength', 'content_constraints', 'evaluation_viewpoint', 'legacy_extra_requirements'] as const;
const optionFields = ['delivery', 'platform', 'performance_mode', 'image_score_required', 'model_roles'] as const;
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
const fieldLabels: Record<string, string> = { count: '篇数', search_keywords: '检索词', topic_preferences: '选题偏向',
  topic_brief: '补充要求', topic_brief_strength: '要求强度', content_constraints: '结构化约束',
  evaluation_viewpoint: '评价视角', delivery: '交付方式', platform: '平台', performance_mode: '运行模式',
  image_score_required: '图片评分硬门槛', model_roles: '模型选择' };
const valueLabels: Record<string, string> = { generate_only: '仅生成本地稿', save_draft: '保存平台草稿',
  speed: '速度优先', balanced: '速度与稳定平衡', preference: '选题偏好', requirement: '必须满足',
  xhs: '小红书', toutiao: '今日头条', both: '小红书＋今日头条' };
function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === '' || Array.isArray(value) && !value.length) return '已清空';
  if (typeof value === 'boolean') return value ? '开启' : '关闭';
  if (Array.isArray(value)) return value.map(displayValue).join('、');
  if (typeof value === 'object') return Object.entries(value as Record<string, unknown>).map(([key, item]) => `${({ agent: '主控', writer: '写稿', image: '生图' } as Record<string, string>)[key] || key}：${displayValue(item)}`).join('；');
  return valueLabels[String(value)] || String(value);
}

function materialize(plan: Plan): Plan {
  return { ...structuredClone(plan), jobs: plan.jobs.map((j, index) => ({ ...structuredClone(j), job_id: j.job_id || `legacy-${index}`,
    search_keywords: j.search_keywords || (j.keyword_mode !== 'preference' ? j.keywords || [] : []),
    topic_preferences: j.topic_preferences || (j.keyword_mode === 'preference' ? j.keywords || [] : []),
    topic_brief: j.topic_brief || '', evaluation_viewpoint: j.evaluation_viewpoint || '无视角评价', legacy_extra_requirements: j.legacy_extra_requirements || '' })) };
}

function changes(base: Plan, draft: Plan) {
  const fields: Record<string, unknown> = {};
  for (const key of optionFields) if (!same(base[key], draft[key])) fields[key] = draft[key];
  if (!same(base.jobs, draft.jobs)) fields.jobs = draft.jobs.map(job => {
    const before = base.jobs.find(j => j.job_id === job.job_id);
    const result: Record<string, unknown> = before ? { target_job_id: job.job_id } : { kind: job.kind };
    for (const key of jobFields) if (!before || !same(before[key], job[key])) result[key] = job[key];
    return result;
  });
  return fields;
}

function Tags({ label, values, source, onChange }: { label: string; values: string[]; source: string; onChange: (values: string[]) => void }) {
  const [input, setInput] = useState('');
  const [error, setError] = useState('');
  function add() {
    const value = input.trim();
    if (!value) return;
    if (value.length > 80 || values.length >= 16) { setError('最多16项，每项1至80字'); return; }
    if (values.includes(value)) { setError('这一项已经存在'); return; }
    onChange([...values, value]); setInput(''); setError('');
  }
  return <div className="pe-field"><label>{label}<small className="pe-origin">{source}</small></label><div className="pe-tags">{values.map(value => <span key={value}>{value}
    <button type="button" title={`移除${value}`} aria-label={`移除${value}`} onClick={() => onChange(values.filter(v => v !== value))}><X size={13} /></button></span>)}</div>
    <div className="pe-tag-input"><input aria-label={label} value={input} maxLength={81} onChange={e => setInput(e.target.value)} onKeyDown={e => {
      if (e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;
      if (e.key === 'Enter') { e.preventDefault(); add(); }
      if (e.key === 'Backspace' && !input && values.length) onChange(values.slice(0, -1));
    }} /><button type="button" className="icon-button" title={`添加${label}`} aria-label={`添加${label}`} onClick={add}><Plus size={17} /></button></div>
    {error && <small className="pe-error" role="alert">{error}</small>}
  </div>;
}

export function PlanEditor({ conversation, plan, models, candidate, recognitionId, disabled, onSaved, onPending }: {
  conversation: Conversation; plan: Plan; models: Model[]; candidate?: Plan; recognitionId?: string; disabled: boolean;
  onSaved: () => Promise<void>; onPending: (pending: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const [initial, setInitial] = useState(() => materialize(candidate || plan));
  const [draft, setDraft] = useState(initial);
  const [saving, setSaving] = useState(false), [error, setError] = useState(''), [discard, setDiscard] = useState(false);
  const [calibrationChoice, setCalibrationChoice] = useState(false);
  const [accepted, setAccepted] = useState<string[]>([]);
  const [decisions, setDecisions] = useState<{ issue_id: string; content_hash: string; decision: string }[]>([]);
  const [addKind, setAddKind] = useState('');
  const trigger = useRef<HTMLButtonElement>(null), dialog = useRef<HTMLDivElement>(null);
  const returnToEditor = useRef<HTMLButtonElement>(null);
  const request = useRef<{ hash: string; key: string } | null>(null);
  const changed = !same(initial, draft) || accepted.length > 0 || decisions.length > 0;
  const title = candidate ? '编辑校准候选' : '编辑本次计划';
  const dismiss = () => { if (saving) return; if (changed) setDiscard(true); else close(); };
  function close() { setOpen(false); setDiscard(false); setCalibrationChoice(false); trigger.current?.focus(); }
  const calibrationPending = conversation.task_recognitions?.some(record => record.base_plan_id === plan.id
    && ['running', 'ready', 'needs_input'].includes(record.status));

  useEffect(() => { if (calibrationChoice) returnToEditor.current?.focus(); }, [calibrationChoice]);

  useEffect(() => { if (!open) return; onPending(true); return () => onPending(false); }, [open, onPending]);
  useEffect(() => {
    if (!open) return;
    const old = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    dialog.current?.focus();
    return () => { document.body.style.overflow = old; };
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const beforeUnload = (e: BeforeUnloadEvent) => { if (changed) { e.preventDefault(); e.returnValue = ''; } };
    window.addEventListener('beforeunload', beforeUnload);
    return () => window.removeEventListener('beforeunload', beforeUnload);
  }, [open, changed]);

  function updateJob(identity: string, fields: Partial<PlanJob>) {
    setDraft(previous => ({ ...previous, jobs: previous.jobs.map(j => j.job_id === identity ? { ...j, ...fields } : j) }));
  }
  function origin(job: PlanJob, field: keyof PlanJob) {
    const before = initial.jobs.find(row => row.job_id === job.job_id);
    if (!before || !same(before[field], job[field])) return '本次人工修改';
    const value = draft.field_origins?.[`jobs.${job.job_id}.${field}`];
    return value === 'user_edit' ? '人工修订' : draft.recognition_source === 'llm' ? '模型建议' : '本地草案';
  }
  async function calibrate() {
    if (saving || calibrationPending) return;
    setSaving(true); setError(''); setCalibrationChoice(false);
    const value = materialize(plan);
    setInitial(value); setDraft(value); setAccepted([]); setDecisions([]);
    try {
      await requestTaskCalibration(conversation, plan);
      await onSaved(); close();
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setSaving(false); }
  }

  async function save(calibrateAfter = false) {
    if (saving) return;
    setSaving(true); setError(''); setCalibrationChoice(false);
    const body = { base_plan_version: plan.version, conversation_revision: conversation.conversation_revision,
      editable_fields: changes(initial, draft), review_decisions: decisions, ...(candidate ? { accepted_candidate_paths: accepted } : {}) };
    const hash = JSON.stringify(body);
    if (request.current?.hash !== hash) request.current = { hash, key: crypto.randomUUID() };
    let savedPlan: Plan | undefined;
    try {
      const route = candidate ? `/api/conversations/${conversation.id}/task-recognitions/${recognitionId}/adopt`
        : `/api/conversations/${conversation.id}/plans/${plan.id}/revisions`;
      const result = await api<{ plan: Plan }>(route, 'POST', body, request.current.key);
      savedPlan = result.plan;
      if (calibrateAfter) await requestTaskCalibration(conversation, savedPlan);
      await onSaved(); close();
    } catch (cause) {
      if (savedPlan) {
        const value = materialize(savedPlan);
        setInitial(value); setDraft(value); setAccepted([]); setDecisions([]);
        try { await onSaved(); } catch { /* Preserve the successful save if refreshing is temporarily unavailable. */ }
      }
      setError(cause instanceof Error ? cause.message : String(cause));
    }
    finally { setSaving(false); }
  }

  return <>
    <button ref={trigger} className="quiet-button" disabled={disabled} onClick={() => {
      const value = materialize(candidate || plan); setInitial(value); setDraft(value); setAccepted([]); setDecisions([]); setError(''); setCalibrationChoice(false); setOpen(true);
    }}><Pencil size={15} />{candidate ? '编辑候选' : '编辑计划'}</button>
    {open && <div className="pe-overlay" onMouseDown={e => { if (e.target === e.currentTarget) dismiss(); }}>
      <div ref={dialog} className="pe-drawer" role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} onKeyDown={e => {
        if (e.key === 'Escape' && !e.nativeEvent.isComposing) { e.preventDefault(); dismiss(); }
        if (e.key !== 'Tab') return;
        const items = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex="0"]') || []).filter(e => e.getClientRects().length > 0);
        const first = items[0], last = items.at(-1);
        if (e.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) { e.preventDefault(); last?.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
      }}>
        <header className="pe-header"><h2>{title}</h2>{!candidate && <button className="quiet-button" disabled={saving || calibrationPending}
          onClick={() => { if (changed) { setDiscard(false); setCalibrationChoice(true); } else void calibrate(); }}><BrainCircuit size={15} />大模型校准</button>}
          <button className="icon-button" title="关闭编辑器" aria-label="关闭编辑器" disabled={saving} onClick={dismiss}><X size={19} /></button></header>
        <fieldset className="pe-body" disabled={saving}>
          <details className="pe-source"><summary>原始指令</summary><p>{conversation.messages.find(m => m.id === plan.source_message_id)?.content || '原始指令未关联，请核对计划字段'}</p></details>
          {draft.jobs.map((job, index) => <section className="pe-column" key={job.job_id}>
            <div className="pe-column-heading"><h3>{columns[job.kind] || job.title}</h3><button className="icon-button" title={`移除${job.title}`} aria-label={`移除${job.title}`} onClick={() => setDraft(p => ({ ...p, jobs: p.jobs.filter(j => j.job_id !== job.job_id) }))}><Trash2 size={16} /></button></div>
            <div className="pe-field"><label htmlFor={`pe-count-${index}`}>篇数<small className="pe-origin">{origin(job, 'count')}</small></label><div className="pe-stepper">
              {job.kind === 'daily_news' ? <><button className="icon-button" title="减少篇数" aria-label="减少篇数" onClick={() => updateJob(job.job_id!, { count: Math.max(1, job.count - 1) })}><Minus size={15} /></button>
                <input id={`pe-count-${index}`} aria-label={`${job.title}篇数`} type="number" min={1} max={20} value={job.count} onChange={e => updateJob(job.job_id!, { count: Number(e.target.value) })} />
                <button className="icon-button" title="增加篇数" aria-label="增加篇数" onClick={() => updateJob(job.job_id!, { count: Math.min(20, job.count + 1) })}><Plus size={15} /></button></> : <span>固定1篇集合稿</span>}
            </div></div>
            {topicKinds.has(job.kind) ? <>
              <Tags label={`${job.title}选题偏向`} source={origin(job, 'topic_preferences')} values={job.topic_preferences || []} onChange={v => updateJob(job.job_id!, { topic_preferences: v })} />
              <Tags label={`${job.title}检索词`} source={origin(job, 'search_keywords')} values={job.search_keywords || []} onChange={v => updateJob(job.job_id!, { search_keywords: v })} />
              <div className="pe-field"><label htmlFor={`pe-brief-${index}`}>补充要求<small className="pe-origin">{origin(job, 'topic_brief')}</small></label><textarea id={`pe-brief-${index}`} aria-label={`${job.title}补充要求`} rows={3} maxLength={2000} value={job.topic_brief || ''} onChange={e => updateJob(job.job_id!, { topic_brief: e.target.value, topic_brief_strength: null })} /></div>
              {!!job.topic_brief && <fieldset className="pe-segments"><legend>要求强度</legend>{[['preference', '选题偏好'], ['requirement', '必须满足']].map(([value, label]) => <label key={value}><input type="radio" name={`strength-${job.job_id}`} checked={job.topic_brief_strength === value} onChange={() => updateJob(job.job_id!, { topic_brief_strength: value as 'preference' | 'requirement' })} />{label}</label>)}</fieldset>}
              <div className="pe-field"><label htmlFor={`pe-view-${index}`}>评价视角</label><input id={`pe-view-${index}`} maxLength={500} value={job.evaluation_viewpoint || ''} onChange={e => updateJob(job.job_id!, { evaluation_viewpoint: e.target.value })} /></div>
            </> : <div className="pe-unsupported">{(['search_keywords', 'topic_preferences', 'topic_brief', 'legacy_extra_requirements', 'evaluation_viewpoint'] as const).map(key => {
              const value = job[key];
              if (!value || Array.isArray(value) && !value.length || key === 'evaluation_viewpoint' && value === '无视角评价') return null;
              return <div key={key}><strong>待处理要求</strong><p>{Array.isArray(value) ? value.join('、') : value}</p><small>此栏目尚未接入该字段</small><button className="text-button" onClick={() => updateJob(job.job_id!, { [key]: Array.isArray(value) ? [] : '' })}><X size={14} />移除此要求</button></div>;
            })}</div>}
            {!!job.legacy_extra_requirements && topicKinds.has(job.kind) && <div className="pe-field"><label>旧版附加要求</label><textarea value={job.legacy_extra_requirements} onChange={e => updateJob(job.job_id!, { legacy_extra_requirements: e.target.value })} /></div>}
            {!!job.content_constraints?.length && <section className="pe-constraints"><h4>结构化约束</h4>{job.content_constraints.map((constraint, constraintIndex) => <div key={constraintIndex}>
              <div className="pe-column-heading"><strong>{constraint && typeof constraint === 'object' ? String(constraint.type || '未识别') : '未识别'}</strong><button type="button" className="icon-button" title={`移除约束${constraintIndex + 1}`} aria-label={`移除约束${constraintIndex + 1}`} onClick={() => updateJob(job.job_id!, { content_constraints: job.content_constraints!.filter((_, index) => index !== constraintIndex) })}><Trash2 size={15} /></button></div>
              <p>{displayValue(constraint)}</p>
            </div>)}</section>}
            {(candidate || plan).field_errors?.filter(e => e.field.startsWith(`jobs.${job.job_id}.`)).map(issue => <div className="pe-error" key={issue.id}><p>{issue.message}</p>{['REMOVED_TOPIC_IN_BRIEF', 'LEGACY_EXTRA_REVIEW'].includes(issue.code) && <label><input type="checkbox" checked={decisions.some(d => d.issue_id === issue.id)} onChange={e => setDecisions(v => e.target.checked ? [...v, { issue_id: issue.id, content_hash: issue.content_hash, decision: 'keep_brief' }] : v.filter(d => d.issue_id !== issue.id))} />保留当前说明</label>}</div>)}
          </section>)}
          <div className="pe-add"><select aria-label="待添加栏目" value={addKind} onChange={e => setAddKind(e.target.value)}><option value="">选择栏目</option>{Object.entries(columns).filter(([kind]) => !draft.jobs.some(j => j.kind === kind)).map(([kind, label]) => <option key={kind} value={kind}>{label}</option>)}</select>
            <button className="quiet-button" disabled={!addKind} onClick={() => { setDraft(v => ({ ...v, jobs: [...v.jobs, { kind: addKind, title: columns[addKind], job_id: `new-${crypto.randomUUID()}`, count: 1, search_keywords: [], topic_preferences: [], topic_brief: '', evaluation_viewpoint: '无视角评价' }] })); setAddKind(''); }}><Plus size={15} />添加栏目</button></div>
          <section className="pe-options"><h3>执行选项</h3>
            {([['performance_mode', '运行模式', [['speed', '速度优先'], ['balanced', '速度与稳定平衡']]], ['delivery', '交付方式', [['generate_only', '仅生成本地稿'], ['save_draft', '保存平台草稿']]]] as const).map(([key, label, choices]) => <fieldset className="pe-segments" key={key}><legend>{label}</legend>{choices.map(([value, title]) => <label key={value}><input type="radio" name={key} checked={draft[key] === value} onChange={() => setDraft(d => ({ ...d, [key]: value }))} />{title}</label>)}</fieldset>)}
            <div className="pe-field"><label htmlFor="pe-platform">平台</label><select id="pe-platform" value={draft.platform} onChange={e => setDraft(d => ({ ...d, platform: e.target.value }))}><option value="xhs">小红书</option><option value="toutiao">今日头条</option><option value="both">小红书＋今日头条</option></select></div>
            <label className="pe-check"><input type="checkbox" checked={draft.image_score_required !== false} onChange={e => setDraft(d => ({ ...d, image_score_required: e.target.checked }))} />图片评分硬门槛</label>
            <p className="pe-policy">日期核验：宿主固定策略 · 禁止自动付费切换</p>
            {(['agent', 'writer', 'image'] as const).map(role => <div className="pe-field" key={role}><span>{({ agent: '主控模型', writer: '写稿模型', image: '生图模型' })[role]}</span><RolePicker label={`编辑${role}模型`} value={draft.model_roles?.[role] || ''} choices={models.filter(m => m.kind === (role === 'image' ? 'image' : 'llm')).map(m => ({ id: m.id, label: `${m.provider_name || m.provider} · ${m.model}`, model: m.model, disabledReason: m.role_reasons?.[role] ?? (m.selectable ? '' : m.disabled_reason || '模型不可用') }))} onChange={ref => setDraft(d => ({ ...d, model_roles: { ...d.model_roles, [role]: ref } }))} /></div>)}
          </section>
          {!!candidate?.field_suggestions?.length && <section className="pe-suggestions"><h3>模型对人工字段的建议</h3>{candidate.field_suggestions.map(s => {
            const job = plan.jobs.find(j => s.field_path.startsWith(`jobs.${j.job_id}.`));
            const name = job ? s.field_path.slice(`jobs.${job.job_id}.`.length) : s.field_path;
            const current = job ? job[name as keyof PlanJob] : plan[name as keyof Plan];
            const label = `${job ? job.title + ' · ' : ''}${fieldLabels[name] || '任务属性'}`;
            return <label key={s.field_path}><input type="checkbox" aria-label={`采用建议${label}`} checked={accepted.includes(s.field_path)} onChange={e => setAccepted(v => e.target.checked ? [...v, s.field_path] : v.filter(p => p !== s.field_path))} /><span><strong>{label}</strong><br />当前：{displayValue(current)} → 建议：{displayValue(s.value)}<br />{s.reason}</span></label>;
          })}</section>}
          <section className="pe-diff"><h3>本次变化</h3>{!changed ? <span>未修改</span> : <ul>{draft.jobs.map(j => { const old = initial.jobs.find(v => v.job_id === j.job_id); return <li key={j.job_id}>{j.title}：{old ? `${old.count} → ${j.count}篇` : '新栏目'}{!same(old?.topic_preferences, j.topic_preferences) && `；偏向：${j.topic_preferences?.join('、') || '已清空'}`}{!same(old?.search_keywords, j.search_keywords) && `；检索词：${j.search_keywords?.join('、') || '已清空'}`}</li>; })}{initial.jobs.filter(j => !draft.jobs.some(v => v.job_id === j.job_id)).map(j => <li key={j.job_id}>移除{j.title}</li>)}</ul>}</section>
          {error && <p className="pe-error" role="alert">{error}</p>}
        </fieldset>
        <footer className="pe-footer">{calibrationChoice ? <><span>校准前需处理未保存的修改</span>
          <button ref={returnToEditor} className="quiet-button" onClick={() => setCalibrationChoice(false)}>返回编辑</button>
          <button className="quiet-button" onClick={() => void calibrate()}>放弃修改后校准</button>
          <button className="primary-button" onClick={() => void save(true)}><Save size={15} />保存后校准</button>
        </> : discard ? <><span>有尚未保存的修改</span><button className="quiet-button" onClick={() => setDiscard(false)}>继续编辑</button><button className="quiet-button" onClick={close}>放弃修改</button></> : <><button className="quiet-button" disabled={saving} onClick={dismiss}>取消</button><button className="primary-button" disabled={saving || !changed && !candidate} onClick={() => void save()}><Save size={15} />{saving ? '处理中' : candidate ? '保存并采用' : '保存计划'}</button></>}</footer>
      </div>
    </div>}
  </>;
}
