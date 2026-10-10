import { useEffect, useState } from "react";
import { Copy, FileInput, Save, Trash2 } from "lucide-react";
import { api } from "../api";
import { CopyId, Drawer, Empty, ErrorLine, Fields, Json, Modes, ReadState, SourceLink, Switch, encoded, filter, navigate, text, useRead, when, type Item, type Listing } from "./shared";

export function Skills({ id, refresh }: { id: string; refresh: number }) {
  const [revision, setRevision] = useState(0), [busy, setBusy] = useState(false), [error, setError] = useState<unknown>();
  const listing = useRead<Listing>("/api/skills", refresh + revision);
  const changed = () => setRevision(v => v + 1);
  const q = filter("query");
  async function patch(row: Item, body: Item) {
    setBusy(true); setError(undefined);
    try { await api(`/api/skills/${encoded(row.id!)}`, "PATCH", { expected_revision: row.revision, ...body }); changed(); }
    catch (e) { setError(e); } finally { setBusy(false); }
  }
  return <><div className="cap-toolbar"><input aria-label="搜索技能" placeholder="搜索技能" value={q} onChange={e => navigate("skills", "", { query: e.target.value })} /><button className="quiet-button" onClick={() => navigate("skills", "import")}><FileInput size={16} />导入技能</button><span>默认模式</span><Modes value={listing.data?.default_mode || "off"} disabled={busy || !listing.data} onChange={default_mode => patch({ id: "defaults", revision: listing.data?.policy_revision ?? 0 }, { default_mode })} /></div>
    <ReadState {...listing} /><ErrorLine error={error} retry={listing.reload} /><table className="cap-table"><thead><tr><th>技能名称 / 描述</th><th className="cap-secondary">来源 / 版本</th><th className="cap-secondary">适用栏目</th><th>启用</th><th className="cap-secondary">最近加载</th></tr></thead><tbody>{listing.data?.rows.filter(row => `${row.name} ${row.description}`.toLowerCase().includes(q.toLowerCase())).map(row => <tr key={row.id}><td><button className="cap-row-link" onClick={() => navigate("skills", row.id!)}>{text(row.name)}</button><small>{text(row.description, "")}</small></td><td className="cap-secondary">{text(row.source)}<small>{text(row.version || row.hash)}</small></td><td className="cap-secondary">{text(row.columns || row.groups)}</td><td><Switch label={`启用${row.name}`} value={!!row.enabled} disabled={busy} onChange={() => patch(row, { enabled: !row.enabled })} /></td><td className="cap-secondary">{when(row.last_loaded_at)}</td></tr>)}</tbody></table>
    {listing.data && !listing.data.rows.length && <Empty><span>尚未导入技能</span><Fields values={[["当前目录", listing.data.directories]]} /><button className="text-button" onClick={() => navigate("skills", "import")}>导入技能</button></Empty>}
    {id === "import" ? <SkillImport onChanged={changed} /> : id && <SkillDetail key={id} id={id} refresh={refresh + revision} onChanged={changed} />}
  </>;
}

