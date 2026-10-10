import { useRef, useState } from "react";
import { FileInput, Plus, Save, Search, ShieldCheck, Trash2 } from "lucide-react";
import { api } from "../api";
import { CopyId, Drawer, Empty, ErrorLine, Fields, Json, Operation, ReadState, Status, Switch, encoded, filter, navigate, text, useOperation, useRead, when, type Item, type Listing } from "./shared";

export function Mcp({ id, refresh }: { id: string; refresh: number }) {
  const [revision, setRevision] = useState(0), [selected, setSelected] = useState<string[]>([]);
  const [error, setError] = useState<unknown>(), [saving, setSaving] = useState("");
  const [seed, setSeed] = useState<Item | null>(null);
  const listing = useRead<Listing>("/api/mcp/connections", refresh + revision);
  const operation = useOperation(() => setRevision(v => v + 1));
  const q = filter("query");
  const rows = (listing.data?.rows || []).filter(row => text(row.name).toLowerCase().includes(q.toLowerCase()));
  const row = listing.data?.rows.find(row => row.id === id);
  const lastDetail = useRef<Item | undefined>(undefined);
  if (row) lastDetail.current = row;
  const detail = row || (listing.loading && lastDetail.current?.id === id ? lastDetail.current : undefined);
  const changed = () => setRevision(v => v + 1);
  async function toggle(row: Item) {
    setSaving(row.id!); setError(undefined);
    try { await api(`/api/mcp/connections/${encoded(row.id!)}`, "PATCH", { expected_revision: row.revision, enabled: !row.enabled }); changed(); }
    catch (e) { setError(e); } finally { setSaving(""); }
  }
  return <><div className="cap-toolbar"><label className="cap-search"><Search size={16} /><input aria-label="搜索连接" value={q} onChange={e => navigate("mcp", "", { query: e.target.value })} /></label>
    <button className="quiet-button" onClick={() => { setSeed(null); navigate("mcp", "new"); }}><Plus size={16} />添加连接</button><button className="quiet-button" onClick={() => navigate("mcp", "import")}><FileInput size={16} />导入配置</button>
    <button className="quiet-button" disabled={!selected.length} onClick={() => operation.start("/api/mcp/checks", { connection_ids: selected })}><ShieldCheck size={16} />检查所选</button></div>
    <ReadState {...listing} /><ErrorLine error={error} retry={listing.reload} /><Operation state={operation} />
    <table className="cap-table"><thead><tr><th className="cap-check-col">选择</th><th>连接名称</th><th className="cap-secondary">传输 / 工具数</th><th>健康</th><th className="cap-secondary">进程 / 错误</th><th>开关</th></tr></thead><tbody>{rows.map(row => <tr key={row.id}><td><input type="checkbox" aria-label={`选择${row.name}`} checked={selected.includes(row.id!)} onChange={e => setSelected(prev => e.target.checked ? [...prev, row.id!] : prev.filter(value => value !== row.id))} /></td>
      <td><button className="cap-row-link" onClick={() => navigate("mcp", row.id!)}>{text(row.name)}</button></td><td className="cap-secondary">{row.transport === "stdio" ? "本地 stdio" : "Streamable HTTP"}<small>允许 {row.allowed_tool_count ?? row.tools?.filter((t: Item) => t.enabled).length ?? "未记录"} / 发现 {row.tool_count ?? row.tools?.length ?? "未记录"}</small></td>
      <td><Status value={row.health?.status} /><small>{when(row.health?.observed_at)}</small></td><td className="cap-secondary"><Status value={row.process_status || "idle"} /><small>{text(row.health?.error || row.last_error, "")}</small></td>
      <td><Switch label={`启用${row.name}`} value={!!row.enabled} disabled={saving === row.id} onChange={() => toggle(row)} /></td></tr>)}</tbody></table>{listing.data && !rows.length && <Empty>暂无连接</Empty>}
    {id === "new" && <ConnectionForm key={seed?.name || "new"} seed={seed || undefined} onSaved={() => { changed(); navigate("mcp", "", undefined, false, true); }} />}
    {id === "import" && <ImportConfig onSelect={value => { setSeed(value); navigate("mcp", "new", undefined, false, true); }} />}
    {id && !["new", "import"].includes(id) && (detail ? <ConnectionDetail key={id} row={detail} onChanged={changed} /> : <Drawer title="连接详情" onClose={() => navigate("mcp")}><ReadState {...listing} />{listing.data && <Empty>连接不存在或已退役</Empty>}</Drawer>)}
  </>;
}

