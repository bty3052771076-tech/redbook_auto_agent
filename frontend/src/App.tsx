import { useCallback, useEffect, useRef, useState } from "react";
import { Activity, Check, CheckCircle2, ChevronRight, CircleAlert, ClipboardList, Database, FileText, Image, LoaderCircle, Menu, MessageSquare, Pause, Play, Puzzle, RefreshCw, Send, Settings2, ShieldCheck, Square, X } from "lucide-react";
import {
  api, startSession, type Connections, type Conversation, type Draft, type DraftSummary,
  type ContextOverview, type GoalSnapshot, type Model, type Plan, type Run, type TurnResponse,
} from "./api";
import { GoalProgress, RunProgress, TechnicalLog, TurnProgress, activeStatuses, pendingTurnStates } from "./RunProgress";
import { WoolGallery } from "./WoolGallery";
import { PlanJobs, TaskCalibration } from "./TaskCalibration";
import { SourceDiagnostics } from "./SourceDiagnostics";
import { ModelPlatforms } from "./ModelPlatforms";
import { PlanModels } from "./PlanModels";
import { ContinuationDiff, PlanEditor } from './PlanEditor';
import { CopyPlan, PlanHistory } from './PlanHistory';
import { CapabilityCenter } from "./capabilities/CapabilityCenter";
import { PlanCapabilities, readPlanSelection } from "./capabilities/PlanCapabilities";
import { guarded } from "./capabilities/shared";
import { CompactionEvents } from "./capabilities/Memory";
import "./source-diagnostics.css";

type Page = "chat" | "drafts" | "runs" | "connections" | "wool" | "sources" | "capabilities";
const pages: { id: Page; label: string; icon: typeof MessageSquare }[] = [
  { id: "chat", label: "对话任务", icon: MessageSquare },
  { id: "drafts", label: "草稿审查", icon: FileText },
  { id: "runs", label: "运行记录", icon: ClipboardList },
  { id: "connections", label: "连接与模型", icon: Settings2 },
  { id: "capabilities", label: "能力中心", icon: Puzzle },
  { id: "sources", label: "信源健康", icon: Activity },
  { id: "wool", label: "AI鸡蛋图库", icon: Image },
];
const requiredChecks = [
  ["source", "来源与原文"], ["date", "日期与时区"], ["body", "事件全貌"], ["image", "图文一致"],
] as const;
const roleNames: Record<string, string> = { agent: "智能体主控", writer: "新闻写稿", image: "AI 生图" };

function niceDate(value?: string | number) {
  if (!value) return "";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}

function DraftEvidence({ evidence }: { evidence: Draft["evidence"] }) {
  return <section className="evidence-section">
    <h3>原始信源</h3>
    {evidence.length ? <ul>{evidence.map((item, index) => <li key={`${item.url}-${index}`}>
      <a href={item.url} target="_blank" rel="noopener noreferrer">{item.title}</a>
      <span>{item.source || "来源未记录"} · {item.published_at ? niceDate(item.published_at) : "发布时间未记录"}</span>
    </li>)}</ul> : <p className="muted">本地记录未保存可打开的原始信源，请另行查证。</p>}
  </section>;
}

