import { useCallback, useEffect, useRef, useState } from "react";
import { Activity, Check, CheckCircle2, ChevronRight, CircleAlert, ClipboardList, Database, FileText, Image, LoaderCircle, Menu, MessageSquare, Play, RefreshCw, Send, Settings2, ShieldCheck, X } from "lucide-react";
import {
  api, startSession, type Connections, type Conversation, type Draft, type DraftSummary,
  type Model, type Plan, type Run,
} from "./api";
import { RunProgress, TechnicalLog, activeStatuses } from "./RunProgress";
import { WoolGallery } from "./WoolGallery";
import { PlanJobs, TaskCalibration } from "./TaskCalibration";
import { SourceDiagnostics } from "./SourceDiagnostics";
import { ModelPlatforms } from "./ModelPlatforms";
import { PlanModels } from "./PlanModels";
import "./source-diagnostics.css";

type Page = "chat" | "drafts" | "runs" | "connections" | "wool" | "sources";
const pages: { id: Page; label: string; icon: typeof MessageSquare }[] = [
  { id: "chat", label: "对话任务", icon: MessageSquare },
  { id: "drafts", label: "草稿审查", icon: FileText },
  { id: "runs", label: "运行记录", icon: ClipboardList },
  { id: "connections", label: "连接与模型", icon: Settings2 },
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
  const [page, setPage] = useState<Page>("chat");
  const [mobileNav, setMobileNav] = useState(false);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [prompt, setPrompt] = useState("");
  const [plan, setPlan] = useState<Plan | null>(null);
  const [calibrationPending, setCalibrationPending] = useState(false);
  const [runs, setRuns] = useState<Run[]>([]);
  const [activeRun, setActiveRun] = useState<Run | null>(null);
  const [runConnectionError, setRunConnectionError] = useState("");
  const [drafts, setDrafts] = useState<DraftSummary[]>([]);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [checks, setChecks] = useState<Record<string, boolean>>({ source: false, date: false, body: false, image: false });
  const [connections, setConnections] = useState<Connections | null>(null);
  const [roles, setRoles] = useState<Record<string, string>>({ agent: "", writer: "", image: "" });
  const selectionVersion = useRef(0);

  const fail = (reason: unknown) => { setError(reason instanceof Error ? reason.message : String(reason)); setNotice(""); };
  const load = useCallback(async () => {
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
  }, []);

  useEffect(() => {
    startSession().then(async () => {
      await load();
      const saved = localStorage.getItem("agent-conversation");
      if (saved) await openConversation(saved);
    }).catch(fail);
  }, [load]);
  useEffect(() => {
    if (!activeRun || !activeStatuses.has(activeRun.status)) return;
    const runId = activeRun.id;
    const conversationId = conversation?.id;
    let disposed = false;
    let timer: number;
    const poll = async () => {
      try {
        const current = await api<Run>(`/api/runs/${runId}`);
        if (disposed) return;
        setActiveRun(current); setRunConnectionError("");
        if (!activeStatuses.has(current.status)) {
          await load();
          if (disposed) return;
          if (conversationId) {
            const updated = await api<Conversation>(`/api/conversations/${conversationId}`);
            if (!disposed) setConversation(updated);
          }
          return;
        }
      } catch (cause) {
        if (!disposed) setRunConnectionError(cause instanceof Error ? cause.message : String(cause));
      }
      if (!disposed) timer = window.setTimeout(poll, 2000);
    };
    timer = window.setTimeout(poll, 2000);
    return () => { disposed = true; window.clearTimeout(timer); };
  }, [activeRun?.id, activeRun?.status, conversation?.id, load]);

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
    setBusy(true); setError("");
    try {
      const created = await api<Conversation>("/api/conversations", "POST", { title: "新对话" });
      setConversation(created); setPlan(null); setActiveRun(null); setRunConnectionError("");
      localStorage.setItem("agent-conversation", created.id); await load();
    } catch (cause) { fail(cause); } finally { setBusy(false); }
  }

  async function openConversation(id: string) {
    const version = ++selectionVersion.current;
    setError("");
    try {
      const loaded = await api<Conversation>(`/api/conversations/${id}`);
      if (version !== selectionVersion.current) return;
      const latest = loaded.runs.at(-1);
      const run = latest ? await api<Run>(`/api/runs/${latest}`) : null;
      if (version !== selectionVersion.current) return;
      setConversation(loaded); setPlan(loaded.plans.at(-1) || null);
      localStorage.setItem("agent-conversation", loaded.id); setRunConnectionError("");
      setActiveRun(run);
    } catch (cause) { if (version === selectionVersion.current) fail(cause); }
  }

  async function sendMessage(text?: string) {
    const content = (text ?? prompt).trim();
    if (!content) return;
    if (activeRun && !activeRun.activity && content.length <= 160
      && !/生成|重做|重新|上传|发布|删除|停止|取消|恢复|执行|添加|修改/.test(content)
      && /进度|状态|到哪|哪一步|卡在|还要多久|用了多久|完成了|完成没|成功了|结束了|怎么样了/.test(content)) {
      await refreshRun(); if (!text) setPrompt(""); return;
    }
    setBusy(true); setError("");
    try {
      let current = conversation;
      if (!current) current = await api<Conversation>("/api/conversations", "POST", { title: content.slice(0, 50) });
      await api<{ plan: Plan | null }>(`/api/conversations/${current.id}/messages`, "POST", { content });
      if (!text) setPrompt(""); await openConversation(current.id); await load();
    } catch (cause) { fail(cause); } finally { setBusy(false); }
  }

  async function askProgress() {
    if (activeRun && !activeRun.activity) { await refreshRun(); return; }
    await sendMessage("查看当前任务进度");
  }

  async function confirmPlan() {
    if (!conversation || !plan || !plan.executable || calibrationPending) return;
    setBusy(true); setError("");
    try {
      const result = await api<Run>(`/api/plans/${plan.id}/confirm`, "POST", {
        conversation_id: conversation.id, version: plan.version,
      });
      setActiveRun(result); setNotice("任务已提交，当前进度将在对话中持续更新。");
      await load(); await openConversation(conversation.id);
    } catch (cause) { fail(cause); } finally { setBusy(false); }
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

  async function openProfile() {
    setBusy(true); setError("");
    try {
      const result = await api<{ login: string }>("/api/profile/open", "POST");
      setNotice(result.login);
    } catch (cause) { fail(cause); } finally { setBusy(false); }
  }

  const currentPlan = plan && conversation?.plans.find((item) => item.id === plan.id);
  const planSubmitted = Boolean(currentPlan?.job_id);
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
            <button key={id} className={`nav-item ${page === id ? "active" : ""}`} onClick={() => { setPage(id); setMobileNav(false); }}>
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
        {!ready && <div className="startup"><LoaderCircle size={18} className="spin" />正在连接本地服务</div>}
        {error && <div className="banner error" role="alert"><CircleAlert size={17} /><span>{error}</span><button className="icon-button" title="关闭提示" aria-label="关闭提示" onClick={() => setError("")}><X size={16} /></button></div>}
        {notice && <div className="banner notice" role="status"><CheckCircle2 size={17} /><span>{notice}</span><button className="icon-button" title="关闭提示" aria-label="关闭提示" onClick={() => setNotice("")}><X size={16} /></button></div>}
        {ready && page === "chat" && <div className="chat-layout">
          <section className="chat-main">
            <div className="page-heading"><div><h1>任务对话</h1><p>说明栏目和数量，确认计划后执行。</p></div><button className="quiet-button" onClick={createConversation} disabled={busy}><MessageSquare size={16} />新对话</button></div>
            <div className="context-line"><span className="status-dot good" />已发布数据和知识库状态：{connections?.database.status === "ready" ? "数据库可用" : "待检查"}</div>
            <div className="conversation-selector"><label htmlFor="conversation-select">当前对话</label><select id="conversation-select" value={conversation?.id || ""} onChange={(event) => { if (event.target.value) openConversation(event.target.value); else { setConversation(null); setPlan(null); setActiveRun(null); localStorage.removeItem("agent-conversation"); } }}><option value="">新任务</option>{conversations.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select></div>
            <div className="messages">{conversation?.messages.length ? conversation.messages.map((message) => <div key={message.id} className={`message ${message.role}`}><small>{message.role === "user" ? "你" : "智能体"} · {niceDate(message.created_at)}</small><p>{message.content}</p></div>) : <div className="empty-chat"><MessageSquare size={22} /><strong>开始一项采编任务</strong><span>输入自然语言要求，智能体先展示可确认的执行计划。</span></div>}
              {activeRun && conversation?.runs.includes(activeRun.id) && <div className="message assistant live-message"><small>智能体 · 运行播报</small><RunProgress run={activeRun} connectionError={runConnectionError} onRefresh={refreshRun} /><TechnicalLog run={activeRun} /></div>}
            </div>
            <div className="composer"><label htmlFor="prompt">任务要求</label><textarea id="prompt" rows={3} value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="例如：生成5条每日新闻，关键词：伊朗、关税、芯片；保存到小红书草稿箱" onKeyDown={(event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); sendMessage(); } }} /><div className="composer-actions">{activeRun ? <button className="text-button" disabled={busy} onClick={askProgress}><RefreshCw size={14} />询问进度</button> : <span>新任务生成前确认计划</span>}<button className="primary-button" disabled={busy || !prompt.trim()} onClick={() => sendMessage()}><Send size={16} />发送</button></div></div>
          </section>
          <aside className="plan-pane">
            <div className="pane-heading"><h2>本次计划</h2><span className="status-amber">{plan ? planRunning ? "执行中" : planSubmitted ? "已执行" : plan.executable ? "待确认" : "需补充" : "等待输入"}</span></div>
            {plan ? <>
              {conversation && <PlanModels conversationId={conversation.id} plan={plan} models={modelRows}
                disabled={busy || planSubmitted || calibrationPending} onChanged={updated => {
                  setPlan(updated); setConversation(current => current ? { ...current, plans: current.plans.map(p => p.id === updated.id ? updated : p) } : current);
                }} />}
              {conversation && <TaskCalibration key={`${conversation.id}:${plan.id}`} conversation={conversation} plan={plan}
                disabled={busy || planSubmitted || Boolean(activeRun && activeStatuses.has(activeRun.status))}
                onPendingChange={setCalibrationPending} onAdopt={() => openConversation(conversation.id)} />}
              <h3 className="current-plan-heading">当前计划</h3>
              <PlanJobs jobs={plan.jobs} />
              {!!plan.unresolved_requirements?.length && <ul className="calibration-issues">{plan.unresolved_requirements.map((item, index) => <li key={index}>{item}</li>)}</ul>}
              <div className="plan-boundary"><h3>执行边界</h3><p>交付：{plan.delivery === "generate_only" ? "仅生成本地稿" : "保存至草稿箱"}</p><p>平台：{plan.platform === "xhs" ? "小红书" : plan.platform}</p><p>运行模式：{plan.performance_mode === "speed" ? "速度优先" : "速度与稳定平衡"}</p><p>公开发布：本次不执行</p></div>
              <button className="primary-button full" disabled={busy || calibrationPending || !plan.executable || planSubmitted} onClick={confirmPlan}><Play size={16} />{planRunning ? "正在执行" : planSubmitted ? "计划已执行" : "确认并执行"}</button>
            </> : <p className="muted">输入任务后，这里会显示栏目、数量和交付方式。</p>}
            {activeRun && <div className="run-inline"><strong>最近一次运行</strong><RunProgress run={activeRun} compact connectionError={runConnectionError} /><button className="text-button" onClick={() => setPage("runs")}>查看运行记录 <ChevronRight size={14} /></button></div>}
          </aside>
        </div>}
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
        {ready && page === "connections" && <main className="standard-page connections-page"><div className="page-heading"><div><h1>连接与模型</h1><p>主控、写稿和生图分别选择；更改仅影响之后的新任务。</p></div></div><section className="db-line"><Database size={20} /><div><strong>PostgreSQL 与知识库</strong><p>{connections?.database.status === "ready" ? `连接正常 · 文档 ${connections.database.documents ?? 0} 条 · 已索引 ${connections.database.indexed_documents ?? 0} 条` : connections?.database.error || "未连接"}</p></div><button className="quiet-button" onClick={() => load().catch(fail)}><RefreshCw size={16} />检查连接</button></section><ModelPlatforms call={api} legacyModels={modelRows} onChanged={load} /><section className="db-line"><div><strong>小红书创作者中心</strong><p>{connections?.profile_configured ? "专用 profile 已配置" : "专用 profile 未配置"}</p></div><button className="quiet-button" onClick={openProfile} disabled={busy}>打开专用浏览器</button></section></main>}
      </div>
    </div>
  );
}

export default App;
