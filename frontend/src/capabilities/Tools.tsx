import { useState } from "react";
import { Play, Search, ShieldOff, ShieldCheck, Save } from "lucide-react";
import { api } from "../api";
import { CopyId, Drawer, Empty, ErrorLine, Fields, Json, Operation, Pager, ReadState, Status, Switch, encoded, filter, navigate, query, text, useOperation, useRead, when, type Item, type Listing } from "./shared";

export function Tools({ id, refresh }: { id: string; refresh: number }) {
  const [revision, setRevision] = useState(0), [selected, setSelected] = useState<string[]>([]);
  const [error, setError] = useState<unknown>(), [saving, setSaving] = useState("");
  const q = filter("query"), kind = filter("kind"), group = filter("group"), status = filter("status"), cursor = filter("cursor");
  const listing = useRead<Listing>(`/api/capabilities${query({ query: q, kind, group, status, cursor, limit: 50 })}`, refresh + revision);
  const operation = useOperation(() => setRevision(v => v + 1));
  const changeFilter = (key: string, value: string) => { setSelected([]); navigate("tools", "", { [key]: value, cursor: "" }); };
  async function toggle(row: Item) {
    setSaving(row.id!); setError(undefined);
    try { await api(`/api/capabilities/${encoded(row.id!)}`, "PATCH", { expected_revision: row.revision, enabled: !row.enabled }); setRevision(v => v + 1); }
    catch (e) { setError(e); } finally { setSaving(""); }
  }
  return <><div className="cap-toolbar"><label className="cap-search"><Search size={16} /><input aria-label="搜索名称或用途" value={q} onChange={e => changeFilter("query", e.target.value)} /></label>
    <select aria-label="工具来源" value={kind} onChange={e => changeFilter("kind", e.target.value)}><option value="">全部来源</option><option value="builtin">内置</option><option value="mcp">MCP</option></select>
    <select aria-label="能力组" value={group} onChange={e => changeFilter("group", e.target.value)}><option value="">全部能力组</option>{Array.from(new Set([group, ...(listing.data?.rows.map(row => row.group) || [])].filter(Boolean))).map(value => <option key={value} value={value}>{value}</option>)}</select>
    <select aria-label="工具状态" value={status} onChange={e => changeFilter("status", e.target.value)}><option value="">全部状态</option>{["ready", "unknown", "blocked", "stale", "disabled"].map(v => <option key={v} value={v}>{{ ready: "就绪", unknown: "待检测", blocked: "不可用", stale: "检测过期", disabled: "停用" }[v]}</option>)}</select>
    <button className="quiet-button" disabled={listing.data?.read_only || !selected.length || ["running", "checking", "pending"].includes(operation.operation?.status)} onClick={() => operation.start("/api/capabilities/checks", { resource_ids: selected })}><ShieldCheck size={16} />检查所选</button></div>
    <ErrorLine error={error} retry={listing.reload} /><ReadState {...listing} /><ErrorLine error={listing.data?.error} retry={listing.reload} /><Operation state={operation} />
    <table className="cap-table"><thead><tr><th className="cap-check-col">选择</th><th>名称及用途</th><th className="cap-secondary">来源 / 能力组</th><th>状态</th><th className="cap-secondary">使用范围</th><th>开关</th></tr></thead><tbody>{listing.data?.rows.map(row => <tr key={row.id}>
      <td><input type="checkbox" aria-label={`选择${row.name}`} disabled={row.configuration_available === false} checked={selected.includes(row.id!)} onChange={e => setSelected(prev => e.target.checked ? [...prev, row.id!] : prev.filter(value => value !== row.id))} /></td>
      <td><button className="cap-row-link" onClick={() => navigate("tools", row.id!)}>{row.name}</button><small>{text(row.description, "")}</small></td>
      <td className="cap-secondary">{row.kind === "builtin" ? "内置" : text(row.kind)}<small>{text(row.group, "")}</small></td>
      <td><Status value={row.health?.status} /><small>{when(row.health?.observed_at)}</small></td><td className="cap-secondary">{text(row.stages)}</td>
      <td><Switch label={`启用${row.name}`} value={!!row.enabled} disabled={saving === row.id || row.configuration_available === false} onChange={() => toggle(row)} />{row.configuration_available === false ? <small>配置未读取</small> : !row.enabled && row.active_runs?.length > 0 && <small>新任务已停用；{row.active_runs.length} 个运行任务仍使用冻结版本</small>}</td>
    </tr>)}</tbody></table>{listing.data && !listing.data.rows.length && <Empty>没有匹配的工具</Empty>}
    <Pager next={listing.data?.next_cursor} cursor={cursor} onChange={value => navigate("tools", "", { cursor: value })} />
    {id && <ToolDetail key={id} id={id} refresh={refresh + revision} onChanged={() => setRevision(v => v + 1)} />}</>;
}

