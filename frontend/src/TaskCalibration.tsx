import { useEffect, useRef, useState } from "react";
import { BrainCircuit, Check, LoaderCircle, X } from "lucide-react";
import { api, type Conversation, type Plan, type PlanJob, type TaskRecognition } from "./api";

export function PlanJobs({ jobs }: { jobs: PlanJob[] }) {
  return <ol className="plan-list">{jobs.map((job, index) => <li key={`${job.kind}-${index}`}>
    <span>{String(index + 1).padStart(2, "0")}</span>
    <div><strong>{job.title}</strong><small>{job.count} 条 · 生成与审查</small>
      {!!job.keywords?.length && <p className="plan-keywords"><span>关键词</span>{job.keywords.join("、")}</p>}
      {job.topic_brief && <p className="plan-topic">{job.topic_brief}</p>}
    </div>
  </li>)}</ol>;
}

const deliveryLabel = (value?: string) => value === "generate_only" ? "仅生成本地稿" : "保存至草稿箱";
const platformLabel = (value?: string) => ({ xhs: "小红书", toutiao: "今日头条", both: "小红书＋今日头条" })[value || ""] || value;
const modeLabel = (value?: string) => value === "speed" ? "速度优先" : "速度与稳定平衡";

export function TaskCalibration({ conversation, plan, disabled, onAdopt, onPendingChange }: {
  conversation: Conversation; plan: Plan; disabled: boolean;
  onAdopt: () => Promise<void>; onPendingChange: (pending: boolean) => void;
}) {
  const [record, setRecord] = useState<TaskRecognition | null>(null);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const inFlight = useRef(false);
  const mounted = useRef(true);
  const button = useRef<HTMLButtonElement>(null);
  const comparison = useRef<HTMLHeadingElement>(null);
  const scope = useRef(`${conversation.id}:${plan.id}`);
  const activeScope = `${conversation.id}:${plan.id}`;
  scope.current = activeScope;

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; onPendingChange(false); };
  }, []);

  useEffect(() => {
    const saved = conversation.task_recognitions?.filter((item) => item.base_plan_id === plan.id).at(-1);
    setRecord(saved || null); setError("");
  }, [activeScope]);

  const pending = sending || record?.status === "running" || record?.status === "ready" || record?.status === "needs_input";
  useEffect(() => { onPendingChange(pending); }, [pending, onPendingChange]);

  useEffect(() => {
    if (record?.status !== "running") return;
    let disposed = false;
    let timer: number;
    const route = `/api/conversations/${conversation.id}/task-recognitions/${record.id}`;
    const poll = async () => {
      try {
        const result = await api<TaskRecognition>(route);
        if (disposed) return;
        setRecord(result); setError("");
        if (result.status !== "running") return;
      } catch (cause) {
        if (!disposed) setError(cause instanceof Error ? cause.message : String(cause));
      }
      if (!disposed) timer = window.setTimeout(poll, 1000);
    };
    timer = window.setTimeout(poll, 500);
    return () => { disposed = true; window.clearTimeout(timer); };
  }, [record?.id, record?.status, conversation.id]);

  useEffect(() => {
    if (record?.status === "ready" || record?.status === "needs_input") comparison.current?.focus();
  }, [record?.status]);

  function sourceMessageId() {
    if (plan.source_message_id) return plan.source_message_id;
    const reply = conversation.messages.findIndex((message) => (message as typeof message & { plan_id?: string }).plan_id === plan.id);
    return reply > 0 && conversation.messages[reply - 1].role === "user" ? conversation.messages[reply - 1].id : "";
  }

  async function calibrate() {
    if (inFlight.current || disabled || pending) return;
    const source = sourceMessageId();
    if (!source) { setError("没有找到该计划的原始消息，请重新发送完整任务指令。"); return; }
    inFlight.current = true; setSending(true); setError("");
    const selected = scope.current;
    try {
      const result = await api<TaskRecognition>(`/api/conversations/${conversation.id}/task-recognitions`, "POST", {
        source_message_id: source, base_plan_id: plan.id, base_plan_version: plan.version,
      }, crypto.randomUUID());
      if (mounted.current && selected === scope.current) setRecord(result);
    } catch (cause) {
      if (mounted.current && selected === scope.current) setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      inFlight.current = false;
      if (mounted.current && selected === scope.current) setSending(false);
    }
  }

  async function choose(adopt: boolean) {
    if (!record || inFlight.current) return;
    inFlight.current = true; setSending(true); setError("");
    const selected = scope.current;
    try {
      const route = `/api/conversations/${conversation.id}/task-recognitions/${record.id}`;
      await api(`${route}/${adopt ? "adopt" : "discard"}`, "POST", adopt ? { base_plan_version: plan.version } : {});
      if (mounted.current && selected === scope.current) {
        setRecord((previous) => previous ? { ...previous, status: adopt ? "adopted" : "discarded" } : null);
        await onAdopt();
        if (!adopt) button.current?.focus();
      }
    } catch (cause) {
      if (mounted.current && selected === scope.current) setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      inFlight.current = false;
      if (mounted.current && selected === scope.current) setSending(false);
    }
  }

  const showCandidate = record?.candidate && ["ready", "needs_input"].includes(record.status);
  return <section className="task-calibration" aria-label="任务识别校准">
    <div className="calibration-toolbar"><small>识别来源：{plan.recognition_source === "llm" ? "大模型校准" : "本地规则"}</small>
      <button ref={button} className="quiet-button" disabled={disabled || pending} onClick={calibrate} title={disabled ? "执行中的任务不能校准，请等待完成" : "使用智能体主控模型重新识别这条指令"}>
        {sending || record?.status === "running" ? <LoaderCircle size={15} className="spin" /> : <BrainCircuit size={15} />}
        {sending || record?.status === "running" ? "校准中" : "大模型校准"}
      </button>
    </div>
    <div aria-live="polite">{record?.status === "running" && <p className="calibration-status">正在使用 {record.provider} · {record.model} 理解任务</p>}
      {(error || record?.error) && <p className="calibration-error" role="alert">{error || record?.error}</p>}
    </div>
    {showCandidate && <div className="calibration-comparison">
      <h3 ref={comparison} tabIndex={-1}>校准候选 <small>{record.elapsed_seconds?.toFixed(1)} 秒</small></h3>
      <p className="calibration-summary">{record.candidate!.assistant_summary}</p>
      <PlanJobs jobs={record.candidate!.jobs} />
      <table className="calibration-options"><caption>计划选项对比</caption><thead><tr><th>选项</th><th>当前</th><th>校准</th></tr></thead>
        <tbody>{[
          ["交付", deliveryLabel(plan.delivery), deliveryLabel(record.candidate!.delivery)],
          ["平台", platformLabel(plan.platform), platformLabel(record.candidate!.platform)],
          ["速度", modeLabel(plan.performance_mode), modeLabel(record.candidate!.performance_mode)],
          ["图片评分", plan.image_score_required === false ? "仅供参考" : "硬门槛", record.candidate!.image_score_required === false ? "仅供参考" : "硬门槛"],
          ...(["agent", "writer", "image"] as const).map((role) => [
            { agent: "主控模型", writer: "写稿模型", image: "生图模型" }[role],
            plan.model_roles?.[role] || "当前配置", record.candidate!.model_roles?.[role] || "当前配置",
          ]),
        ].map(([label, before, after]) => <tr key={label}><th>{label}</th><td>{before}</td><td className={before !== after ? "calibration-changed" : ""}>{after}</td></tr>)}</tbody>
      </table>
      {!!record.candidate!.unresolved_requirements?.length && <ul className="calibration-issues">{record.candidate!.unresolved_requirements.map((item, index) => <li key={index}>{item}</li>)}</ul>}
      <div className="calibration-actions"><button className="quiet-button" disabled={sending} onClick={() => choose(false)}><X size={15} />保留当前计划</button>
        <button className="quiet-button" disabled={sending || !record.candidate!.executable} onClick={() => choose(true)}><Check size={15} />采用校准计划</button></div>
    </div>}
  </section>;
}
