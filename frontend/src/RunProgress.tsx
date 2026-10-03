import { useEffect, useState } from "react";
import { CheckCircle2, CircleAlert, Clock3, LoaderCircle, RefreshCw } from "lucide-react";
import type { Run } from "./api";

export const activeStatuses = new Set(["queued", "running", "waiting_user", "stopping"]);
const jobLabels: Record<string, string> = { pending: "待开始", running: "进行中", completed: "已交付", failed: "未完成", partial: "部分完成", not_completed: "未完成" };
const statusLabels: Record<string, string> = { queued: "等待启动", running: "执行中", waiting_user: "等待处理", stopping: "正在停止", completed: "执行完成", success: "执行完成", partial_success: "部分完成", failed: "执行失败", interrupted: "运行中断", cancelled: "已取消" };
const stageLabels: Record<string, string> = { sync_context: "同步账号与知识库", plan: "规划任务", generate: "生成内容与配图", review: "审查内容与配图", upload_batch: "保存平台草稿", upload: "保存平台草稿", recover: "处理异常", finish: "汇总结果" };

// Rolling UI upgrades must not require terminating an older worker mid-task.
function legacyActivity(run: Run) {
  const saved = new Set((run.post_rows || []).filter((p) => p.status === "saved_as_draft" || p.readback === "verified").map((p) => p.id));
  const verified = new Set((run.post_rows || []).filter((p) => p.readback === "verified").map((p) => p.id));
  const active = activeStatuses.has(run.status);
  const stage = stageLabels[run.stage || ""] || run.stage || "等待运行记录";
  return {
    active, status_label: statusLabels[run.status] || "状态待确认", headline: active ? stage : statusLabels[run.status] || "状态待确认",
    started_at: run.started_at || run.created_at || null,
    elapsed_seconds: run.ended_at && run.started_at ? run.ended_at - run.started_at : 0,
    last_update: run.events?.at(-1)?.at || run.started_at || null,
    counts: { generated: null, reviewed: null, saved: saved.size, verified: verified.size, local: 0 },
    jobs: [], timeline: [], issues: [], summary: `平台保存记录 ${saved.size} 条，回读确认 ${verified.size} 条；详细栏目数量尚未返回。`,
  };
}

function duration(seconds: number) {
  const value = Math.max(0, Math.floor(seconds));
  return `${Math.floor(value / 3600) ? `${Math.floor(value / 3600)} 小时 ` : ""}${Math.floor(value / 60) % 60} 分 ${value % 60} 秒`;
}

export function RunProgress({ run, compact = false, connectionError = "", onRefresh }: {
  run: Run; compact?: boolean; connectionError?: string; onRefresh?: () => void;
}) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  const activity = run.activity ?? legacyActivity(run);
  useEffect(() => {
    setNow(Date.now() / 1000);
    if (!activity?.active) return;
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, [run.id, activity?.active]);
  const elapsed = activity.active && activity.started_at ? now - activity.started_at : activity.elapsed_seconds;
  const age = activity.last_update ? Math.max(0, now - activity.last_update) : null;
  const problem = ["failed", "interrupted", "partial_success", "partial", "paused"].includes(run.status);
  const Icon = activity.active ? LoaderCircle : problem ? CircleAlert : CheckCircle2;
  return <section className={`run-progress ${compact ? "compact" : ""}`} aria-label="任务实时进度">
    <div className="progress-heading"><Icon size={18} className={activity.active ? "spin" : ""} /><strong>{activity.status_label}</strong>
      {onRefresh && <button className="icon-button" title="刷新进度" aria-label="刷新进度" onClick={onRefresh}><RefreshCw size={15} /></button>}
    </div>
    <p className="progress-current" aria-live="polite">{activity.headline}</p>
    <div className="progress-time"><Clock3 size={13} /><span>已用时 {duration(elapsed)}</span></div>
    {!compact && <>
      <div className="progress-counts">{([
        ["产出记录", activity.counts.generated], ["通过审查", activity.counts.reviewed],
        ["平台保存", activity.counts.saved], ["回读确认", activity.counts.verified],
      ] as const).map(([label, count]) => <div key={label}><span>{label}</span><strong>{count ?? "待记录"}{count !== null && <small> 条</small>}</strong></div>)}</div>
      {!!activity.jobs.length && <ul className="progress-jobs">{activity.jobs.map((job, index) => <li key={`${job.kind}-${index}`}>
        <div><strong>{job.title}</strong><span className={job.status === "completed" ? "status-good" : "muted"}>{jobLabels[job.status]}</span></div>
        <p>目标 {job.requested} 条 · 通过审查 {job.reviewed ?? "待记录"} · 平台保存 {job.saved} · 回读确认 {job.verified}{job.local > 0 && ` · 仅本地 ${job.local}`}{(job.retained ?? 0) > 0 && ` · 保留待上传 ${job.retained}`}</p>
      </li>)}</ul>}
      {!!run.retained_post_ids?.length && <details className="progress-timeline" open><summary>已审查并保留的稿件</summary>{run.retained_post_ids.map((id) => {
        const post = run.post_rows?.find((item) => item.id === id);
        return <p key={id}><span>{post?.title || id.slice(0, 8)}</span>{post?.images !== undefined && <small> · 图片 {post.images} 张</small>}</p>;
      })}</details>}
    </>}
    <p className="progress-summary">{activity.summary}</p>
    {connectionError ? <div className="progress-warning" role="status"><strong>进度连接中断，正在重连</strong><p>{connectionError}；下方保留的是最后一次收到的状态。</p></div>
      : activity.active && age !== null && <p className={`progress-freshness ${age >= 60 ? "waiting" : ""}`}>{age < 5 ? "刚收到运行动态" : `最近动态：${duration(age)}前`}{age >= 60 && "。当前步骤尚未返回新事件，不代表任务已失败。"}</p>}
    {!!activity.issues.length && <div className="progress-issues">{activity.issues.map((issue) => <div key={issue.message}><strong>{issue.message}</strong><p>下一步：{issue.action}</p></div>)}</div>}
    {!compact && !!activity.timeline.length && <details className="progress-timeline" open><summary>最近动态</summary><ol>{activity.timeline.slice(-8).map((event) => <li key={event.id}>
      <time>{event.at ? new Date(event.at * 1000).toLocaleTimeString("zh-CN", { hour12: false }) : ""}</time>
      <span className={event.status === "failed" ? "event-error" : ""}>{event.text}</span>
    </li>)}</ol></details>}
  </section>;
}

export function TechnicalLog({ run }: { run: Run }) {
  return <details className="technical-log"><summary>技术日志</summary><pre>{JSON.stringify(run.events || [], null, 2)}</pre></details>;
}