function ToolDetail({ id, refresh, onChanged }: { id: string; refresh: number; onChanged: () => void }) {
  const detail = useRead<Item>(`/api/capabilities/${encoded(id)}`, refresh);
  const [timeout, setTimeout] = useState(""), [reason, setReason] = useState("");
  const [error, setError] = useState<unknown>(), [saving, setSaving] = useState(false);
  const [trialOpen, setTrialOpen] = useState(false), [trialInput, setTrialInput] = useState(""), [trialPreview, setTrialPreview] = useState<Item | null>(null), [acknowledged, setAcknowledged] = useState(false);
  const row = detail.data;
  const dirty = !!timeout || !!reason || trialOpen;
  const operation = useOperation(onChanged);
  async function save(revoke = false) {
    if (!row) return;
    setSaving(true); setError(undefined);
    try { await api(`/api/capabilities/${encoded(id)}${revoke ? "/revoke" : ""}`, revoke ? "POST" : "PATCH", {
      expected_revision: row.revision, ...(revoke ? { reason } : { timeout_seconds: Number(timeout), reason }),
    }); setTimeout(""); setReason(""); onChanged(); }
    catch (e) { setError(e); } finally { setSaving(false); }
  }
  return <Drawer title={row?.name || "工具详情"} dirty={dirty} onClose={() => navigate("tools")}><ReadState {...detail} /><ErrorLine error={error} retry={detail.reload} />{row && <>
    <CopyId id={id} /><p>{text(row.description)}</p><Status value={row.health?.status} /><Fields values={[["来源", row.kind], ["版本 / 修订", `${text(row.version)} / ${row.revision}`], ["最近检查", when(row.health?.observed_at)], ["探针", row.health?.probe], ["作用阶段", row.stages], ["影响", row.effects], ["并发", row.concurrency_policy || row.concurrency], ["幂等",row.idempotency], ["恢复",row.recovery]]} />
    <ErrorLine error={row.health?.error} /><h3>输入字段</h3>{row.input_schema?.description && <p>{text(row.input_schema.description)}</p>}{Object.keys(row.input_schema?.properties || {}).length ? <Fields values={Object.entries(row.input_schema.properties).map(([name, schema]) => [name, (schema as Item).description || (schema as Item).type])} /> : <p className="muted">{row.input_schema?.description ? "无固定字段" : "尚未登记独立输入字段"}</p>}
    <h3>输出字段</h3>{row.output_schema?.description && <p>{text(row.output_schema.description)}</p>}{Object.keys(row.output_schema?.properties || row.output_schema?.items?.properties || {}).length ? <Fields values={Object.entries(row.output_schema.properties || row.output_schema.items.properties).map(([name, schema]) => [name, (schema as Item).description || (schema as Item).type])} /> : row.output_schema?.prefixItems?.length ? <Fields values={row.output_schema.prefixItems.map((schema:Item,index:number) => [`结果${index+1}`,schema.description || schema.type])} /> : <p className="muted">{row.output_schema?.description ? "无固定字段" : "尚未登记独立输出字段"}</p>}
    <h3>依赖</h3>{row.dependencies?.length ? (row.dependency_details || row.dependencies).map((dependency: Item | string, i: number) => typeof dependency === "string" ? <p key={i}>{dependency}</p> : <button key={i} className="text-button" onClick={() => navigate(dependency.kind === "mcp" ? "mcp" : dependency.kind === "skill" ? "skills" : "tools", dependency.resource_id || dependency.id)}>{text(dependency.name || dependency.resource_id || dependency.id)}</button>) : <p className="muted">未登记依赖</p>}
    <h3>正在使用的任务</h3>{row.active_runs?.length ? row.active_runs.map((run: Item) => <Fields key={run.id || run.run_id} values={[["任务", run.id || run.run_id], ["冻结版本", run.version], ["在途调用", run.in_flight_calls]]} />) : <p className="muted">没有运行任务</p>}
    <h3>最近调用</h3>{row.recent_calls?.length ? row.recent_calls.map((call: Item) => <button className="cap-row-link" key={call.id} onClick={() => navigate("calls", call.id!, { resource_id: id })}>{when(call.started_at)} / {text(call.status)}</button>) : <p className="muted">未记录调用</p>}
    <button className="quiet-button" onClick={() => operation.start("/api/capabilities/checks", { resource_ids: [id] })}><ShieldCheck size={16} />检查可用性</button><Operation state={operation} />
    {row.trial?.supported && <button className="quiet-button" disabled={!row.enabled || saving} onClick={() => {setTrialOpen(true);setTrialInput(JSON.stringify(row.trial.template,null,2));setTrialPreview(null);setAcknowledged(false);}}><Play size={16} />试运行</button>}
    {trialOpen && <section className="cap-band"><h3>单次试运行</h3><p>{text(row.trial.description)}</p><label className="cap-field">试运行输入（JSON）<textarea aria-label="试运行输入（JSON）" rows={7} value={trialInput} onChange={event => {setTrialInput(event.target.value);setTrialPreview(null);setAcknowledged(false);}} /></label><button className="quiet-button" disabled={saving} onClick={async () => {
      setSaving(true);setError(undefined);setTrialPreview(null);setAcknowledged(false);
      try {const value = JSON.parse(trialInput);if (!value || Array.isArray(value) || typeof value !== "object") throw new Error("输入须为 JSON 对象");setTrialPreview(await api<Item>(`/api/capabilities/${encoded(id)}/trial-preview`,"POST",{...value,expected_revision:row.revision}));}
      catch (e) {setError(e);} finally {setSaving(false);}
    }}><Search size={16} />核对本次输入</button>{trialPreview && <><Fields values={[["精确输入",trialPreview.input],["影响",trialPreview.effects],["模型",trialPreview.models],["专用 profile",trialPreview.profile],["预览有效期",when(trialPreview.expires_at)]]} /><label className="cap-checkbox"><input type="checkbox" checked={acknowledged} onChange={event => setAcknowledged(event.target.checked)} />已核对输入及额度、文件和平台写入影响</label><button className="primary-button" disabled={!acknowledged || saving || ["queued","running","pending"].includes(operation.operation?.status) || Date.now()/1000>trialPreview.expires_at} onClick={() => {setTrialOpen(false);void operation.start(`/api/capability-trials/${encoded(trialPreview.preview_id)}/confirm`,{preview_hash:trialPreview.preview_hash,acknowledge_effects:true});}}><Play size={16} />确认本次试运行</button></>}<button className="quiet-button" disabled={saving} onClick={() => setTrialOpen(false)}>取消</button></section>}
    {row.timeout_configurable && <form onSubmit={e => { e.preventDefault(); save(); }}><label className="cap-field">单次请求超时秒数<input type="number" min="1" max={row.max_timeout_seconds || 600} placeholder="适配器默认" value={timeout || row.timeout_seconds || ""} onChange={e => setTimeout(e.target.value)} required /></label><button className="quiet-button" disabled={saving || !timeout}><Save size={16} />保存参数</button></form>}
    <details><summary>停止后续调用</summary><p>影响 {row.active_runs?.length || 0} 个运行任务；在途外部请求仍需核对，已完成产物保留。</p><label className="cap-field">撤销原因<textarea value={reason} onChange={e => setReason(e.target.value)} /></label><button className="quiet-button cap-danger" disabled={saving || !reason.trim()} onClick={() => save(true)}><ShieldOff size={16} />确认停止后续调用</button></details>
    <details><summary>高级详情与版本</summary><Json value={{ binding: row.binding, input_schema: row.input_schema, output_schema: row.output_schema, versions: row.versions }} /></details>
  </>}</Drawer>;
}