function App() {
  const [page, setPage] = useState<Page>(location.hash.startsWith("#capabilities") ? "capabilities" : "chat");
  const [mobileNav, setMobileNav] = useState(false);
  const [ready, setReady] = useState(false);
  const [sessionReady, setSessionReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [prompt, setPrompt] = useState("");
  const [plan, setPlan] = useState<Plan | null>(null);
  const [selectedGoalId, setSelectedGoalId] = useState('');
  const [contextOverview, setContextOverview] = useState<ContextOverview | null>(null);
  const [continuityError, setContinuityError] = useState('');
  const [statusBusy, setStatusBusy] = useState(false);
  const goalSelection = useRef({ conversationId: '', goalId: '' });
  const dismissedCandidates = useRef(new Set<string>());
  const conversationRef = useRef(conversation);
  conversationRef.current = conversation;
  const messageInFlight = useRef(false), statusInFlight = useRef(false), confirmInFlight = useRef(false);
  const messageRequest = useRef<{ signature: string; id: string; key: string } | null>(null);
  const confirmRequest = useRef<{ signature: string; key: string } | null>(null);
  const [calibrationPending, setCalibrationPending] = useState(false);
  const [capabilityPending, setCapabilityPending] = useState(false);
  const [planEditing, setPlanEditing] = useState(false);
  const [runs, setRuns] = useState<Run[]>([]);
  const [activeRun, setActiveRun] = useState<Run | null>(null);
  const [runConnectionError, setRunConnectionError] = useState("");
  const [drafts, setDrafts] = useState<DraftSummary[]>([]);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [checks, setChecks] = useState<Record<string, boolean>>({ source: false, date: false, body: false, image: false });
  const [connections, setConnections] = useState<Connections | null>(null);
  const [roles, setRoles] = useState<Record<string, string>>({ agent: "", writer: "", image: "" });
  const selectionVersion = useRef(0);
  const [selectionEpoch, setSelectionEpoch] = useState(0);
  const goals = conversation?.goals || [];
  const selectedGoal = goals.find(goal => goal.goal_id === selectedGoalId);
  const hasContinuity = conversation?.goals !== undefined || conversation?.turns !== undefined;
  const goalPending = (conversation?.turns || []).some(turn => turn.goal_id === selectedGoalId && pendingTurnStates.has(turn.state) && turn.kind !== 'status');

  function receiveConversation(loaded: Conversation, explicitGoalId?: string) {
    const sameConversation = conversationRef.current?.id === loaded.id;
    const requested = explicitGoalId ?? (goalSelection.current.conversationId === loaded.id ? goalSelection.current.goalId : '');
    const goalId = loaded.goals?.some(goal => goal.goal_id === requested) ? requested : loaded.goals?.length === 1 ? loaded.goals[0].goal_id : '';
    goalSelection.current = { conversationId: loaded.id, goalId };
    setSelectedGoalId(goalId);
    const goal = loaded.goals?.find(item => item.goal_id === goalId);
    const turn = loaded.turns?.filter(item => item.goal_id === goalId && item.proposal_plan_id
      && ['awaiting_confirmation', 'accepted', 'queued', 'applying'].includes(item.state) && !dismissedCandidates.current.has(item.turn_id)).at(-1);
    const candidate = turn && (loaded.plans.filter(item => item.continuation?.turn_id === turn.turn_id
      && item.continuation.goal_id === goalId && item.status !== 'discarded').at(-1)
      || loaded.plans.find(item => item.id === turn.proposal_plan_id));
    const editable = loaded.plans.filter(item => {
      if (item.continuation || item.job_id || item.execution_request_id || item.status === 'discarded') return false;
      if (!goal) return true;
      let parent: string | undefined = item.id;
      const seen = new Set<string>();
      while (parent && !seen.has(parent)) {
        if (parent === goal.active_plan_id) return true;
        seen.add(parent);
        parent = loaded.plans.find(p => p.id === parent)?.parent_plan_id;
      }
      return false;
    }).at(-1);
    const current = candidate || editable || loaded.plans.find(item => item.id === goal?.active_plan_id) || goal?.plan || loaded.plans.filter(item => !item.continuation).at(-1);
    setPlan(previous => current || (sameConversation ? previous : null));
    if (!sameConversation) { setContextOverview(null); setContinuityError(''); }
    conversationRef.current = loaded;
    setConversation(loaded);
  }

  function receiveTurnResponse(cid: string, response: TurnResponse) {
    const previous = conversationRef.current;
    if (!previous || previous.id !== cid) return;
    const messages = [...previous.messages];
    for (const message of [response.message, response.assistant]) if (message && !messages.some(item => item.id === message.id)) messages.push(message);
    const updated = { ...previous, messages, plans: [...previous.plans], runs: [...previous.runs] };
    if (response.plan) updated.plans = [...updated.plans.filter(item => item.id !== response.plan!.id), response.plan];
    if (response.goal) updated.goals = [...(updated.goals || []).filter(item => item.goal_id !== response.goal!.goal_id), response.goal];
    if (response.turn) updated.turns = [...(updated.turns || []).filter(item => item.turn_id !== response.turn!.turn_id), response.turn];
    if (response.run) { if (!updated.runs.includes(response.run.id)) updated.runs.push(response.run.id); setActiveRun(response.run); }
    receiveConversation(updated, response.action === 'new_goal' ? response.goal?.goal_id : undefined);
    // Nullable status/continue responses must not clear the plan already on screen.
    if (response.plan) setPlan(response.plan);
  }
  useEffect(() => {
    const listener = () => { if (location.hash.startsWith("#capabilities")) setPage("capabilities"); else if (location.hash === "#chat") setPage("chat"); };
    window.addEventListener("hashchange", listener);
    return () => window.removeEventListener("hashchange", listener);
  }, []);
  function goPage(next: Page) {
    guarded(() => { setPage(next); setMobileNav(false); location.hash = next === "capabilities" ? "#capabilities/overview" : `#${next}`; });
  }
  async function showCapabilityRun(id: string) {
    try { setActiveRun(await api<Run>(`/api/runs/${id}`)); setRunConnectionError(""); goPage("runs"); }
    catch (cause) { fail(cause); }
  }

  const fail = (reason: unknown) => { setError(reason instanceof Error ? reason.message : String(reason)); setNotice(""); };
  const load = useCallback(async () => {
    try {
      const [allConversations, allRuns, allDrafts, allConnections] = await Promise.all([
        api<{ rows: Conversation[] }>("/api/conversations"),
        api<{ rows: Run[] }>("/api/runs"),
        api<{ rows: DraftSummary[] }>("/api/drafts"),
        api<Connections>("/api/connections"),
      ]);
      setConversations(allConversations.rows);
      setRuns(allRuns.rows);
      setDrafts(allDrafts.rows);
      setConnections(allConnections);
      setRoles(allConnections.providers.bindings);
      setReady(true);
      setError("");
    } catch (cause) {
      setReady(false); setConnections(null);
      throw cause;
    }
  }, []);

  useEffect(() => {
    startSession().then(async () => {
      setSessionReady(true);
      await load();
      const saved = localStorage.getItem("agent-conversation");
      if (saved) await openConversation(saved);
    }).catch(fail);
  }, [load]);
  useEffect(() => {
    if ((!activeRun || !activeStatuses.has(activeRun.status)) && !(hasContinuity && page === 'chat')) return;
    const runId = activeRun?.id;
    const conversationId = conversation?.id;
    const version = selectionVersion.current;
    let disposed = false;
    let timer: number;
    const poll = async () => {
      try {
        const current = runId ? await api<Run>(`/api/runs/${runId}`) : null;
        if (disposed) return;
        if (version !== selectionVersion.current) return;
        if (current) setActiveRun(current);
        setRunConnectionError("");
        if (hasContinuity && conversationId && page === 'chat') {
          const updated = await api<Conversation>(`/api/conversations/${conversationId}`);
          const goalRows = await api<{ rows: GoalSnapshot[] }>(`/api/conversations/${conversationId}/goals`);
          if (disposed || version !== selectionVersion.current) return;
          receiveConversation({ ...updated, goals: goalRows.rows }); setContinuityError('');
          try {
            const context = await api<ContextOverview>(`/api/conversations/${conversationId}/context`);
            if (!disposed && version === selectionVersion.current) setContextOverview(context);
          } catch (cause) { if (!disposed) setContinuityError(cause instanceof Error ? cause.message : String(cause)); }
        }
        if (current && !activeStatuses.has(current.status) && !hasContinuity) {
          await load();
          if (disposed) return;
          if (conversationId) {
            const updated = await api<Conversation>(`/api/conversations/${conversationId}`);
            if (!disposed && version === selectionVersion.current) receiveConversation(updated);
          }
          return;
        }
      } catch (cause) {
        if (!disposed && version === selectionVersion.current) {
          const message = cause instanceof Error ? cause.message : String(cause);
          if (hasContinuity) setContinuityError(message); else setRunConnectionError(message);
        }
      }
      if (!disposed) timer = window.setTimeout(poll, 2000);
    };
    timer = window.setTimeout(poll, 2000);
    return () => { disposed = true; window.clearTimeout(timer); };
  }, [activeRun?.id, activeRun?.status, conversation?.id, hasContinuity, page, selectionEpoch, load]);

  async function refreshRun() {
    if (!activeRun) return;
    const version = selectionVersion.current;
    const id = activeRun.id;
    try {
      const updated = await api<Run>(`/api/runs/${id}`);
      if (version !== selectionVersion.current) return;
      setActiveRun((previous) => previous?.id === id ? updated : previous); setRunConnectionError("");
    } catch (cause) { if (version === selectionVersion.current) setRunConnectionError(cause instanceof Error ? cause.message : String(cause)); }
  }

  async function createConversation() {
    selectionVersion.current++;
    setSelectionEpoch(value => value + 1);
    setBusy(true); setError("");
    try {
      const created = await api<Conversation>("/api/conversations", "POST", { title: "新对话" });
      receiveConversation(created); setPlan(null); setActiveRun(null); setRunConnectionError("");
      localStorage.setItem("agent-conversation", created.id); await load();
    } catch (cause) { fail(cause); } finally { setBusy(false); }
  }

  async function openConversation(id: string) {
    const version = ++selectionVersion.current;
    setSelectionEpoch(value => value + 1);
    setError("");
    try {
      let loaded = await api<Conversation>(`/api/conversations/${id}`);
      if (version !== selectionVersion.current) return;
      if (loaded.goals !== undefined || loaded.turns !== undefined) {
        try {
          const goalRows = await api<{ rows: GoalSnapshot[] }>(`/api/conversations/${id}/goals`);
          loaded = { ...loaded, goals: goalRows.rows };
        } catch (cause) { if (version === selectionVersion.current) setContinuityError(cause instanceof Error ? cause.message : String(cause)); }
      }
      const goalId = goalSelection.current.conversationId === id ? goalSelection.current.goalId : loaded.goals?.length === 1 ? loaded.goals[0].goal_id : '';
      const goal = loaded.goals?.find(item => item.goal_id === goalId);
      const latest = goal?.active_attempt_id || loaded.runs.at(-1);
      const run = latest ? await api<Run>(`/api/runs/${latest}`) : null;
      if (version !== selectionVersion.current) return;
      receiveConversation(loaded);
      localStorage.setItem("agent-conversation", loaded.id); setRunConnectionError("");
      setActiveRun(run);
    } catch (cause) { if (version === selectionVersion.current) fail(cause); }
  }

  async function sendMessage(text?: string) {
    const content = (text ?? prompt).trim();
    if (!content) return;
    const statusOnly = text === '查看当前任务进度';
    if (statusOnly ? statusInFlight.current : messageInFlight.current) return;
    if (!hasContinuity && activeRun && !activeRun.activity && content.length <= 160
      && !/生成|重做|重新|上传|发布|删除|停止|取消|恢复|执行|添加|修改/.test(content)
      && /进度|状态|到哪|哪一步|卡在|还要多久|用了多久|完成了|完成没|成功了|结束了|怎么样了/.test(content)) {
      await refreshRun(); if (!text) setPrompt(""); return;
    }
    if (statusOnly) { statusInFlight.current = true; setStatusBusy(true); }
    else { messageInFlight.current = true; setBusy(true); }
    setError("");
    const version = selectionVersion.current;
    try {
      let current = conversation;
      if (!current) current = await api<Conversation>("/api/conversations", "POST", { title: content.slice(0, 50) });
      if (version !== selectionVersion.current) return;
      if (!conversationRef.current) { receiveConversation(current); localStorage.setItem('agent-conversation', current.id); }
      const goal = current.goals?.find(item => item.goal_id === goalSelection.current.goalId);
      const payload = { content, goal_id: goal?.goal_id ?? null, expected_goal_revision: goal?.revision,
        expected_attempt_id: goal?.active_attempt_id || undefined };
      const signature = JSON.stringify({ cid: current.id, ...payload });
      if (!statusOnly && (!messageRequest.current || messageRequest.current.signature !== signature)) messageRequest.current = { signature, id: crypto.randomUUID(), key: crypto.randomUUID() };
      const request = statusOnly ? { id: crypto.randomUUID(), key: crypto.randomUUID() } : messageRequest.current!;
      const result = await api<TurnResponse>(`/api/conversations/${current.id}/messages`, "POST", { ...payload, client_message_id: request.id }, request.key);
      if (version !== selectionVersion.current) return;
      receiveTurnResponse(current.id, result);
      if (!statusOnly) messageRequest.current = null;
      if (!text) setPrompt(previous => previous.trim() === content ? '' : previous);
      // Refresh only reads server state; it never resubmits accepted commands.
      if (!hasContinuity && !result.turn && !result.goal) await openConversation(current.id);
      await load();
    } catch (cause) { if (version === selectionVersion.current) fail(cause); }
    finally { if (statusOnly) { statusInFlight.current = false; setStatusBusy(false); } else { messageInFlight.current = false; setBusy(false); } }
  }

  async function askProgress() {
    if (!hasContinuity && activeRun && !activeRun.activity) { await refreshRun(); return; }
    await sendMessage("查看当前任务进度");
  }

  async function confirmPlan() {
    if (!conversation || !plan || !plan.executable || calibrationPending || capabilityPending || planEditing || confirmInFlight.current) return;
    const continuation = plan.continuation;
    const turn = conversation.turns?.find(item => item.turn_id === continuation?.turn_id);
    if (continuation && (turn?.state !== 'awaiting_confirmation' || !plan.semantic_hash)) return;
    confirmInFlight.current = true;
    const version = selectionVersion.current;
    setBusy(true); setError("");
    try {
      if (continuation) {
        const body = { expected_goal_revision: continuation.expected_goal_revision, proposal_hash: plan.semantic_hash };
        const signature = JSON.stringify({ cid: conversation.id, tid: continuation.turn_id, ...body });
        if (confirmRequest.current?.signature !== signature) confirmRequest.current = { signature, key: crypto.randomUUID() };
        const response = await api<TurnResponse>(`/api/conversations/${conversation.id}/turns/${continuation.turn_id}/confirm`, 'POST', body, confirmRequest.current.key);
        if (version !== selectionVersion.current) return;
        receiveTurnResponse(conversation.id, response);
        setNotice(response.turn?.state === 'applied' ? '修改已应用。' : '修改已接收，等待当前操作结束后生效。');
        return;
      }
      const selection = await readPlanSelection(conversation.id, plan.id);
      const result = await api<Run>(`/api/plans/${plan.id}/confirm`, "POST", {
        conversation_id: conversation.id, version: plan.version, semantic_hash: plan.semantic_hash,
        configuration_fingerprint: plan.configuration_fingerprint, ...selection,
      });
      setActiveRun(result); setNotice("任务已提交，当前进度将在对话中持续更新。");
      await load(); await openConversation(conversation.id);
    } catch (cause) { if (version === selectionVersion.current) fail(cause); }
    finally { confirmInFlight.current = false; setBusy(false); }
  }

  async function discardCandidate() {
    if (!conversation || !continuation) return;
    const version = selectionVersion.current;
    setBusy(true); setError('');
    try {
      const response = await api<TurnResponse>(`/api/conversations/${conversation.id}/turns/${continuation.turn_id}/discard`, 'POST', {});
      if (version !== selectionVersion.current) return;
      dismissedCandidates.current.add(continuation.turn_id);
      receiveTurnResponse(conversation.id, response);
      setNotice('已放弃候选，原计划和已有产物已保留。');
    } catch (cause) { if (version === selectionVersion.current) fail(cause); }
    finally { setBusy(false); }
  }

  async function openDraft(id: string) {
    setError(""); setChecks({ source: false, date: false, body: false, image: false });
    try { setDraft(await api<Draft>(`/api/drafts/${id}`)); } catch (cause) { fail(cause); }
  }

  async function showRunDraft(id: string) {
    await openDraft(id);
    setPage("drafts");
  }

  async function approveDraft() {
    if (!draft) return;
    setBusy(true); setError("");
    try {
      await api(`/api/drafts/${draft.id}/review`, "POST", { updated_at: draft.updated_at, checks, note: "人工核验通过" });
      setNotice(`已记录《${draft.title}》的核验结果；没有公开发布。`);
    } catch (cause) { fail(cause); } finally { setBusy(false); }
  }

  async function saveRoles() {
    setBusy(true); setError("");
    try {
      await api("/api/model-roles", "PUT", roles);
      setNotice("模型角色已保存；运行中的任务继续使用启动时冻结的配置。");
      await load();
    } catch (cause) { fail(cause); } finally { setBusy(false); }
  }

  async function refreshModelConfiguration() {
    await load();
    if (conversation) await openConversation(conversation.id);
  }

  async function openProfile() {
    setBusy(true); setError("");
    try {
      const result = await api<{ login: string }>("/api/profile/open", "POST");
      setNotice(result.login);
    } catch (cause) { fail(cause); } finally { setBusy(false); }
  }

  const currentPlan = plan && conversation?.plans.find((item) => item.id === plan.id);
  const continuation = plan?.continuation;
  const candidateTurn = conversation?.turns?.find(turn => turn.turn_id === continuation?.turn_id);
  const candidateLocked = Boolean(continuation && candidateTurn?.state !== 'awaiting_confirmation');
  const planSubmitted = Boolean(currentPlan?.job_id || currentPlan?.execution_request_id);
  const planRunning = Boolean(planSubmitted && activeRun &&
    [currentPlan?.job_id, currentPlan?.resume_job_id].includes(activeRun.id) && activeStatuses.has(activeRun.status));
  const modelRows: Model[] = connections?.models.rows || [];
  const verified = requiredChecks.every(([key]) => checks[key]);

  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobileNav ? "sidebar-open" : ""}`}>
        <div className="brand"><span className="brand-mark" />采编智能体</div>
        <nav aria-label="主导航">
          {pages.map(({ id, label, icon: Icon }) => (
            <button key={id} className={`nav-item ${page === id ? "active" : ""}`} onClick={() => goPage(id)}>
              <Icon size={18} strokeWidth={1.8} /><span>{label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-status"><span>本地运行</span><strong><span className={`status-dot ${connections?.database.status === "ready" ? "good" : "bad"}`} />PostgreSQL {connections?.database.status === "ready" ? "已连接" : "待连接"}</strong></div>
      </aside>
      {mobileNav && <button className="mobile-scrim" aria-label="关闭导航" onClick={() => setMobileNav(false)} />}
      <div className="main-shell">
        <header className="topbar">
          <button className="icon-button mobile-menu" aria-label="打开导航" title="打开导航" onClick={() => setMobileNav(true)}><Menu size={19} /></button>
          <span>本地工作空间 / 内容生产</span>
          <div className="topbar-right"><span>{activeRun?.model_snapshots?.agent?.upstream_model_id || modelRows.find(model => model.id === (plan?.model_roles?.agent || roles.agent))?.model || "继承主控配置"}</span><span>项目专用浏览器</span></div>
        </header>
        {!sessionReady && <div className="startup"><LoaderCircle size={18} className="spin" />正在连接本地服务</div>}
        {sessionReady && !ready && <div className="startup"><CircleAlert size={18} />业务数据暂不可用，可查看能力中心。<button className="quiet-button" onClick={() => load().catch(fail)}><RefreshCw size={16} />重新读取业务数据</button></div>}
        {error && <div className="banner error" role="alert"><CircleAlert size={17} /><span>{error}</span><button className="icon-button" title="关闭提示" aria-label="关闭提示" onClick={() => setError("")}><X size={16} /></button></div>}
        {notice && <div className="banner notice" role="status"><CheckCircle2 size={17} /><span>{notice}</span><button className="icon-button" title="关闭提示" aria-label="关闭提示" onClick={() => setNotice("")}><X size={16} /></button></div>}
        {ready && page === "chat" && <div className="chat-layout">
          <section className="chat-main">
            <div className="page-heading"><div><h1>任务对话</h1><p>说明栏目和数量，确认计划后执行。</p></div><button className="quiet-button" onClick={createConversation} disabled={busy}><MessageSquare size={16} />新对话</button></div>
            <div className="context-line"><span className="status-dot good" />已发布数据和知识库状态：{connections?.database.status === "ready" ? "数据库可用" : "待检查"}</div>
            <div className="conversation-selector"><label htmlFor="conversation-select">当前对话</label><select id="conversation-select" value={conversation?.id || ""} onChange={(event) => { if (event.target.value) openConversation(event.target.value); else { selectionVersion.current++; setSelectionEpoch(v => v + 1); conversationRef.current = null; setConversation(null); setSelectedGoalId(''); setContextOverview(null); setPlan(null); setActiveRun(null); localStorage.removeItem("agent-conversation"); } }}><option value="">新任务</option>{conversations.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select></div>
            {hasContinuity && <section className="conversation-goals" aria-label="对话目标">
              <div className="conversation-selector"><label htmlFor="goal-select">当前目标</label><select id="goal-select" value={selectedGoalId} disabled={planEditing} onChange={async event => {
                if (!conversation) return;
                const goalId = event.target.value, version = ++selectionVersion.current;
                setSelectionEpoch(v => v + 1); receiveConversation(conversation, goalId); setActiveRun(null);
                const goal = goals.find(row => row.goal_id === goalId);
                if (goal?.active_attempt_id) try {
                  const run = await api<Run>(`/api/runs/${goal.active_attempt_id}`);
                  if (version === selectionVersion.current) setActiveRun(run);
                } catch (cause) { if (version === selectionVersion.current) fail(cause); }
              }}><option value="">{goals.length > 1 ? '请选择要继续的目标' : '未选择目标'}</option>{goals.map(goal => <option key={goal.goal_id} value={goal.goal_id}>{goal.jobs.map(job => job.title).join('、')} · {goal.target_date || goal.plan?.target_date || goal.goal_id.slice(0, 8)} · 修订 {goal.revision}</option>)}</select></div>
              {selectedGoal && <><GoalProgress goal={selectedGoal} /><div className="goal-actions" role="group" aria-label="目标操作">
                <button className="icon-button" title="继续目标" aria-label="继续目标" disabled={busy || goalPending || Boolean(continuation) || ['completed', 'cancelled'].includes(selectedGoal.state)} onClick={() => sendMessage('继续')}><Play size={17} /></button>
                <button className="icon-button" title="暂停目标" aria-label="暂停目标" disabled={busy || goalPending || ['paused', 'completed', 'cancelled'].includes(selectedGoal.state)} onClick={() => sendMessage('暂停当前目标')}><Pause size={17} /></button>
                <button className="icon-button" title="取消目标" aria-label="取消目标" disabled={busy || goalPending || ['completed', 'cancelled'].includes(selectedGoal.state)} onClick={() => sendMessage('取消当前目标')}><Square size={16} /></button>
              </div></>}
              {continuityError && <p className="calibration-error" role="status">状态连接中断，保留上次记录：{continuityError}</p>}
            </section>}
            <div className="messages">{conversation?.messages.length ? conversation.messages.map((message) => <div key={message.id} className={`message ${message.role}`}><small>{message.role === "user" ? "你" : "智能体"} · {niceDate(message.created_at)}</small><p>{message.content}</p></div>) : <div className="empty-chat"><MessageSquare size={22} /><strong>开始一项采编任务</strong><span>输入自然语言要求，智能体先展示可确认的执行计划。</span></div>}
              <TurnProgress turns={(conversation?.turns || []).filter(turn => !selectedGoalId || !turn.goal_id || turn.goal_id === selectedGoalId)} />
              {conversation && <CompactionEvents overview={contextOverview} conversationId={conversation.id} compact />}
              {activeRun && conversation?.runs.includes(activeRun.id) && <div className="message assistant live-message"><small>智能体 · 运行播报</small><RunProgress run={activeRun} connectionError={runConnectionError} onRefresh={refreshRun} /><TechnicalLog run={activeRun} /></div>}
            </div>
            <div className="composer"><label htmlFor="prompt">任务要求</label><textarea id="prompt" rows={3} value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="例如：生成5条每日新闻，关键词：伊朗、关税、芯片；保存到小红书草稿箱" onKeyDown={(event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); sendMessage(); } }} /><div className="composer-actions">{activeRun || hasContinuity ? <button className="text-button" title="询问进度" disabled={statusBusy} onClick={askProgress}><RefreshCw size={14} />询问进度</button> : <span>新任务生成前确认计划</span>}<button className="primary-button" disabled={busy || !prompt.trim()} onClick={() => sendMessage()}><Send size={16} />发送</button></div></div>
          </section>
          <aside className="plan-pane">
            <div className="pane-heading"><h2>本次计划</h2><span className="status-amber">{continuation ? candidateLocked ? '待生效' : '修改待确认' : plan ? planRunning ? "执行中" : planSubmitted ? "已执行" : plan.executable ? "待确认" : "需补充" : "等待输入"}</span></div>
            {plan ? <>
              {continuation && conversation && <section className="continuation-proposal" aria-label="剩余任务修改">
                <ContinuationDiff plan={plan} />
                <div className="calibration-actions"><PlanEditor conversation={conversation} plan={plan} models={modelRows} disabled={busy || candidateLocked} onSaved={() => openConversation(conversation.id)} onPending={setPlanEditing} />
                  <button className="quiet-button" disabled={busy || candidateLocked || planEditing} onClick={() => void discardCandidate()}><X size={15} />放弃候选</button>
                  <button className="primary-button" disabled={busy || calibrationPending || planEditing || !plan.executable || !plan.semantic_hash || candidateLocked} onClick={confirmPlan}><Check size={16} />确认修改并继续</button>
                </div>
              </section>}
              {conversation && !continuation && <PlanEditor conversation={conversation} plan={plan} models={modelRows}
                disabled={busy || planSubmitted} onSaved={() => openConversation(conversation.id)} onPending={setPlanEditing} />}
              {conversation && !continuation && <PlanHistory conversation={conversation} plan={plan}
                disabled={busy || planSubmitted || planEditing || capabilityPending} onSaved={() => openConversation(conversation.id)} />}
              {conversation && !continuation && planSubmitted && <CopyPlan key={plan.id} conversation={conversation} plan={plan}
                disabled={busy || planEditing || capabilityPending} onSaved={()=>openConversation(conversation.id)} />}
              {conversation && <PlanModels conversationId={conversation.id} plan={plan} models={modelRows}
                defaultRoles={roles} onDefaultsChanged={refreshModelConfiguration}
                disabled={busy || Boolean(continuation) || planSubmitted || calibrationPending || planEditing} onChanged={updated => {
                  setPlan(updated); void openConversation(conversation.id);
                }} />}
              {conversation && <TaskCalibration key={`calibration:${conversation.id}:${plan.id}`} conversation={conversation} plan={plan} models={modelRows}
                disabled={busy || candidateLocked || planSubmitted || (!continuation && Boolean(activeRun && activeStatuses.has(activeRun.status)))}
                onPendingChange={setCalibrationPending} onEditing={setPlanEditing} onAdopt={() => openConversation(conversation.id)} />}
              <h3 className="current-plan-heading">当前计划</h3>
              <PlanJobs jobs={plan.jobs} />
              {conversation && <PlanCapabilities key={`capabilities:${conversation.id}:${plan.id}`} conversationId={conversation.id} plan={plan}
                runId={planSubmitted ? currentPlan?.resume_job_id || currentPlan?.job_id : undefined}
                disabled={busy || Boolean(continuation) || planSubmitted || calibrationPending} onPending={setCapabilityPending} onChanged={updated => {
                  setPlan(updated); void openConversation(conversation.id);
                }} />}
              {!!plan.unresolved_requirements?.length && <ul className="calibration-issues">{plan.unresolved_requirements.map((item, index) => <li key={index}>{item}</li>)}</ul>}
              <div className="plan-boundary"><h3>执行边界</h3><p>交付：{plan.delivery === "generate_only" ? "仅生成本地稿" : "保存至草稿箱"}</p><p>平台：{plan.platform === "xhs" ? "小红书" : plan.platform}</p><p>运行模式：{plan.performance_mode === "speed" ? "速度优先" : "速度与稳定平衡"}</p><p>公开发布：本次不执行</p></div>
              {!continuation && <button className="primary-button full" disabled={busy || calibrationPending || capabilityPending || planEditing || !plan.executable || planSubmitted} onClick={confirmPlan}><Play size={16} />{planRunning ? "正在执行" : planSubmitted ? "计划已执行" : "确认并执行"}</button>}
            </> : <p className="muted">输入任务后，这里会显示栏目、数量和交付方式。</p>}
            {activeRun && <div className="run-inline"><strong>最近一次运行</strong><RunProgress run={activeRun} compact connectionError={runConnectionError} /><button className="text-button" onClick={() => setPage("runs")}>查看运行记录 <ChevronRight size={14} /></button></div>}
          </aside>
        </div>}
        {sessionReady && page === "capabilities" && <CapabilityCenter conversations={conversations} runs={runs} onRun={showCapabilityRun} />}
        {ready && page === "sources" && <main className="standard-page"><div className="page-heading"><h1>信源健康</h1></div>
          <SourceDiagnostics read={() => api("/api/sources")} check={request => {
            const { kind: _kind, title: _title, ...body } = request;
            return api("/api/sources/check", "POST", body, crypto.randomUUID());
          }}/></main>}
        {ready && page === "wool" && <main className="standard-page"><WoolGallery request={(path, method, body) => api("/api" + path, method, body)}/></main>}
        {ready && page === "drafts" && <div className="draft-layout">
          <section className="draft-list">
            <div className="page-heading"><div><h1>待审草稿</h1><p>先核对内容再决定下一步。</p></div></div>
            <div className="list-tabs"><strong>全部 {drafts.length}</strong><span>已上传 {drafts.filter((item) => item.uploaded).length}</span></div>
            <div className="draft-items">{drafts.map((item) => <button key={item.post_id} className={`draft-item ${draft?.id === item.post_id ? "selected" : ""}`} onClick={() => openDraft(item.post_id)}><strong>{item.title || "未命名草稿"}</strong><span>{item.body_preview || "暂无正文摘要"}</span><small>{item.status} · 图片 {item.asset_count} 张</small></button>)}{!drafts.length && <p className="muted empty-list">独立运行区还没有本地草稿。</p>}</div>
          </section>
          <section className="draft-detail">{draft ? <>
            <div className="page-heading"><div><h2>{draft.title}</h2><p>本地预览不代表平台审核通过。</p></div><span className="status-amber">{draft.readback === "verified" ? "平台已回读" : "未回读"}</span></div>
            <div className="draft-meta"><span>更新时间<strong>{niceDate(draft.updated_at)}</strong></span><span>平台状态<strong>{draft.status}</strong></span><span>图片数量<strong>{draft.assets.length} 张</strong></span></div>
            <DraftEvidence evidence={draft.evidence} />
            <div className="draft-preview"><div><h3>正文预览</h3><p>{draft.body}</p></div>{draft.assets.length ? <img src={draft.assets[0].url.replace("/api/posts/", "/api/drafts/")} alt={`《${draft.title}》配图`} /> : <div className="image-missing"><Image size={32} />暂无图片</div>}</div>
            <div className="review-section"><h3>核验清单</h3>{requiredChecks.map(([key, label]) => <label key={key} className="check-row"><input type="checkbox" checked={checks[key]} onChange={(event) => setChecks((current) => ({ ...current, [key]: event.target.checked }))} /><span>{label}</span><span className={checks[key] ? "status-good" : "status-amber"}>{checks[key] ? "已确认" : "待确认"}</span></label>)}</div>
            <div className="review-actions"><button className="quiet-button" onClick={() => setDraft(null)}>返回列表</button><button className="primary-button" onClick={approveDraft} disabled={busy || !verified || !draft.assets.length}><ShieldCheck size={16} />{verified ? "记录核验" : "核验未完成"}</button></div>
          </> : <div className="empty-detail"><ClipboardList size={30} /><h2>选择一条草稿</h2><p>查看正文、图片和平台回读状态。</p></div>}</section>
        </div>}
        {ready && page === "runs" && <main className="standard-page"><div className="page-heading"><div><h1>运行记录</h1><p>查看每项任务的状态、耗时和错误原因。</p></div><button className="quiet-button" onClick={() => load().catch(fail)}><RefreshCw size={16} />刷新</button></div><div className="run-table"><div className="run-table-head"><span>任务</span><span>状态</span><span>开始时间</span><span>详细信息</span></div>{runs.map((item) => <button className="run-table-row" key={item.id} onClick={async () => { try { setActiveRun(await api<Run>(`/api/runs/${item.id}`)); setRunConnectionError(""); } catch (cause) { fail(cause); } }}><strong>{item.title || item.id}</strong><span className={["completed", "success"].includes(item.status) ? "status-good" : "status-amber"}>{item.status_label || "状态待读取"}</span><span>{niceDate(item.created_at)}</span><span>{item.display_message || "查看进度详情"}</span></button>)}{!runs.length && <p className="muted empty-list">尚无运行记录。</p>}</div>{activeRun && <div className="run-detail"><h2>任务详情</h2><RunProgress run={activeRun} connectionError={runConnectionError} onRefresh={refreshRun} />{activeRun.local_post_ids?.length ? <div className="run-draft-links"><strong>本地生成草稿，仍需人工审查</strong>{activeRun.local_post_ids.map((id) => <button className="quiet-button" key={id} onClick={() => showRunDraft(id)}>查看草稿 {id.slice(0, 8)}<ChevronRight size={14} /></button>)}</div> : null}<TechnicalLog run={activeRun} /></div>}</main>}
        {ready && page === "connections" && <main className="standard-page connections-page"><div className="page-heading"><div><h1>连接与模型</h1><p>主控、写稿和生图分别选择；更改仅影响之后的新任务。</p></div></div><section className="db-line"><Database size={20} /><div><strong>PostgreSQL 与知识库</strong><p>{connections?.database.status === "ready" ? `连接正常 · 文档 ${connections.database.documents ?? 0} 条 · 已索引 ${connections.database.indexed_documents ?? 0} 条` : connections?.database.error || "未连接"}</p></div><button className="quiet-button" onClick={() => load().catch(fail)}><RefreshCw size={16} />检查连接</button></section><ModelPlatforms call={api} legacyModels={modelRows} defaultRoles={roles} onChanged={refreshModelConfiguration} /><section className="db-line"><div><strong>小红书创作者中心</strong><p>{connections?.profile_configured ? "专用 profile 已配置" : "专用 profile 未配置"}</p></div><button className="quiet-button" onClick={openProfile} disabled={busy}>打开专用浏览器</button></section></main>}
      </div>
    </div>
  );
}

export default App;