function ConnectionForm({ row, seed, onSaved }: { row?: Item; seed?: Item; onSaved: () => void }) {
  const initial = row || seed || {};
  const [baseRevision] = useState(row?.revision);
  const [network, setNetwork] = useState(initial.network_policy?.mode || (initial.transport === "streamable_http" ? "inherit" : "direct"));
  const [proxy, setProxy] = useState(initial.network_policy?.proxy_url || "");
  const [values, setValues] = useState({ name: initial.name || "", transport: initial.transport || "stdio", command: initial.command || "", args: JSON.stringify(initial.args || []), cwd: initial.cwd || "", url: initial.url || "", environment: "", headers: "", startup_timeout_seconds: initial.startup_timeout_seconds ?? 30, timeout_seconds: initial.timeout_seconds ?? 60 });
  const [dirty, setDirty] = useState(false), [saving, setSaving] = useState(false), [error, setError] = useState<unknown>();
  function field(key: keyof typeof values, value: string) { setValues(prev => ({ ...prev, [key]: value })); setDirty(true); }
  async function save() {
    setSaving(true); setError(undefined);
    try {
      const args = JSON.parse(values.args);
      if (!Array.isArray(args) || args.some(arg => typeof arg !== "string")) throw new Error("参数必须是字符串数组");
      const body: Item = { name: values.name, transport: values.transport, command: values.transport === "stdio" ? values.command : "", args, cwd: values.cwd, url: values.transport === "streamable_http" ? values.url : "", startup_timeout_seconds: Number(values.startup_timeout_seconds), timeout_seconds: Number(values.timeout_seconds), enabled: row?.enabled ?? false };
      body.network_policy = { mode:network, ...(network === "custom" ? {proxy_url:proxy} : {}) };
      if (seed?.import_ref) body.import_ref = seed.import_ref;
      for (const key of ["environment", "headers"] as const) if (values[key].trim()) {
        const parsed = JSON.parse(values[key]);
        if (!parsed || Array.isArray(parsed) || typeof parsed !== "object" || Object.values(parsed).some(value => typeof value !== "string")) throw new Error("环境变量和 Header 必须是字符串键值对象");
        body[key] = parsed;
      }
      if (row) body.expected_revision = baseRevision;
      await api(row ? `/api/mcp/connections/${encoded(row.id!)}` : "/api/mcp/connections", row ? "PATCH" : "POST", body);
      setDirty(false); setValues(prev => ({ ...prev, environment: "", headers: "" })); onSaved();
    } catch (e) { setError(e); } finally { setSaving(false); }
  }
  return <Drawer title={row ? "编辑连接" : "添加连接"} dirty={dirty} onClose={() => navigate("mcp", row?.id || "")}><ErrorLine error={error} /><form onSubmit={e => { e.preventDefault(); save(); }}>
    {row && row.revision !== baseRevision && <p role="status">连接配置已更新，当前修改仍基于旧版本；保存将检查冲突，不覆盖新版配置。</p>}
    <label className="cap-field">连接名称<input value={values.name} onChange={e => field("name", e.target.value)} required maxLength={160} /></label>
    <label className="cap-field">传输方式<select aria-label="传输方式" value={values.transport} onChange={e => field("transport", e.target.value)}><option value="stdio">本地 stdio</option><option value="streamable_http">Streamable HTTP</option></select></label>
    {values.transport === "stdio" ? <><label className="cap-field">执行文件<input value={values.command} onChange={e => field("command", e.target.value)} required /></label><label className="cap-field">参数（JSON 数组）<textarea value={values.args} onChange={e => field("args", e.target.value)} /></label><label className="cap-field">工作目录<input value={values.cwd} onChange={e => field("cwd", e.target.value)} /></label></> : <label className="cap-field">HTTP 地址<input type="url" value={values.url} onChange={e => field("url", e.target.value)} required /></label>}
    <label className="cap-field">网络策略<select aria-label="网络策略" value={network} onChange={event => {setNetwork(event.target.value);setDirty(true);}}><option value="direct">直连</option><option value="inherit">继承进程代理</option><option value="custom">指定代理</option></select></label>
    {network === "custom" && <label className="cap-field">代理地址<input type="url" value={proxy} placeholder="http://127.0.0.1:7890" required onChange={event => {setProxy(event.target.value);setDirty(true);}} /></label>}
    <label className="cap-field">环境变量（JSON，只写）<textarea autoComplete="off" value={values.environment} onChange={e => field("environment", e.target.value)} /></label>
    <label className="cap-field">Header（JSON，只写）<textarea autoComplete="off" value={values.headers} onChange={e => field("headers", e.target.value)} /></label>
    {row && <p className="muted">留空保留已配置的密钥引用。</p>}
    {seed?.import_ref && <p className="muted">导入的敏感配置已保存在本机，保存时使用该引用。</p>}
    <label className="cap-field">启动超时（秒）<input type="number" min="1" max="3600" value={values.startup_timeout_seconds} onChange={e => field("startup_timeout_seconds", e.target.value)} /></label>
    <label className="cap-field">调用超时（秒）<input type="number" min="1" max="3600" value={values.timeout_seconds} onChange={e => field("timeout_seconds", e.target.value)} /></label>
    {!row && <p className="muted">保存为未启用连接。</p>}<button className="primary-button" disabled={saving}><Save size={16} />保存连接</button>
  </form></Drawer>;
}

