import { useEffect, useRef, useState, type ReactNode } from "react";
import { CheckCircle2, CircleAlert, Copy, LoaderCircle, RefreshCw, X } from "lucide-react";
import { api } from "../api";

// Management payloads include adapter-specific fields alongside the shared contract.
export type Item = { id?: string; [key: string]: any };
export type Listing = Item & { rows: Item[]; next_cursor?: string | null };
export const tabs = [["overview", "概览"], ["tools", "工具"], ["mcp", "MCP"], ["skills", "SKILLS"], ["memory", "记忆与知识库"], ["calls", "调用与变更"]] as const;
export const encoded = (id: string) => encodeURIComponent(id);
export function route() {
  const [path, search = ""] = location.hash.slice(1).split("?");
  const [, rawTab, rawId] = path.split("/");
  const tab = tabs.some(([value]) => value === rawTab) ? rawTab : "overview";
  const params = new URLSearchParams(search);
  let id = "";
  try { id = rawId ? decodeURIComponent(rawId) : params.get(`${tab}.detail`) || ""; } catch { /* Invalid deep links remain at the list. */ }
  return { tab, id, params };
}
export function guarded(proceed: () => void) {
  if (window.dispatchEvent(new CustomEvent("capability-leave", { cancelable: true, detail: { proceed } }))) proceed();
}
export function navigate(tab: string, id = "", filters?: Record<string, string>, restore = false, saved = false) {
  const current = route(), params = current.params;
  params.set(`${current.tab}.detail`, current.id);
  if (restore) id = params.get(`${tab}.detail`) || "";
  params.set(`${tab}.detail`, id);
  for (const [key, value] of Object.entries(filters || {})) {
    if (value) params.set(`${tab}.${key}`, value); else params.delete(`${tab}.${key}`);
  }
  const hash = `#capabilities/${tab}${id ? `/${encoded(id)}` : ""}${params.size ? `?${params}` : ""}`;
  if (saved) location.hash = hash;
  else guarded(() => { location.hash = hash; });
}
export const filter = (key: string, fallback = "") => route().params.get(`${route().tab}.${key}`) ?? fallback;
export function query(values: Record<string, string | number | undefined>) {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => { if (value !== undefined && value !== "") params.set(key, String(value)); });
  return params.size ? `?${params}` : "";
}
export function safe(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(safe);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, val]) => [key,
    /secret|token|password|authorization|api.?key|environment|headers/i.test(key) ? (val ? "已配置" : "未配置") : safe(val)]));
  if (typeof value === "string") return value.replace(/(Bearer\s+)\S+/gi, "$1[已隐藏]")
    .replace(/((?:api[_-]?key|secret|password|access[_-]?token|authorization)[=:]\s*)[^\s,;]+/gi, "$1[已隐藏]")
    .replace(/([?&](?:token|key|api_key|secret)=)[^&\s]+/gi, "$1[已隐藏]")
    .replace(/(https?:\/\/)[^/\s@]+:[^/\s@]+@/gi, "$1[已隐藏]@");
  return value;
}
export function text(value: unknown, fallback = "未记录"): string {
  if (value === null || value === undefined || value === "") return fallback;
  const clean = safe(value);
  return typeof clean === "object" ? JSON.stringify(clean, null, 2) : String(clean);
}
export function when(value: unknown) {
  if (!value) return "未检查";
  const date = new Date(typeof value === "number" ? value * 1000 : String(value));
  return Number.isNaN(date.getTime()) ? text(value) : date.toLocaleString("zh-CN", { hour12: false });
}
export function ErrorLine({ error, retry }: { error: unknown; retry?: () => void }) {
  if (!error) return null;
  const e = error as Item;
  return <div className="cap-error" role="alert"><CircleAlert size={16} /><div>{text(e.message || error)}{e.code && <small>{text(e.code)}{e.revision !== undefined ? ` / 当前版本 ${e.revision}` : ""}</small>}{e.next_action && <small>{text(e.next_action)}</small>}</div>{retry && <IconButton label="重试" onClick={retry}><RefreshCw size={16} /></IconButton>}</div>;
}
export function IconButton({ label, children, ...props }: { label: string; children: ReactNode } & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button type="button" className="icon-button" title={label} aria-label={label} {...props}>{children}</button>;
}
export function Switch({ label, value, disabled, onChange }: { label: string; value: boolean; disabled?: boolean; onChange: () => void }) {
  return <button type="button" role="switch" aria-label={label} aria-checked={value} className={`cap-switch ${value ? "on" : ""}`} disabled={disabled} onClick={onChange}><span /></button>;
}
const statuses: Record<string, string> = { ready: "就绪", unknown: "待检测", stale: "检测已过期", blocked: "不可用", failed: "失败", offline: "离线", degraded: "部分可用", running: "进行中", checking: "检测中", succeeded: "成功", idle: "按需启动 / 空闲", not_recorded: "该版本未采集", denied: "已阻止", timed_out: "超时", uncertain: "结果待核对", disabled: "停用", pending: "待处理", skipped_empty: "空文本跳过" };
export function Status({ value }: { value?: string }) {
  const good = ["ready", "succeeded"].includes(value || "");
  const pending = !value || ["unknown", "idle", "pending", "not_recorded"].includes(value);
  const Icon = good ? CheckCircle2 : pending ? CircleAlert : ["running", "checking"].includes(value!) ? LoaderCircle : CircleAlert;
  return <span className={`cap-status ${good ? "good" : pending ? "pending" : "bad"}`}><Icon size={14} />{statuses[value || "unknown"] || value}</span>;
}
export function Empty({ children = "暂无记录" }: { children?: ReactNode }) { return <div className="cap-empty">{children}</div>; }
export function Fields({ values }: { values: [string, unknown][] }) {
  return <dl className="cap-fields">{values.map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{text(value)}</dd></div>)}</dl>;
}
export function Json({ value }: { value: unknown }) { return <pre className="cap-json">{text(value)}</pre>; }
export function CopyId({ id }: { id: string }) {
  const [error, setError] = useState<unknown>();
  return <span className="cap-copy"><code>{id}</code><IconButton label="复制 ID" onClick={() => navigator.clipboard.writeText(id).catch(setError)}><Copy size={15} /></IconButton><ErrorLine error={error} /></span>;
}
export function useRead<T>(path: string, refresh = 0) {
  const [data, setData] = useState<T | null>(null), [error, setError] = useState<unknown>(), [loading, setLoading] = useState(false);
  const [last, setLast] = useState<{ path:string; data:T; observed_at:number } | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let disposed = false;
    setError(undefined); setData(null);
    if (!path) return;
    setLoading(true);
    api<T>(path).then(result => { if (!disposed) {setData(result);if (!(result as Item)?.read_only) setLast({path,data:result,observed_at:Date.now()/1000});} }).catch(e => { if (!disposed) setError(e); }).finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [path, refresh, retry]);
  return { data, error, loading, lastKnown:!loading && (error || (data as Item)?.read_only) && last?.path===path ? last : null, reload: () => setRetry(value => value + 1) };
}
export function ReadState({ loading, error, reload, lastKnown }: { loading: boolean; error: unknown; reload: () => void; lastKnown?: {data:unknown;observed_at:number} | null }) {
  return <>{loading && <div className="cap-loading" role="status"><LoaderCircle size={16} className="spin" />正在读取</div>}<ErrorLine error={error} retry={reload} />{lastKnown && <details className="cap-band"><summary>上次读取的只读记录</summary><p>{when(lastKnown.observed_at)}；不代表当前状态，不能据此执行或修改配置。</p><Json value={lastKnown.data} /></details>}</>;
}
export function Pager({ next, cursor = "", onChange }: { next?: string | null; cursor?: string; onChange: (value: string) => void }) {
  return <div className="cap-pager"><button className="quiet-button" disabled={!cursor} onClick={() => onChange("")}>首页</button><button className="quiet-button" disabled={!next} onClick={() => onChange(next!)}>下一页</button></div>;
}
export function Modes({ value, onChange, disabled, modes = ["off", "auto", "manual"] }: { value: string; onChange: (value: string) => void; disabled?: boolean; modes?: string[] }) {
  return <div className="cap-segment" role="group" aria-label="技能模式">{modes.map(mode => <button type="button" key={mode} aria-pressed={value === mode} disabled={disabled} onClick={() => onChange(mode)}>{{ off: "关闭", auto: "自动", manual: "手动" }[mode] || mode}</button>)}</div>;
}
export function Drawer({ title, dirty = false, onClose, children }: { title: string; dirty?: boolean; onClose: () => void; children: ReactNode }) {
  const panel = useRef<HTMLElement>(null), opener = useRef<HTMLElement | null>(null);
  const [leave, setLeave] = useState<(() => void) | null>(null);
  useEffect(() => { if (leave) panel.current?.querySelector<HTMLButtonElement>(".cap-unsaved button")?.focus(); }, [leave]);
  const dirtyRef = useRef(dirty); dirtyRef.current = dirty;
  const closeRef = useRef(onClose); closeRef.current = onClose;
  useEffect(() => {
    opener.current = document.activeElement as HTMLElement;
    panel.current?.focus();
    const guard = (event: Event) => {
      if (!dirtyRef.current) return;
      event.preventDefault(); setLeave(() => (event as CustomEvent).detail.proceed);
    };
    const unload = (event: BeforeUnloadEvent) => { if (dirtyRef.current) event.preventDefault(); };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.isComposing || panel.current?.contains(event.target as Node)) return;
      event.preventDefault();
      if (dirtyRef.current) setLeave(() => closeRef.current); else closeRef.current();
    };
    window.addEventListener("capability-leave", guard); window.addEventListener("beforeunload", unload);
    window.addEventListener("keydown", escape);
    return () => { window.removeEventListener("capability-leave", guard); window.removeEventListener("beforeunload", unload); window.removeEventListener("keydown", escape); opener.current?.focus(); };
  }, []);
  const close = () => { if (dirty) setLeave(() => onClose); else onClose(); };
  return <div className="cap-drawer-layer"><button className="cap-scrim" tabIndex={-1} aria-label="关闭详情背景" onClick={close} /><aside ref={panel} className="cap-drawer" role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} onKeyDown={event => {
    if (event.key === "Escape" && !event.nativeEvent.isComposing) { event.preventDefault(); event.stopPropagation(); close(); }
    if (event.key === "Tab") {
      const controls = Array.from(panel.current!.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href],summary,[tabindex="0"]')).filter(el => el.getClientRects().length);
      const first = controls[0], last = controls.at(-1);
      if (!controls.length) { event.preventDefault(); return; }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === panel.current)) { event.preventDefault(); first?.focus(); }
    }
  }}><header><h2>{title}</h2><IconButton label="关闭详情" onClick={close}><X size={18} /></IconButton></header>
    {leave ? <section className="cap-unsaved"><h3>有未保存修改</h3><button className="primary-button" onClick={() => setLeave(null)}>保留修改</button><button className="quiet-button" onClick={() => { const proceed = leave; setLeave(null); dirtyRef.current = false; proceed(); }}>放弃修改</button></section> : children}
  </aside></div>;
}
export function useOperation(onComplete: () => void) {
  const [operation, setOperation] = useState<Item | null>(null), [error, setError] = useState<unknown>();
  const complete = useRef(onComplete); complete.current = onComplete;
  useEffect(() => {
    if (!operation?.operation_id || !["running", "checking", "pending", "queued"].includes(operation.status)) return;
    let disposed = false, timer: number;
    const poll = async () => {
      try {
        const result = await api<Item>(`/api/capabilities/checks/${encoded(operation.operation_id)}`);
        if (disposed) return;
        setOperation({ ...result, operation_id: operation.operation_id });
        if (!["running", "checking", "pending", "queued"].includes(result.status)) complete.current();
      } catch (e) { if (!disposed) setError(e); }
    };
    timer = window.setTimeout(poll, 1000);
    return () => { disposed = true; clearTimeout(timer); };
  }, [operation]);
  return { operation, error, start: async (path: string, body: unknown) => {
    setError(undefined);
    try { const result = await api<Item>(path, "POST", body); setOperation(result); if (!["running", "checking", "pending", "queued"].includes(result.status)) complete.current(); }
    catch (e) { setError(e); }
  }, retry: () => { setError(undefined); setOperation(previous => previous ? { ...previous } : previous); } };
}
export function Operation({ state }: { state: ReturnType<typeof useOperation> }) {
  return <><ErrorLine error={state.error} retry={state.retry} />{state.operation && <section className="cap-operation" aria-live="polite"><strong>{state.operation.status === "succeeded" ? "检测完成" : "操作状态"}</strong><Status value={state.operation.status} />{state.operation.stages?.map((stage: Item, i: number) => <div key={stage.name || i}>{text(stage.name || stage.stage)} <Status value={stage.status} />{stage.elapsed_ms !== undefined && <span>{stage.elapsed_ms} ms</span>}</div>)}<ErrorLine error={state.operation.error} /><details><summary>操作结果</summary><Json value={state.operation.results || state.operation.result} /></details></section>}</>;
}
export function SourceLink({ value }: { value?: string }) {
  const clean = text(value, "");
  return /^https?:\/\//i.test(clean) || /^#/.test(clean) ? <a href={clean} rel="noopener noreferrer" target={clean.startsWith("#") ? undefined : "_blank"}>查看来源</a> : <span>{clean || "未记录来源"}</span>;
}
