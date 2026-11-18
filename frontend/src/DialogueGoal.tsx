import { useEffect, useRef } from "react";
import { Check, Flag, LoaderCircle, Pause, Pencil, Play, Plus, Trash2, X } from "lucide-react";

const stageLabels = { local: "本地生成", reviewed: "审查通过", saved: "平台草稿已确认", manual: "人工验收" };
const stateLabels = { active: "进行中", paused: "已暂停", blocked: "待处理", completed: "已完成" };
type GoalStage = keyof typeof stageLabels;
type GoalFields = { objective: string; stage: GoalStage; target: number; max_turns: number };
export type Goal = GoalFields & {
  id: string; state: keyof typeof stateLabels; reason: string; turns: number; counts: Record<Exclude<GoalStage, "manual">, number>;
};
export type GoalCommand = { action: "set" | "edit" | "pause" | "resume" | "clear" | "complete"; expectedVersion?: number } & Partial<GoalFields>;

export function DialogueGoal({ goal, version, ready, pending, error, onCommand }: {
  goal?: Goal | null; version: number; ready: boolean; pending: boolean; error: string; onCommand: (command: GoalCommand) => Promise<boolean>;
}) {
  const editor = useRef<HTMLDialogElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const stage = useRef<HTMLSelectElement>(null);
  const count = useRef<HTMLInputElement>(null);
  const openedVersion = useRef(version);
  const editing = useRef(false);
  const edit = () => {
    openedVersion.current = version;
    editing.current = Boolean(goal && goal.state !== "completed");
    editor.current?.querySelector("form")?.reset();
    if (stage.current && count.current) count.current.disabled = stage.current.value === "manual";
    editor.current?.showModal();
  };
  const confirmClear = () => { openedVersion.current = version; dialog.current?.showModal(); };
  useEffect(() => {
    if (stage.current && count.current) count.current.disabled = stage.current.value === "manual";
  }, [goal?.stage]);
  const done = goal && goal.stage !== "manual" ? goal.counts[goal.stage] : null;
  const showError = error && !editor.current?.open && !dialog.current?.open;
  return <section className="dialogue-goal" aria-label="会话目标">
    <div className="goal-main">
      <div className="goal-name"><Flag size={16} /><b>目标</b>
        {goal && <span className="goal-state" data-state={goal.state}>{stateLabels[goal.state]}</span>}
        {!goal && <span className="goal-unset">未设置</span>}
      </div>
      {goal && <div className="goal-objective" title={goal.objective}>{goal.objective}</div>}
      {goal && <div className="goal-metrics">
        <span>{stageLabels[goal.stage]}{done !== null && <> <b>{done}/{goal.target}</b></>}</span>
        <span>轮次 <b>{goal.turns}/{goal.max_turns}</b></span>
      </div>}
    </div>
    <div className="goal-controls" role="group" aria-label="会话目标操作">
      {(!goal || goal.state === "completed") && <button type="button" className="quiet-button" onClick={edit} disabled={!ready || pending}><Plus size={16} />设定目标</button>}
      {goal && goal.state !== "completed" && <>
        {goal.state === "active" ? <button className="icon-button" title="暂停目标自动推进" aria-label="暂停目标" disabled={pending}
          onClick={() => void onCommand({ action: "pause" })}><Pause size={16} /></button>
          : <button className="icon-button" title="恢复目标" aria-label="恢复目标" disabled={pending}
            onClick={() => void onCommand({ action: "resume" })}><Play size={16} /></button>}
        <button className="icon-button" title="编辑目标" aria-label="编辑目标" onClick={edit} disabled={!ready || pending}><Pencil size={15} /></button>
        {goal.stage === "manual" && <button className="icon-button" title="确认目标完成" aria-label="确认目标完成" disabled={!ready || pending}
          onClick={() => void onCommand({ action: "complete" })}><Check size={16} /></button>}
      </>}
      {goal && <button className="icon-button" title="清除目标" aria-label="清除目标" disabled={pending} onClick={confirmClear}><Trash2 size={16} /></button>}
    </div>
    {goal?.reason && <p className="goal-reason">{goal.reason}</p>}
    {showError && <p role="alert" className="goal-error">{error}</p>}
    <dialog ref={editor} className="goal-dialog" aria-labelledby="goal-editor-title">
      <form key={goal?.id ?? "new"} onSubmit={async event => {
        event.preventDefault();
        const fields = new FormData(event.currentTarget);
        const success = await onCommand({
          action: editing.current ? "edit" : "set", expectedVersion: openedVersion.current,
          objective: String(fields.get("objective") ?? "").trim(),
          stage: fields.get("stage") as GoalStage, target: Number(fields.get("target") ?? 1), max_turns: Number(fields.get("max_turns")),
        });
        if (success) editor.current?.close();
      }}>
        <header><h2 id="goal-editor-title">{goal?.state !== "completed" && goal ? "编辑会话目标" : "设定会话目标"}</h2>
          <button type="button" className="icon-button" aria-label="关闭目标设置" onClick={() => editor.current?.close()}><X size={18} /></button></header>
        <div className="goal-objective-field"><label htmlFor="goal-objective">目标</label>
          <textarea id="goal-objective" name="objective" rows={4} required maxLength={4000}
            defaultValue={goal?.state !== "completed" ? goal?.objective : ""} placeholder="例如：生成 3 篇不重复的本地新闻稿" /></div>
        <div className="goal-fields">
          <label>验收条件<select ref={stage} name="stage" defaultValue={goal?.stage ?? "local"}
            onChange={event => { if (count.current) count.current.disabled = event.target.value === "manual"; }}>
            {Object.entries(stageLabels).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label>
          <label>篇数<input ref={count} name="target" type="number" defaultValue={goal?.target ?? 1} min={1} max={100} required /></label>
          <label>轮次上限<input name="max_turns" type="number" defaultValue={goal?.max_turns ?? 8} min={1} max={50} required /></label>
        </div>
        {error && <p role="alert" className="goal-error">{error}</p>}
        <footer><button type="button" className="quiet-button" onClick={() => editor.current?.close()}>取消</button>
          <button type="submit" className="primary-button" disabled={pending}>{pending ? <LoaderCircle size={16} className="spin" /> : <Check size={16} />}
            {goal && goal.state !== "completed" ? "保存修改" : "开始目标"}</button></footer>
      </form>
    </dialog>
    <dialog ref={dialog} className="goal-dialog" aria-labelledby="goal-clear-title">
      <header><h2 id="goal-clear-title">清除此目标？</h2></header>
      <p>保留对话、稿件和回执。正在执行的操作不会中断。</p>
      {error && <p role="alert" className="goal-error">{error}</p>}
      <footer><button className="quiet-button" autoFocus onClick={() => dialog.current?.close()}>返回</button>
        <button className="primary-button" disabled={pending} onClick={async () => {
          if (await onCommand({ action: "clear", expectedVersion: openedVersion.current })) dialog.current?.close();
        }}>清除目标</button></footer>
    </dialog>
  </section>;
}
