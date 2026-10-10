import { useEffect, useState } from "react";
import { Save, Settings2 } from "lucide-react";
import { api, type Plan } from "../api";
import { Empty, ErrorLine, Fields, Json, Modes, ReadState, Status, encoded, navigate, text, useRead, type Item } from "./shared";
import "../capabilities.css";

export type PlanSelection = { skill_mode: string; skill_names: string[] };
export async function readPlanSelection(conversationId: string, planId: string): Promise<PlanSelection> {
  const result = await api<Item>(`/api/conversations/${encoded(conversationId)}/plans/${encoded(planId)}/capabilities`);
  if (!["off", "auto", "manual"].includes(result.skill_mode) || !Array.isArray(result.skill_names)) throw new Error("本次技能选择未返回，请刷新后重试");
  return { skill_mode: result.skill_mode, skill_names: result.skill_names || [] };
}
export function PlanCapabilities({ conversationId, plan, runId, disabled, onChanged, onPending }: {
  conversationId: string; plan: Plan; runId?: string; disabled: boolean;
  onChanged: (plan: Plan) => void; onPending: (pending: boolean) => void;
}) {
  const path = runId ? `/api/runs/${encoded(runId)}/capabilities` : `/api/conversations/${encoded(conversationId)}/plans/${encoded(plan.id)}/capabilities`;
  const detail = useRead<Item>(path, plan.version);
  const [mode, setMode] = useState("off"), [names, setNames] = useState<string[]>([]), [disabledTools, setDisabledTools] = useState<string[]>([]), [busy, setBusy] = useState(false), [error, setError] = useState<unknown>();
  const [adjusting, setAdjusting] = useState(false);
  useEffect(() => { if (detail.data) { setMode(detail.data.skill_mode || "off"); setNames(detail.data.skill_names || []); setDisabledTools(detail.data.disabled_tools || []); setAdjusting(false); } }, [detail.data]);
  const changed = !!detail.data && (mode !== detail.data.skill_mode || JSON.stringify(names) !== JSON.stringify(detail.data.skill_names || []) || JSON.stringify(disabledTools) !== JSON.stringify(detail.data.disabled_tools || []));
  const blocked = detail.data?.readiness?.ready === false || detail.data?.readiness?.status === "blocked";
  useEffect(() => { onPending(busy || changed || blocked); return () => onPending(false); }, [busy, changed, blocked, onPending]);
  const frozen = !!runId || !!detail.data?.frozen || disabled;
  return <details className="cap-plan"><summary>本次能力</summary><ReadState {...detail} /><ErrorLine error={error} retry={detail.reload} />{detail.data && (detail.data.not_recorded || detail.data.status === "not_recorded" ? <Empty>该版本未采集</Empty> : <>
    <span className="cap-plan-state">{frozen ? `只读快照 ${text(detail.data.snapshot_id || detail.data.version)}` : `计划版本 ${detail.data.version}`}</span>
    <h3>工具</h3>{detail.data.tools?.length ? detail.data.tools.map((tool: Item) => <div className="cap-plan-tool" key={tool.id || tool.resource_id}><button className="text-button" onClick={() => navigate("tools", tool.id || tool.resource_id)}>{text(tool.name || tool.resource_name || tool.id)}</button>{adjusting && !frozen && <label><input type="checkbox" aria-label={`本次允许${tool.name || tool.id}`} checked={!disabledTools.includes(tool.id || tool.resource_id)} onChange={e => setDisabledTools(previous => e.target.checked ? previous.filter(id => id !== (tool.id || tool.resource_id)) : [...previous, tool.id || tool.resource_id])} /></label>}<small>{text(tool.version)} / {tool.called ? "已调用" : tool.selected ? "本次已选" : "候选"}</small></div>) : <p className="muted">未登记工具</p>}
    <h3>SKILLS</h3><Modes value={mode} disabled={frozen || !adjusting} onChange={setMode} /><p className="muted">已选 {names.length} 项{mode === "auto" ? " / 自动匹配" : ""}</p>{(adjusting && !frozen ? detail.data.skill_candidates || detail.data.skills : detail.data.skills)?.map((skill: Item) => <div className="cap-plan-tool" key={skill.id || skill.name}><button className="text-button" onClick={() => skill.id && navigate("skills", skill.id)}>{text(skill.name)}</button>{mode === "manual" && adjusting && !frozen && <input type="checkbox" aria-label={`本次选择${skill.name}`} checked={names.includes(skill.id || skill.name)} disabled={!names.includes(skill.id || skill.name) && names.length >= 3} onChange={e => setNames(previous => e.target.checked ? [...previous, skill.id || skill.name] : previous.filter(name => name !== (skill.id || skill.name)))} />}<small>{skill.loaded ? `已加载 ${skill.loaded_bodies ?? 1} 项正文 / ${skill.loaded_resources ?? 0} 个附件` : skill.selected || names.includes(skill.id || skill.name) ? "已选但尚未加载" : "已启用但本次未选中"}</small></div>)}
    <h3>记忆</h3>{Array.isArray(detail.data.memory) ? (detail.data.memory.length ? detail.data.memory.map((memory: Item) => <section className="cap-band" key={memory.id}><p>{text(memory.content)}</p><Fields values={[["来源", memory.source_ref], ["作用范围", memory.scope], ["适用栏目", memory.applies_to], ["修订", memory.revision]]} /><p className="muted">历史偏好；本次明确要求优先。</p>{memory.overridden_preferences?.length > 0 && <details><summary>被覆盖的旧偏好（{memory.overridden_preferences.length}）</summary>{memory.overridden_preferences.map((old: Item) => <section className="cap-band" key={old.id}><p>{text(old.content)}</p><Fields values={[["旧偏好来源", old.source_ref], ["旧偏好修订", old.revision], ["覆盖原因", old.reason]]} /></section>)}</details>}</section>) : <p className="muted">本次未选择长期偏好</p>) : <Json value={detail.data.memory} />}<h3>模型与浏览器</h3><Fields values={[["模型绑定", detail.data.models], ["profile", detail.data.profile]]} /><h3>检查</h3>{typeof detail.data.readiness === "string" ? <Status value={detail.data.readiness} /> : <Json value={detail.data.readiness} />}
    {!frozen && (!adjusting ? <button className="quiet-button" onClick={() => setAdjusting(true)}><Settings2 size={15} />调整本次能力</button> : <button className="quiet-button" disabled={busy || !changed || names.length > 3} onClick={async () => {
      setBusy(true); setError(undefined);
      try { const result = await api<Item>(path, "PUT", { version: plan.version, skill_mode: mode, skill_names: mode === "manual" ? names : [], disabled_tools: disabledTools });
        if (!result.plan) throw new Error("接口未返回更新后的计划，请刷新后重试");
        onChanged(result.plan); setAdjusting(false);
      } catch (e) { setError(e); } finally { setBusy(false); }
    }}><Save size={15} />应用本次能力</button>)}
    {runId && <><h3>实际调用</h3><Json value={detail.data.calls} /><h3>恢复差异</h3><Json value={detail.data.resume_diff} /></>}
  </>)}</details>;
}