function SkillImport({ onChanged }: { onChanged: () => void }) {
  const [source, setSource] = useState(""), [preview, setPreview] = useState<Item | null>(null), [allow, setAllow] = useState(false), [error, setError] = useState<unknown>(), [busy, setBusy] = useState(false);
  return <Drawer title="导入技能" dirty={!!source} onClose={() => navigate("skills")}><label className="cap-field">文件夹或 ZIP 路径<input value={source} onChange={e => { setSource(e.target.value); setPreview(null); setAllow(false); }} /></label><ErrorLine error={error} /><button className="quiet-button" disabled={busy || !source.trim()} onClick={async () => { setBusy(true); setError(undefined); try { setPreview(await api<Item>("/api/skills/import-preview", "POST", { source_path: source })); } catch (e) { setError(e); } finally { setBusy(false); } }}><FileInput size={16} />预览技能</button>
    {preview && <><Fields values={[["名称", preview.name], ["有效", preview.valid ? "通过" : "未通过"], ["文件数量", preview.files?.length], ["总大小（字节）", preview.total_bytes], ["正文 hash", preview.hash], ["名称冲突", preview.collision]]} /><h3>校验问题与附带文件</h3><Json value={preview.issues} /><Json value={preview.files} />{preview.collision && <label className="cap-checkbox"><input type="checkbox" checked={allow} onChange={e => setAllow(e.target.checked)} />允许同名新版本，不覆盖旧版本</label>}
      <button className="primary-button" disabled={busy || !preview.valid || !preview.hash || (!!preview.collision && !allow)} onClick={async () => { setBusy(true); setError(undefined); try { await api("/api/skills/import-commit", "POST", { preview_id: preview.preview_id, hash: preview.hash, allow_new_version: allow }); setSource(""); onChanged(); navigate("skills", "", undefined, false, true); } catch (e) { setError(e); setPreview(null); } finally { setBusy(false); } }}><Save size={16} />确认导入</button></>}
  </Drawer>;
}