function ImportConfig({ onSelect }: { onSelect: (value: Item) => void }) {
  const [config, setConfig] = useState(""), [preview, setPreview] = useState<Item | null>(null), [error, setError] = useState<unknown>(), [busy, setBusy] = useState(false);
  return <Drawer title="导入配置" dirty={!!config} onClose={() => navigate("mcp")}><label className="cap-field">MCP JSON 配置<textarea rows={10} value={config} onChange={e => { setConfig(e.target.value); setPreview(null); }} /></label><ErrorLine error={error} /><button className="quiet-button" disabled={busy || !config.trim()} onClick={async () => { setBusy(true); setError(undefined); try { setPreview(await api<Item>("/api/mcp/import-preview", "POST", { config: JSON.parse(config) })); } catch (e) { setError(e); } finally { setBusy(false); } }}><FileInput size={16} />预览配置</button>
    {preview && <><h3>配置预览</h3><Json value={preview.issues || preview.unsupported_fields || []} />{(preview.rows || preview.connections || []).map((connection: Item) => <button key={connection.name} className="quiet-button" onClick={() => { setConfig(""); window.setTimeout(() => onSelect(connection), 0); }}>填入 {text(connection.name)}</button>)}</>}
  </Drawer>;
}

function ConnectionDetail({ row, onChanged }: { row: Item; onChanged: () => void }) {
  const [editing, setEditing] = useState(false), [retiring, setRetiring] = useState(false), [error, setError] = useState<unknown>();
  const operation = useOperation(onChanged);
  if (editing) return <ConnectionForm row={row} onSaved={() => { onChanged(); setEditing(false); }} />;
  return <Drawer title={row.name} onClose={() => navigate("mcp")}><CopyId id={row.id!} /><Fields values={[["传输", row.transport], ["执行文件", row.command], ["参数", row.args], ["工作目录", row.cwd], ["HTTP 地址", row.url], ["配置版本", row.revision], ["协议版本", row.protocol_version], ["最近检测", when(row.health?.observed_at)], ["进程", row.process_status || "按需启动 / 空闲"], ["网络策略", row.network_policy]]} />
    <Status value={row.health?.status} /><ErrorLine error={row.health?.error || row.last_error} /><ErrorLine error={error} />
    <h3>敏感配置</h3><Fields values={Object.entries(row.environment || row.environment_configured || {}).map(([name, value]) => [name, value ? "已配置" : "未配置"])} /><Fields values={Object.entries(row.headers || row.headers_configured || {}).map(([name, value]) => [name, value ? "已配置" : "未配置"])} />
    <div className="cap-actions"><button className="quiet-button" onClick={() => setEditing(true)}><Save size={16} />编辑连接</button><button className="quiet-button" onClick={() => operation.start(`/api/mcp/connections/${encoded(row.id!)}/discover`, {})}><ShieldCheck size={16} />检测连接</button><button className="quiet-button" onClick={() => operation.start(`/api/mcp/connections/${encoded(row.id!)}/discover`, {})}><Search size={16} />检测并发现工具</button></div><Operation state={operation} />
    <h3>发现工具</h3>{row.tools?.length ? row.tools.map((tool: Item) => <ToolPolicy key={`${row.revision}:${tool.name || tool.tool_name}:${tool.schema_hash}`} connection={row} tool={tool} onChanged={onChanged} />) : <Empty>尚未发现工具</Empty>}
    <details><summary>日志与版本差异</summary><Json value={{ logs: row.logs, stderr: row.stderr, exit_code: row.exit_code, schema_diff: row.schema_diff, discovery_diff: row.discovery_diff }} /></details>
    <details><summary>退役连接</summary><p>后续新任务不可再选择此连接，已完成产物保留。</p><button className="quiet-button cap-danger" disabled={retiring} onClick={async () => { setRetiring(true); setError(undefined); try { await api(`/api/mcp/connections/${encoded(row.id!)}?expected_revision=${row.revision}`, "DELETE"); onChanged(); navigate("mcp"); } catch (e) { setError(e); } finally { setRetiring(false); } }}><Trash2 size={16} />确认退役连接</button></details>
  </Drawer>;
}

