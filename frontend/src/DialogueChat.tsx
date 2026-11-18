import { useEffect, useMemo, useRef, useState } from "react";
import { Activity, ArrowDown, Check, CircleAlert, FileCheck2, FileText, LoaderCircle, MessageSquare, Pause, Play, Puzzle, RefreshCw, Send, Settings2, ShieldCheck, Square, Wrench, X } from "lucide-react";
import { api, type Conversation } from "./api";
import { DialogueGoal, type Goal, type GoalCommand } from "./DialogueGoal";
import "./dialogue-chat.css";

export type AssistantState = "idle" | "starting" | "running" | "stopping" | "paused" | "cancelled" | "waiting_approval" | "waiting_input" | "error";
export type AssistantDecision = "approve_once" | "approve_session" | "deny" | "answer" | "configured";
export interface AssistantRequest {
  id: string;
  kind: string;
  tool: string;
  arguments: Record<string, unknown>;
  reason: string;
  allowed_decisions: AssistantDecision[];
  version: number;
}
export interface AssistantSnapshot {
  engine: "langchain";
  state: AssistantState;
  revision: number;
  execution_id: string | null;
  round_started_at?: number | null;
  goal?: Goal | null;
  goal_version?: number;
  events: { id: string; kind: string; payload: Record<string, unknown>; created_at: string | number }[];
  requests: AssistantRequest[];
  actions: { id: string; tool: string; status: string; result: unknown; execution_id?: string; created_at?: number }[];
  messages: { id: string; role: string; content: string; created_at: string | number }[];
}
export interface DialogueChatProps {
  conversation: Conversation | null;
  onCreated: (conversation: Conversation) => void;
  onChanged: (conversationId: string) => void;
  onCapabilities: () => void;
  onModels: () => void;
}

const stateLabels: Record<AssistantState, string> = {
  idle: "就绪", starting: "正在启动", running: "执行中", stopping: "正在停止", paused: "已暂停", cancelled: "本次执行已取消",
  waiting_approval: "等待授权", waiting_input: "等待补充", error: "执行受阻",
};
const actionLabels: Record<string, string> = {
  pending: "待执行", queued: "排队中", running: "执行中", completed: "已完成", success: "已完成",
  succeeded: "已完成", failed: "失败", error: "错误", uncertain: "结果待核对", cancelled: "已取消", denied: "已拒绝 · 0 次执行",
  missing_capability: "缺少配置或能力", starting: "正在启动", started: "已开始",
};
const toolLabels: Record<string, string> = {
  capabilities: "查看可用能力", list_capabilities: "查看可用能力", sync_account: "同步账号数据",
  news_search: "查找新闻材料", generate_news: "生成单篇稿件", generate_digest: "生成集合稿",
  list_materials: "读取保留材料", list_drafts: "查看已有草稿", controller: "主控模型",
  create_image: "生成稿件配图", inspect_draft: "查看稿件", review_draft: "审查稿件", save_draft: "保存平台草稿",
  knowledge_search: "检索知识库", list_skills: "查看已登记技能", read_skill: "读取技能", mcp_call: "调用已登记工具",
};
const eventLabels: Record<string, string> = {
  tool_start: "工具开始", tool_started: "工具开始", tool_end: "工具结束", tool_completed: "工具结束",
  tool_error: "工具错误", error: "错误", waiting_approval: "等待授权", waiting_input: "等待补充",
  paused: "已暂停", cancelled: "执行已取消", starting: "正在启动", stopping: "正在停止", message: "消息", status: "状态更新",
  "tool/started": "工具开始", "tool/completed": "工具完成", "tool/progress": "工具进度",
  "model/status": "模型状态", "agent/waiting": "等待你的确认", "agent/idle": "本轮结束",
  "host/stopping": "正在停止", "host/paused": "对话已暂停", "host/cancelled": "执行已取消",
  "host/recovered": "进度已恢复", "host/error": "执行受阻", "worker/progress": "执行进度",
  "context/compacting": "正在压缩对话", "context/compacted": "对话压缩完成",
  "assistant/message": "智能体回复", "user/message": "收到消息", "user/queued": "补充要求已保存",
  "user/control": "执行控制", "user/decision": "已收到你的决定",
  "goal/set": "目标已设定", "goal/edit": "目标已更新", "goal/pause": "目标已暂停",
  "goal/resume": "目标已恢复", "goal/clear": "目标已清除", "goal/complete": "目标已验收",
  "goal/continued": "继续推进目标", "goal/completed": "目标已完成", "goal/blocked": "目标待处理",
};
const privateField = /reasoning|thought|chain_of_thought|analysis|thinking/i;

function publicValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(publicValue);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value)
    .filter(([key]) => !privateField.test(key)).map(([key, item]) => [key, publicValue(item)]));
  return value;
}
function display(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(publicValue(value), null, 2) ?? "";
}
function dateLabel(value: string | number) {
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", { hour12: false });
}
function errorText(cause: unknown) {
  return cause instanceof Error ? cause.message : String(cause);
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function readableText(value: unknown, depth = 0): string {
  if (depth > 4) return "";
  if (typeof value === "string") {
    const text = value.trim();
    if (text.startsWith("{") || text.startsWith("[")) {
      try { return readableText(JSON.parse(text), depth + 1); } catch { return ""; }
    }
    return (actionLabels[text] || text).slice(0, 280) + (text.length > 280 ? "..." : "");
  }
  const data = record(value);
  return readableText(data.text ?? data.summary ?? data.message ?? data.reason ?? data.detail ?? data.error, depth + 1);
}
function resultPosts(data: Record<string, unknown>) {
  return [...(Array.isArray(data.posts) ? data.posts : []), data.post, data.receipt]
    .map(record).filter(post => Object.keys(post).length > 0);
}
function resultPostIds(data: Record<string, unknown>) {
  return [...new Set([...(Array.isArray(data.post_ids) ? data.post_ids : []), data.post_id,
    ...resultPosts(data).map(post => post.id)].filter((id): id is string => typeof id === "string" && Boolean(id)))];
}
type AssistantAction = AssistantSnapshot["actions"][number];

function evidence(actions: AssistantAction[]) {
  const generated = new Set<string>(), saved = new Set<string>();
  const titles = new Map<string, string>(), materials = new Map<string, string>();
  for (const action of actions) {
    const data = record(action.result);
    const ids = resultPostIds(data);
    for (const post of resultPosts(data)) {
      const id = typeof post.id === "string" ? post.id : ids[0];
      if (id && typeof post.title === "string" && !titles.has(id)) titles.set(id, post.title);
    }
    for (const item of Array.isArray(data.materials) ? data.materials : []) {
      const { material_id, title } = record(item);
      if (typeof material_id === "string" && typeof title === "string") materials.set(material_id, title);
    }
    if (data.ok === false || data.status === "uncertain" || !["succeeded", "success", "completed"].includes(action.status)) continue;
    if (["generate_news", "generate_digest"].includes(action.tool)) ids.forEach(id => generated.add(id));
    if (action.tool === "save_draft" && data.platform_receipt_confirmed === true) ids.forEach(id => saved.add(id));
  }
  return { generated, saved, titles, materials };
}

function RawDetails({ label, value, meta }: { label: string; value: unknown; meta?: string }) {
  const [open, setOpen] = useState(false);
  return <details className="dialogue-result-details" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{label}</summary>{open && <>{meta && <small>{meta}</small>}<pre>{display(value)}</pre></>}
  </details>;
}

function ToolResult({ action, titles }: { action: AssistantAction; titles: Map<string, string> }) {
  const data = record(action.result);
  const ids = resultPostIds(data);
  const posts = resultPosts(data);
  const names = [...new Set([...posts.map(post => post.title), data.title, ...ids.map(id => titles.get(id))]
    .filter((title): title is string => typeof title === "string" && Boolean(title.trim())))];
  const count = ids.length || posts.length;
  const errors = [...new Set([data.error, ...(Array.isArray(data.errors) ? data.errors : [])].map(value => readableText(value)).filter(Boolean))];
  const failed = data.ok === false || ["failed", "error", "missing_capability"].includes(action.status);
  const succeeded = ["succeeded", "success", "completed"].includes(action.status) && !failed;
  const unverified = action.status === "uncertain" || data.status === "uncertain"
    || action.tool === "save_draft" && succeeded && data.platform_receipt_confirmed !== true;
  let summary = readableText(data.summary ?? data.message ?? (typeof action.result === "string" ? action.result : undefined))
    || actionLabels[action.status] || (count ? `已返回 ${count} 篇稿件` : "工具结果已返回");
  if (action.status === "uncertain" || data.status === "uncertain") summary = "结果尚未核对，不计入完成数；已有产物保留。";
  else if (action.status === "denied") summary = "本次未执行 · 完成 0 次";
  else if (data.passed === false) summary = `审查未通过 · ${errors.length} 项问题`;
  else if (failed) summary = "本次操作未完成";
  else if (succeeded) {
    if (action.tool === "generate_news" || action.tool === "generate_digest") summary = count
      ? `已生成 ${count} 篇稿件${data.gap ? " · 仍有缺口" : ""}` : "未生成稿件 · 完成 0 篇";
    else if (action.tool === "save_draft") summary = data.platform_receipt_confirmed === true && count
      ? `平台草稿回执已核对 · 完成 ${count} 篇` : "保存结果待核对，暂不计入完成数。";
    else if (action.tool === "review_draft") summary = data.passed === true ? "审查通过" : "审查结果待确认";
    else if (action.tool === "create_image") summary = Array.isArray(data.assets)
      ? `配图完成 · ${data.assets.length} 张` : "配图结果待确认";
    else if (Array.isArray(data.materials)) summary = `${action.tool === "list_materials" ? "读取" : "找到"} ${data.materials.length} 条候选材料${data.query_gap ? " · 未命中检索主题" : data.gap ? " · 仍有缺口" : ""}`;
    else if (action.tool === "list_drafts" && Array.isArray(data.drafts)) summary = `本对话已有 ${data.drafts.length} 篇本地稿件`;
    else if (Array.isArray(data.tools)) summary = `已读取 ${data.tools.length} 项能力`;
    else if (action.tool === "sync_account" && typeof data.fetched === "number") summary = `已同步 ${data.fetched} 条已发布记录`;
    else if (count) summary = `已读取 ${count} 篇稿件`;
    else summary = readableText(data.summary ?? data.message ?? action.result) || "操作已完成";
  }
  return <article data-action-id={action.id}>
    <header><strong>{toolLabels[action.tool] || "其他工具"}</strong>
      <span>{unverified ? "结果待核对" : data.passed === false ? "审查未通过" : actionLabels[action.status] || "状态待确认"}</span></header>
    <p className="dialogue-result-summary">{summary}</p>
    {!!names.length && <ul className="dialogue-result-titles">{names.slice(0, 5).map(name => <li key={name}>{name}</li>)}
      {names.length > 5 && <li>另有 {names.length - 5} 篇稿件</li>}</ul>}
    {!!errors.length && <ul className="dialogue-result-errors">{errors.slice(0, 3).map(reason => <li key={reason}>{reason}</li>)}
      {errors.length > 3 && <li>另有 {errors.length - 3} 项问题</li>}</ul>}
    {failed && !errors.length && <p className="dialogue-result-errors">服务端未提供具体错误原因，未自动重试。</p>}
    {action.result != null && <RawDetails label="原始结果" value={action.result} meta={`${action.tool} · ${action.status}`} />}
  </article>;
}

function ToolMessageResult({ message, titles }: { message: AssistantSnapshot["messages"][number]; titles: Map<string, string> }) {
  let result: unknown = message.content;
  try { result = JSON.parse(message.content); } catch { /* Plain tool replies remain readable text. */ }
  const data = record(result);
  return <ToolResult titles={titles} action={{ id: message.id, result,
    tool: typeof data.tool === "string" ? data.tool : "",
    status: typeof data.status === "string" ? data.status : data.ok === false ? "failed" : "returned" }} />;
}

function EventContent({ event }: { event: AssistantSnapshot["events"][number] }) {
  const data = record(event.payload);
  const tool = typeof data.tool === "string" ? toolLabels[data.tool] || "工具" : "工具";
  const status = typeof data.status === "string" ? actionLabels[data.status] : "";
  let title = eventLabels[event.kind] || "执行状态更新";
  let text = readableText(data);
  if (event.kind === "model/status") {
    title = data.status === "started" ? "模型调用开始" : data.status === "completed" ? "模型响应完成" : title;
    text = data.status === "completed" && typeof data.tool_calls === "number" ? `本轮请求调用 ${data.tool_calls} 项工具。` : "";
  } else if (event.kind === "tool/started") text = `${tool}开始执行。`;
  else if (event.kind === "tool/completed") text = `${tool}：${status || "结果已返回"}。`;
  else if (event.kind === "tool/progress" && !text) text = `${tool}正在处理。`;
  return <><strong>{title}</strong>{text && <p>{text}</p>}
    <RawDetails label="事件详情" value={event} /></>;
}

type DecisionBody = { decision: AssistantDecision; version: number; answer?: string; max_calls?: number };

function RequestCard({ request, subject, onDecision, onCapabilities, onModels }: {
  request: AssistantRequest;
  subject?: string;
  onDecision: (request: AssistantRequest, body: DecisionBody) => Promise<void>;
  onCapabilities: () => void;
  onModels: () => void;
}) {
  const [answer, setAnswer] = useState("");
  const [maxCalls, setMaxCalls] = useState("1");
  const [pending, setPending] = useState(false);
  const pendingRef = useRef(false);
  const allowed = new Set(request.allowed_decisions);
  const count = Number(maxCalls);
  const validCount = Number.isInteger(count) && count >= 1 && count <= 20;
  const args = request.arguments;
  const columnNames: Record<string, string> = {daily_news: "每日新闻", daily_wow: "每日我去", ai_digest: "每日 AI 讯息", ai_wool: "AI 福利", global_map: "全球事件关注图"};
  const kindNames: Record<string, string> = {approval: "等待授权", question: "待回答", input: "待回答", error: "执行受阻", configuration: "待检查配置", capability: "需要能力"};
  const isApproval = allowed.has("approve_once") || allowed.has("approve_session");
  const incomplete = !isApproval && request.tool === "controller" && /\bOUTPUT_INCOMPLETE\b/.test(request.reason);
  const unknownTool = !isApproval && /\bCAPABILITY_UNKNOWN\b/.test(request.reason);
  const resource = unknownTool && (typeof args.resource_id === "string" ? args.resource_id :
    request.reason.match(/['"]resource_id['"]\s*:\s*['"]([^'"]+)['"]/)?.[1]);
  const help = incomplete ? "主控模型没有返回完整答复，这次响应未计为成功。可补充一个更小的步骤后提交，或检查主控模型设置后继续。"
    : unknownTool ? "智能体请求了尚未登记的能力。请到能力中心核对，或在这里告诉智能体改用哪项已有工具。" : "";
  const effects: Record<string, string> = {
    generate_news: "调用写稿模型并保存在本地，不上传。", generate_digest: "生成本地集合稿，不上传。",
    create_image: "调用生图模型，更新这篇稿件的配图。", save_draft: "写入专用账号的草稿箱，不公开发布。",
    sync_account: "读取专用账号的已发布数据，更新本地分析。"
  };
  async function decide(decision: AssistantDecision) {
    if (pendingRef.current || !allowed.has(decision) || (decision === "answer" && !answer.trim())
      || (decision === "approve_session" && !validCount)) return;
    pendingRef.current = true; setPending(true);
    try {
      await onDecision(request, { decision, version: request.version,
        ...(decision === "answer" ? { answer: answer.trim() } : {}),
        ...(decision === "approve_session" ? { max_calls: count } : {}),
      });
    } finally { pendingRef.current = false; setPending(false); }
  }
  return <section className="dialogue-request" tabIndex={-1} aria-label={`申请：${request.tool || request.kind}`}>
    <header><ShieldCheck size={18} /><h3>{toolLabels[request.tool] || request.tool || "需要补充信息"}</h3><span>{kindNames[request.kind] || "待处理"}</span></header>
    {isApproval && <div className="dialogue-subject">
      {typeof args.column === "string" && <small>{columnNames[args.column] || args.column}</small>}
      <strong>{subject || (typeof args.material_id === "string" ? `材料 ${args.material_id}` : typeof args.post_id === "string" ? `稿件 ${args.post_id}` : "")}</strong>
    </div>}
    <p>{help || (isApproval && effects[request.tool] ? effects[request.tool] : request.reason || "等待你确认下一步。")}</p>
    {resource && <p>请求的能力：<code>{resource}</code></p>}
    {help && <RawDetails label="技术原因" value={request.reason} />}
    {isApproval && effects[request.tool] && request.reason && <details><summary>申请原因</summary><p>{request.reason}</p></details>}
    <details><summary>参数与操作范围</summary><pre>{display(request.arguments)}</pre></details>
    {allowed.has("answer") && <label className="dialogue-answer">补充信息<textarea rows={3} value={answer}
      onChange={event => setAnswer(event.target.value)} disabled={pending} /></label>}
    {allowed.has("approve_session") && <label className="dialogue-limit">本会话最多调用
      <input aria-label="本会话最多调用次数" type="number" min={1} max={20} step={1} value={maxCalls}
        onChange={event => setMaxCalls(event.target.value)} disabled={pending} />次（1–20）</label>}
    <div className="dialogue-request-actions">
      {allowed.has("approve_once") && <button className="primary-button" disabled={pending} onClick={() => void decide("approve_once")}><Check size={16} />允许本次</button>}
      {allowed.has("approve_session") && <button className="quiet-button" disabled={pending || !validCount} onClick={() => void decide("approve_session")}><ShieldCheck size={16} />授权本会话 {validCount ? count : ""} 次</button>}
      {allowed.has("deny") && <button className="quiet-button dialogue-danger" disabled={pending} onClick={() => void decide("deny")}><X size={16} />拒绝（0 次执行）</button>}
      {allowed.has("answer") && <button className="primary-button" disabled={pending || !answer.trim()} onClick={() => void decide("answer")}><Send size={16} />提交回答</button>}
      {allowed.has("configured") && <button className="quiet-button" disabled={pending} onClick={() => void decide("configured")}><RefreshCw size={16} />{incomplete ? "已检查模型，继续执行" : "已配置，重新检查"}</button>}
      {pending && <LoaderCircle size={16} className="spin" aria-label="正在提交决定" />}
    </div>
    {!isApproval && <div className="dialogue-links"><button className="text-button" onClick={onCapabilities}><Puzzle size={15} />能力中心</button>
      <button className="text-button" onClick={onModels}><Settings2 size={15} />连接与模型</button></div>}
  </section>;
}

export function DialogueChat({ conversation, onCreated, onChanged, onCapabilities, onModels }: DialogueChatProps) {
  const cid = conversation?.id ?? "";
  const [snapshot, setSnapshot] = useState<AssistantSnapshot | null>(null);
  const [prompt, setPrompt] = useState("");
  const [sending, setSending] = useState(false);
  const [controls, setControls] = useState({ pause: false, cancel: false });
  const [connectionError, setConnectionError] = useState("");
  const [operationError, setOperationError] = useState("");
  const [notice, setNotice] = useState("");
  const [goalPending, setGoalPending] = useState(false);
  const [goalError, setGoalError] = useState("");
  const [onlyRound, setOnlyRound] = useState(false);
  const [view, setView] = useState<"chat" | "events" | "actions">("chat");
  const scope = useRef({ cid, generation: 0 });
  const mounted = useRef(true);
  const current = useRef<AssistantSnapshot | null>(null);
  const callbacks = useRef({ onCreated, onChanged });
  callbacks.current = { onCreated, onChanged };
  const messageLock = useRef(false);
  const goalLock = useRef(false);
  const controlLocks = useRef({ pause: false, cancel: false });
  const messageRequest = useRef<{ cid: string; content: string; id: string; key: string } | null>(null);
  const followTail = useRef(true);
  const feed = useRef<HTMLDivElement>(null);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; scope.current.generation++; }; }, []);

  function isCurrent(id: string, generation: number) {
    return mounted.current && scope.current.cid === id && scope.current.generation === generation;
  }
  function receive(id: string, value: AssistantSnapshot) {
    if (scope.current.cid !== id || !mounted.current) return;
    if (value.engine !== "langchain" || !stateLabels[value.state] || !Number.isFinite(value.revision)
      || !Array.isArray(value.messages) || !Array.isArray(value.events) || !Array.isArray(value.requests) || !Array.isArray(value.actions)) {
      throw new Error("对话服务返回了无效状态，未切换到旧计划执行器。");
    }
    if (current.current && value.revision < current.current.revision) return;
    const changed = !current.current || value.revision > current.current.revision;
    // Retain public events if the server returns only its latest event window.
    const events = new Map(current.current?.events.map(event => [event.id, event]) ?? []);
    for (const event of value.events) events.set(event.id, event);
    const next = { ...value, events: [...events.values()] };
    current.current = next; setSnapshot(next); setConnectionError("");
    if (changed) callbacks.current.onChanged(id);
  }

  useEffect(() => {
    if (scope.current.cid !== cid) {
      scope.current = { cid, generation: scope.current.generation + 1 };
      current.current = null; setSnapshot(null); setPrompt(""); setOperationError(""); setConnectionError(""); setNotice("");
      messageRequest.current = null; messageLock.current = false; controlLocks.current = { pause: false, cancel: false };
      goalLock.current = false; setGoalPending(false); setGoalError(""); setOnlyRound(false);
      setSending(false); setControls({ pause: false, cancel: false }); setView("chat"); followTail.current = true;
    }
    if (!cid) return;
    const generation = scope.current.generation;
    let disposed = false;
    let timer: number;
    async function poll() {
      try {
        const value = await api<AssistantSnapshot>(`/api/conversations/${encodeURIComponent(cid)}/assistant`);
        if (!disposed && isCurrent(cid, generation)) receive(cid, value);
      } catch (cause) {
        if (!disposed && isCurrent(cid, generation)) setConnectionError(errorText(cause));
      } finally {
        if (!disposed) timer = window.setTimeout(poll, 1000);
      }
    }
    void poll();
    return () => { disposed = true; window.clearTimeout(timer); };
  }, [cid]);

  useEffect(() => {
    const area = feed.current;
    if (view !== "chat" || !area || !followTail.current || area.contains(document.activeElement)) return;
    const pending = area.querySelector<HTMLElement>(".dialogue-request");
    area.scrollTop = pending ? area.scrollTop + pending.getBoundingClientRect().top - area.getBoundingClientRect().top : area.scrollHeight;
  }, [cid, view, snapshot?.messages.at(-1)?.id, snapshot?.requests[0]?.id, notice, operationError]);

  function showRequests() {
    setView("chat");
    requestAnimationFrame(() => {
      const area = feed.current, first = area?.querySelector<HTMLElement>(".dialogue-request");
      if (!area || !first) return;
      first.focus({ preventScroll: true });
      area.scrollTop += first.getBoundingClientRect().top - area.getBoundingClientRect().top;
    });
  }

  async function refresh() {
    const { cid: id, generation } = scope.current;
    if (!id) return;
    try {
      const value = await api<AssistantSnapshot>(`/api/conversations/${encodeURIComponent(id)}/assistant`);
      if (isCurrent(id, generation)) receive(id, value);
    } catch (cause) { if (isCurrent(id, generation)) setConnectionError(errorText(cause)); }
  }

  async function sendMessage(text = prompt) {
    const content = text.trim();
    if (!content || messageLock.current || goalLock.current) return;
    messageLock.current = true; setSending(true); setOperationError(""); setNotice("");
    let { cid: id, generation } = scope.current;
    try {
      if (!id) {
        const created = await api<Conversation>("/api/conversations", "POST", { title: content.slice(0, 50) });
        if (!isCurrent(id, generation)) return;
        id = created.id; scope.current.cid = id;
        callbacks.current.onCreated(created);
      }
      if (!isCurrent(id, generation)) return;
      if (messageRequest.current?.cid !== id || messageRequest.current.content !== content) {
        messageRequest.current = { cid: id, content, id: crypto.randomUUID(), key: crypto.randomUUID() };
      }
      const request = messageRequest.current;
      const value = await api<AssistantSnapshot>(`/api/conversations/${encodeURIComponent(id)}/assistant/messages`, "POST",
        { content, client_message_id: request.id }, request.key);
      if (!isCurrent(id, generation)) return;
      receive(id, value); messageRequest.current = null;
      setPrompt(previous => previous.trim() === content ? "" : previous); setView("chat"); followTail.current = true;
    } catch (cause) { if (isCurrent(id, generation)) setOperationError(errorText(cause)); }
    finally { if (isCurrent(id, generation)) { messageLock.current = false; setSending(false); } }
  }

  async function changeGoal(command: GoalCommand) {
    if (goalLock.current || messageLock.current) return false;
    goalLock.current = true; setGoalPending(true); setGoalError("");
    let { cid: id, generation } = scope.current;
    try {
      if (!id) {
        const created = await api<Conversation>("/api/conversations", "POST", { title: command.objective?.slice(0, 50) });
        if (!isCurrent(id, generation)) return false;
        id = created.id; scope.current.cid = id;
        callbacks.current.onCreated(created);
      }
      const { expectedVersion, ...fields } = command;
      const value = await api<AssistantSnapshot>(`/api/conversations/${encodeURIComponent(id)}/assistant/goal`, "POST",
        { ...fields, version: expectedVersion ?? current.current?.goal_version ?? 0 });
      if (!isCurrent(id, generation)) return false;
      receive(id, value);
      if (command.action === "set") { setView("chat"); followTail.current = true; }
      return true;
    } catch (cause) {
      if (isCurrent(id, generation)) { setGoalError(errorText(cause)); void refresh(); }
      return false;
    } finally {
      if (isCurrent(id, generation)) { goalLock.current = false; setGoalPending(false); }
    }
  }

  async function control(action: "pause" | "cancel") {
    const { cid: id, generation } = scope.current;
    if (!id || controlLocks.current[action]) return;
    controlLocks.current[action] = true; setControls(previous => ({ ...previous, [action]: true })); setOperationError(""); setNotice("");
    try {
      const value = await api<AssistantSnapshot>(`/api/conversations/${encodeURIComponent(id)}/assistant/control`, "POST",
        { action, ...(current.current?.execution_id ? { execution_id: current.current.execution_id } : {}) });
      if (isCurrent(id, generation)) receive(id, value);
    } catch (cause) { if (isCurrent(id, generation)) setOperationError(errorText(cause)); }
    finally { if (isCurrent(id, generation)) { controlLocks.current[action] = false; setControls(previous => ({ ...previous, [action]: false })); } }
  }

  async function decide(request: AssistantRequest, body: DecisionBody) {
    const { cid: id, generation } = scope.current;
    if (!id) return;
    setOperationError(""); setNotice("");
    try {
      const value = await api<AssistantSnapshot>(`/api/conversations/${encodeURIComponent(id)}/assistant/requests/${encodeURIComponent(request.id)}`, "POST", body);
      if (!isCurrent(id, generation)) return;
      receive(id, value);
      if (body.decision === "deny") setNotice(`已拒绝 ${request.tool || request.kind}，本次申请不授权执行。`);
    } catch (cause) {
      if (isCurrent(id, generation)) { setOperationError(errorText(cause)); void refresh(); }
    }
  }

  const state = snapshot?.state;
  const active = state === "starting" || state === "running" || state === "stopping";
  const canControl = Boolean(cid && (!state || active || ["waiting_approval", "waiting_input", "error"].includes(state) || sending));
  const messages = snapshot?.messages.filter(message => ["user", "assistant", "tool"].includes(message.role)) ?? [];
  const events = snapshot?.events.filter(event => !privateField.test(event.kind)) ?? [];
  const actions = snapshot?.actions ?? [];
  const { titles, materials, generated, saved } = useMemo(() => evidence(snapshot?.actions ?? []), [snapshot?.actions]);
  const roundStart = snapshot?.round_started_at;
  const roundActions = useMemo(() => (snapshot?.actions ?? []).filter(row =>
    roundStart ? (row.created_at ?? 0) >= roundStart : Boolean(snapshot?.execution_id && row.execution_id === snapshot.execution_id)
  ), [snapshot?.actions, snapshot?.execution_id, roundStart]);
  const round = useMemo(() => evidence(roundActions), [roundActions]);
  const running = actions.filter(a => a.status === "running" && a.execution_id === snapshot?.execution_id);
  const lastEvent = events.at(-1);
  const work = active && state !== "stopping" ? running.length
    ? [...new Set(running.map(a => toolLabels[a.tool] || "执行工具"))].join("、")
    : lastEvent?.kind === "model/status" && lastEvent.payload.status === "started"
      ? lastEvent.payload.purpose === "compaction" ? "正在压缩对话" : "主控模型处理中" : "" : "";
  const pendingCount = snapshot?.requests.length ?? 0;

  return <section className="dialogue-chat" aria-label="模型对话">
    <DialogueGoal goal={snapshot?.goal} version={snapshot?.goal_version ?? 0}
      ready={!cid || Boolean(state && ["idle", "paused", "cancelled"].includes(state) && !pendingCount)}
      pending={goalPending || sending} error={goalError} onCommand={changeGoal} />
    <div className="dialogue-statusbar">
      <div role="status"><span className={`status-dot ${state === "idle" ? "good" : ""}`} />
        {state ? stateLabels[state] : cid ? "正在读取对话" : "新对话"}
        {work && <small>{work}</small>}</div>
      <div className="dialogue-controls" role="group" aria-label="执行控制">
        {state === "paused" && <button className="icon-button" title="继续对话" aria-label="继续对话" disabled={sending} onClick={() => void sendMessage("继续")}><Play size={18} /></button>}
        <button className="icon-button" title="暂停对话" aria-label="暂停对话" disabled={!canControl || controls.pause} onClick={() => void control("pause")}>
          {controls.pause ? <LoaderCircle size={18} className="spin" /> : <Pause size={18} />}</button>
        <button className="icon-button dialogue-danger" title="取消执行" aria-label="取消执行" disabled={!cid || state === "cancelled" || controls.cancel} onClick={() => void control("cancel")}>
          {controls.cancel ? <LoaderCircle size={18} className="spin" /> : <Square size={17} />}</button>
        <button className="icon-button" title="刷新状态" aria-label="刷新状态" disabled={!cid} onClick={() => void refresh()}><RefreshCw size={17} /></button>
      </div>
    </div>
    {snapshot && <div className="dialogue-overview" aria-label="本对话累计">
      <span>本对话累计</span>
      <span><FileText size={15} />本地生成 <b>{generated.size}</b></span>
      <span><FileCheck2 size={15} />平台已确认 <b>{saved.size}</b></span>
      {roundStart && <span className="dialogue-round">本轮 · 本地 <b>{round.generated.size}</b> · 平台确认 <b>{round.saved.size}</b></span>}
      {pendingCount > 0 && <button type="button" onClick={showRequests}><ArrowDown size={14} />处理 {pendingCount} 项请求</button>}
    </div>}
    {snapshot && <nav aria-label="对话视图" className="dialogue-views">
      {([{id: "chat", label: "对话", Icon: MessageSquare}, {id: "events", label: "执行动态", Icon: Activity},
        {id: "actions", label: "工具与产物", Icon: Wrench}] as const).map(({id, label, Icon}) =>
        <button key={id} type="button" aria-pressed={view === id} onClick={() => {
          setView(id); if (id !== "chat" && feed.current) feed.current.scrollTop = 0;
        }}><Icon size={15} />{label}</button>)}
    </nav>}
    {connectionError && <div className="dialogue-alert" role="alert"><CircleAlert size={17} /><div>状态读取失败，正在重连。{connectionError}
      <div className="dialogue-links"><button className="text-button" onClick={() => void refresh()}><RefreshCw size={15} />重新读取</button><button className="text-button" onClick={onModels}><Settings2 size={15} />连接与模型</button></div></div></div>}
    <div className="dialogue-feed" ref={feed} onScroll={() => {
      const element = feed.current;
      if (element && view === "chat") followTail.current = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
    }}>
      {view === "chat" && !messages.length && !snapshot?.requests.length && <div className="dialogue-empty"><MessageSquare size={25} /><h2>采编对话</h2></div>}
      {view === "chat" && messages.map(message => <article key={message.id} className={`message ${message.role}`}>
        <small>{message.role === "user" ? "你" : message.role === "tool" ? "工具" : "智能体"} · {dateLabel(message.created_at)}</small>
        {message.role === "tool" ? <ToolMessageResult message={message} titles={titles} /> : <p>{message.content}</p>}
      </article>)}
      {view === "events" && <section className="dialogue-events" aria-label="实时事件"><h3><Activity size={16} />执行动态</h3>
        {!events.length && <p>尚无执行事件。</p>}<ol>{[...events].reverse().map(event => <li key={event.id}>
        <time>{dateLabel(event.created_at)}</time><div><EventContent event={event} />
        </div></li>)}</ol></section>}
      {view === "actions" && <section className="dialogue-actions" aria-label="工具与产物">
        <div className="dialogue-result-heading"><h3><Wrench size={16} />工具与产物</h3>
          {roundStart && <label><input type="checkbox" checked={onlyRound} onChange={event => setOnlyRound(event.target.checked)} />仅本轮</label>}</div>
        {!(onlyRound && roundStart ? roundActions : actions).length && <p>尚无工具执行记录。</p>}
        {(onlyRound && roundStart ? roundActions : actions).map(action => <ToolResult key={action.id} action={action} titles={titles} />)}</section>}
      {!!pendingCount && <div className="dialogue-inbox" hidden={view !== "chat"}>
        {snapshot?.requests.map(request => <RequestCard key={`${request.id}:${request.version}`} request={request}
          subject={materials.get(String(request.arguments.material_id)) || titles.get(String(request.arguments.post_id))}
          onDecision={decide} onCapabilities={onCapabilities} onModels={onModels} />)}</div>}
      {state === "stopping" && <p className="dialogue-state-note" role="status"><LoaderCircle size={16} className="spin" />等待执行进程确认停止。</p>}
      {state === "cancelled" && <p className="dialogue-state-note">本次执行已取消，已有稿件与回执保留。</p>}
      {state === "error" && <div className="dialogue-alert"><CircleAlert size={17} /><div>当前执行受阻。
        <div className="dialogue-links"><button className="text-button" onClick={onCapabilities}><Puzzle size={15} />检查能力</button><button className="text-button" onClick={onModels}><Settings2 size={15} />检查模型</button></div></div></div>}
      {operationError && <div className="dialogue-alert" role="alert"><CircleAlert size={17} /><div>{operationError}
        <div className="dialogue-links"><button className="text-button" onClick={onCapabilities}><Puzzle size={15} />能力中心</button><button className="text-button" onClick={onModels}><Settings2 size={15} />连接与模型</button></div></div></div>}
      {notice && <p className="dialogue-state-note" role="status"><Check size={16} />{notice}</p>}
    </div>
    <form className="composer dialogue-composer" onSubmit={event => { event.preventDefault(); void sendMessage(); }}>
      <label htmlFor="dialogue-prompt">{active ? "补充要求" : "消息"}</label>
      <textarea id="dialogue-prompt" rows={3} value={prompt} onChange={event => setPrompt(event.target.value)} placeholder="输入消息…"
        onKeyDown={event => { if (event.key === "Enter" && !event.nativeEvent.isComposing && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void sendMessage(); } }} />
      <div className="composer-actions"><span>{sending ? "正在提交消息" : state === "waiting_input" ? "等待你的补充" : state === "paused" ? "已暂停" : ""}</span>
        <button className="primary-button" type="submit" disabled={sending || goalPending || !prompt.trim()}>{sending ? <LoaderCircle size={16} className="spin" /> : <Send size={16} />}发送</button></div>
    </form>
  </section>;
}