// A deliberately restricted Markdown view: React escapes HTML, no remote images or scripts.
function Markdown({ body }: { body: string }) {
  const lines = body.split(/\r?\n/);
  let code = false;
  return <div className="cap-markdown">{lines.map((line, index) => {
    if (line.startsWith("```")) { code = !code; return null; }
    if (code) return <pre key={index}>{line || " "}</pre>;
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) return heading[1].length === 1 ? <h3 key={index}>{heading[2]}</h3> : <h4 key={index}>{heading[2]}</h4>;
    if (/^[-*]\s+/.test(line)) return <p className="cap-markdown-list" key={index}>{line.slice(2)}</p>;
    if (!line) return <br key={index} />;
    return <p key={index}>{line.split(/(\[[^\]]+\]\([^\s)]+\)|`[^`]+`|\*\*[^*]+\*\*)/).map((part, i) => {
      const link = part.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      if (link && /^https?:\/\//.test(link[2])) return <a key={i} href={link[2]} target="_blank" rel="noopener noreferrer">{link[1]}</a>;
      if (part.startsWith("`") && part.endsWith("`")) return <code key={i}>{part.slice(1, -1)}</code>;
      if (part.startsWith("**") && part.endsWith("**")) return <strong key={i}>{part.slice(2, -2)}</strong>;
      return part;
    })}</p>;
  })}</div>;
}

function SkillDetail({ id, refresh, onChanged }: { id: string; refresh: number; onChanged: () => void }) {
  const detail = useRead<Item>(`/api/skills/${encoded(id)}`, refresh);
  const tab = filter("detailTab", "body"), resourcePath = filter("resource");
  const resource = useRead<Item>(tab === "files" && resourcePath ? `/api/skills/${encoded(id)}/resources${"?path=" + encoded(resourcePath)}` : "");
  const [body, setBody] = useState(""), [editing, setEditing] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState<unknown>();
  const [copying, setCopying] = useState(false), [copyName, setCopyName] = useState("");
  useEffect(() => { if (!editing) setBody(detail.data?.body || ""); }, [detail.data, editing]);
  const dirty = (editing && body !== detail.data?.body) || copying;
  async function patch(values: Item) {
    setBusy(true); setError(undefined);
    try { await api(`/api/skills/${encoded(id)}`, "PATCH", { expected_revision: detail.data?.revision, ...values }); setEditing(false); onChanged(); }
    catch (e) { setError(e); } finally { setBusy(false); }
  }
  async function copySkill() {
    setBusy(true); setError(undefined);
    try {
      const row = await api<Item>(`/api/skills/${encoded(id)}/copy`, "POST", { expected_revision: detail.data?.revision, name: copyName });
      setCopying(false); onChanged(); navigate("skills", row.id!, undefined, false, true);
    } catch (e) { setError(e); } finally { setBusy(false); }
  }
  return <Drawer title={detail.data?.name || "技能详情"} dirty={dirty} onClose={() => navigate("skills")}><ReadState {...detail} /><ErrorLine error={error} retry={detail.reload} />{detail.data && <><CopyId id={id} /><p>{text(detail.data.description)}</p><Fields values={[["来源", detail.data.source], ["版本", detail.data.version || detail.data.hash]]} /><div className="cap-tabs cap-subtabs" role="tablist" aria-label="技能详情">{[["body", "说明"], ["files", "引用文件"], ["versions", "版本"], ["calls", "使用记录"]].map(([value, label]) => <button role="tab" key={value} aria-selected={tab === value} onClick={() => navigate("skills", id, { detailTab: value })}>{label}</button>)}</div>
    {tab === "body" && <>{editing ? <><label className="cap-field">技能正文<textarea aria-label="技能正文" rows={18} value={body} onChange={e => setBody(e.target.value)} /></label><button className="primary-button" disabled={busy || !dirty} onClick={() => patch({ body })}><Save size={16} />保存新版本</button></> : <><Markdown body={detail.data.body || ""} />{detail.data.source !== "builtin" ? <button className="quiet-button" onClick={() => setEditing(true)}><Save size={16} />编辑正文</button> : <button className="quiet-button" disabled={busy} onClick={() => { setCopyName(`${detail.data!.name.slice(0, 75)}-copy`); setCopying(true); }}><Copy size={16} />创建个人副本</button>}</>}
      {copying && <form onSubmit={event => { event.preventDefault(); void copySkill(); }}><label className="cap-field">副本名称<input value={copyName} maxLength={80} pattern="[a-z0-9][a-z0-9._-]{0,79}" required onChange={event => setCopyName(event.target.value)} /></label><p>副本保留来源版本与附件，默认停用；原技能不变。</p><button type="submit" className="primary-button" disabled={busy || !copyName.trim()}><Copy size={16} />确认创建副本</button><button type="button" className="quiet-button" disabled={busy} onClick={() => setCopying(false)}>取消</button></form>}</>}
    {tab === "files" && <>{detail.data.files?.map((file: string | Item) => { const path = typeof file === "string" ? file : file.path || file.name; return <button className="cap-row-link" key={path} onClick={() => navigate("skills", id, { resource: path })}>{path}</button>; })}<ReadState {...resource} />{resource.data && <pre className="cap-json">{text(resource.data.content)}</pre>}</>}
    {tab === "versions" && <>{detail.data.versions?.map((version: Item | string, index: number) => <section className="cap-band" key={index}><Json value={version} /><button className="quiet-button" disabled={busy || (typeof version !== "string" && !version.revision)} onClick={() => patch(typeof version === "string" ? { version } : { version_revision: version.revision })}>采用此版本</button></section>)}</>}
    {tab === "calls" && (detail.data.recent_calls?.length ? detail.data.recent_calls.map((call: Item) => <section className="cap-band" key={call.id}><button className="cap-row-link" onClick={() => navigate("calls", call.id!)}>{when(call.started_at)} / {text(call.status)}</button><Fields values={[["正文加载", call.loaded_bodies], ["附件读取", call.loaded_resources], ["冻结版本", call.version]]} /><SourceLink value={call.source_ref} /></section>) : <Empty>未记录使用</Empty>)}
    <details><summary>退役技能</summary><p>新任务不再选择此技能，已冻结版本与产物保留。</p><button className="quiet-button cap-danger" disabled={busy} onClick={async () => { setBusy(true); setError(undefined); try { await api(`/api/skills/${encoded(id)}?expected_revision=${detail.data?.revision}`, "DELETE"); onChanged(); navigate("skills"); } catch (e) { setError(e); } finally { setBusy(false); } }}><Trash2 size={16} />确认退役技能</button></details>
  </>}</Drawer>;
}