function ToolPolicy({ connection, tool, onChanged }: { connection: Item; tool: Item; onChanged: () => void }) {
  const policy = connection.tool_policies?.[tool.name || tool.tool_name] || tool;
  const [enabled, setEnabled] = useState(!!policy.enabled), [stages, setStages] = useState<string[]>(policy.stages || []), [purpose, setPurpose] = useState(policy.purpose || ""), [readOnly, setReadOnly] = useState(!!connection.builtin || !!policy.read_only_confirmed), [error, setError] = useState<unknown>(), [busy, setBusy] = useState(false);
  return <section className="cap-policy"><h3>{text(tool.name || tool.tool_name)}</h3><p>{text(tool.description)}</p><Switch label={`允许${tool.name || tool.tool_name}`} value={enabled} disabled={tool.read_only_eligible === false} onChange={() => setEnabled(v => !v)} />{tool.read_only_eligible === false && <p role="status">写入或未声明只读的工具不可接入此阶段。</p>}{!connection.builtin && <label className="cap-checkbox"><input type="checkbox" checked={readOnly} onChange={e => setReadOnly(e.target.checked)} />已核对可信服务及只读凭据</label>}{[["preparation", "准备阶段"], ["evidence", "证据阶段"]].map(([value, label]) => <label className="cap-checkbox" key={value}><input type="checkbox" checked={stages.includes(value)} onChange={e => setStages(prev => e.target.checked ? [...prev, value] : prev.filter(stage => stage !== value))} />{label}</label>)}
    <label className="cap-field">使用目的<select aria-label="使用目的" value={purpose} onChange={e => setPurpose(e.target.value)}><option value="">选择用途</option><option value="evidence">证据</option><option value="duplicate_reference">历史查重</option><option value="style_reference">风格参考</option><option value="operations">运行诊断</option></select></label><details><summary>输入 schema 与版本差异</summary><Json value={{ schema: tool.input_schema || tool.schema, schema_hash: tool.schema_hash, diff: tool.schema_diff }} /></details><ErrorLine error={error} />
    <button className="quiet-button" disabled={busy || (enabled && (!stages.length || !purpose.trim() || !readOnly))} onClick={async () => { setBusy(true); setError(undefined); try { await api(`/api/mcp/connections/${encoded(connection.id!)}/tool-policy`, "POST", { expected_revision: connection.revision, tool_name: tool.tool_name || tool.name, schema_hash: tool.schema_hash, enabled, stages, purpose, read_only_confirmed: readOnly }); onChanged(); } catch (e) { setError(e); } finally { setBusy(false); } }}><Save size={16} />保存允许范围</button>
  </section>;
}
