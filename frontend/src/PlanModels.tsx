import { useEffect, useState } from "react";
import { Save } from "lucide-react";
import { api, type Model, type Plan } from "./api";
import { RolePicker } from "./ModelPlatforms";

export function PlanModels({ conversationId, plan, models, defaultRoles, disabled, onChanged, onDefaultsChanged }: {
  conversationId: string; plan: Plan; models: Model[]; defaultRoles: Record<string, string>; disabled: boolean;
  onChanged: (plan: Plan) => void;
  onDefaultsChanged: () => Promise<void>;
}) {
  const [roles, setRoles] = useState(plan.model_roles || {});
  const [error, setError] = useState(""), [notice, setNotice] = useState(""), [saving, setSaving] = useState(false);
  useEffect(() => { setRoles(plan.model_roles || {}); setError(""); setNotice(""); }, [plan.id, plan.version]);
  const changed = ["agent", "writer", "image"].some(role => (roles[role] || "") !== (plan.model_roles?.[role] || ""));
  const defaultsChanged = ["agent", "writer", "image"].some(role => (roles[role] || "") !== (defaultRoles[role] || ""));
  return <details className="plan-models"><summary>本次模型</summary>
    {[["agent", "本次主控模型"], ["writer", "本次写稿模型"], ["image", "本次生图模型"]].map(([role, label]) =>
      <div className="mp-field" key={role}><span>{label}</span><RolePicker label={label} value={roles[role] || ""}
        disabled={disabled || saving} choices={models.filter(m => m.kind === (role === "image" ? "image" : "llm")).map(m => ({
          id: m.id, label: `${m.provider_name || m.provider} · ${m.model}`, model: m.model,
          disabledReason: m.role_reasons?.[role] ?? (m.selectable ? "" : m.disabled_reason || "模型不可用"),
        }))} onChange={ref => setRoles(previous => ({ ...previous, [role]: ref }))} /></div>)}
    {error && <p className="mp-error" role="alert">{error}</p>}
    {notice && <p className="mp-notice" role="status">{notice}</p>}
    <div className="mp-actions"><button className="quiet-button" disabled={disabled || saving || !changed} onClick={async () => {
      setSaving(true); setError(""); setNotice("");
      try { const result = await api<{ plan: Plan }>(`/api/conversations/${conversationId}/plans/${plan.id}/models`, "PUT", {
        version: plan.version, model_roles: { agent: roles.agent || "", writer: roles.writer || "", image: roles.image || "" },
      }); onChanged(result.plan); } catch (e) { setError(String(e)); } finally { setSaving(false); }
    }}><Save size={15} />应用于本次计划</button>
    <button className="quiet-button" disabled={disabled || saving || !defaultsChanged}
      title="保存后，新任务及重启后沿用这些模型；不修改当前计划或运行中的任务" onClick={async () => {
        setSaving(true); setError(""); setNotice("");
        try {
          const state = await api<{ revision: number }>("/api/model-platforms/roles");
          await api("/api/model-platforms/roles", "PUT", {
            expected_revision: state.revision,
            agent: roles.agent || "", writer: roles.writer || "", image: roles.image || "",
          });
          await onDefaultsChanged(); setNotice("默认模型已保存");
        } catch (e) { setError(String(e)); } finally { setSaving(false); }
      }}><Save size={15} />保存为默认模型</button></div>
  </details>;
}
