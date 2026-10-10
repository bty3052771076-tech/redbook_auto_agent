import { useState } from 'react';
import { Copy, History, RotateCcw } from 'lucide-react';
import { api, type Conversation, type Plan } from './api';

export function PlanHistory({ conversation, plan, disabled, onSaved }: {
  conversation: Conversation; plan: Plan; disabled: boolean; onSaved: () => Promise<void>;
}) {
  const [selected, setSelected] = useState(''), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const historical = conversation.plans.filter(row => row.id !== plan.id);
  const target = historical.find(row => row.id === selected);
  return <details className="pe-history"><summary><History size={14} />计划修订记录</summary>
    <ol>{[...conversation.plans].reverse().map(row => <li key={row.id}>
      <strong>版本 {row.version}{row.id === plan.id ? ' · 当前' : ''}</strong>
      <span>{row.jobs.map(job => `${job.title} ${job.count}篇`).join('、')}</span>
      <small>{row.last_editor === 'user' ? '人工修订' : row.recognition_source === 'llm' ? '模型候选' : '本地草案'}</small>
      {row.id !== plan.id && <button className="text-button" disabled={disabled || busy} onClick={() => { setSelected(row.id); setError(''); }}><RotateCcw size={13} />恢复此版本</button>}
    </li>)}</ol>
    {target && <section className="pe-restore" aria-label="确认恢复历史计划">
      <p>将版本 {target.version} 恢复为新修订。原记录保留，不会启动任务。</p>
      <button className="quiet-button" disabled={busy} onClick={() => setSelected('')}>取消恢复</button>
      <button className="quiet-button" disabled={disabled || busy} onClick={async () => {
        setBusy(true); setError('');
        try {
          await api(`/api/conversations/${conversation.id}/plans/${plan.id}/restore`, 'POST', {
            base_plan_version: plan.version, conversation_revision: conversation.conversation_revision,
            restore_plan_id: target.id, editable_fields: {}, review_decisions: [],
          }, `restore-${plan.id}-${target.id}`);
          await onSaved(); setSelected('');
        } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
        finally { setBusy(false); }
      }}><RotateCcw size={14} />{busy ? '恢复中' : '确认恢复为新修订'}</button>
      {error && <p className="pe-error" role="alert">{error}</p>}
    </section>}
  </details>;
}

export function CopyPlan({conversation,plan,disabled,onSaved}:{conversation:Conversation;plan:Plan;disabled:boolean;onSaved:()=>Promise<void>}) {
  const [open,setOpen]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('');
  return <><button className="quiet-button" disabled={disabled || busy} onClick={()=>{setOpen(true);setError('');}}><Copy size={14}/>复制为新计划</button>
    {open && <section className="pe-restore" aria-label="确认复制计划"><p>版本 {plan.version} 保持冻结。复制后需要重新确认，不会立即执行。</p>
      <button className="quiet-button" disabled={busy} onClick={()=>setOpen(false)}>取消复制</button>
      <button className="quiet-button" disabled={disabled || busy} onClick={async()=>{
        setBusy(true);setError('');
        try {
          await api(`/api/conversations/${conversation.id}/plans/${plan.id}/copy`,'POST',{
            base_plan_version:plan.version,conversation_revision:conversation.conversation_revision,
          },`copy-${plan.id}-${conversation.conversation_revision}`);
          await onSaved();setOpen(false);
        } catch(cause) {setError(cause instanceof Error?cause.message:String(cause));}
        finally {setBusy(false);}
      }}><Copy size={14}/>{busy?'复制中':'确认复制为新计划'}</button>
      {error && <p className="pe-error" role="alert">{error}</p>}
    </section>}
  </>;
}
