import { useEffect, useState } from "react";
import { Save } from "lucide-react";
import { api, type Model, type Plan } from "./api";
import { RolePicker } from "./ModelPlatforms";

export function PlanModels({ conversationId, plan, models, disabled, onChanged }: {
  conversationId: string; plan: Plan; models: Model[]; disabled: boolean;
  onChanged: (plan: Plan) => void;
}) {
  const [roles, setRoles] = useState(plan.model_roles || {});
  const [error, setError] = useState(""), [saving, setSaving] = useState(false);
  useEffect(() => { setRoles(plan.model_roles || {}); setError(""); }, [plan.id, plan.version]);
  const changed = ["agent", "writer", "image"].some(role => (roles[role] || "") !== (plan.model_roles?.[role] || ""));
  return <details className="plan-models"><summary>本次模型</summary>
    {[["agent", "本次主控模型"], ["writer", "本次写稿模型"], ["image", "本次生图模型"]].map(([role, label]) =>
      <div className="mp-field" key={role}><span>{label}</span><RolePicker label={label} value={roles[role] || ""}
        disabled={disabled || saving} choices={models.filter(m => m.kind === (role === "image" ? "image" : "llm")).map(m => ({
          id: m.id, label: `${m.provider_name || m.provider} · ${m.model}`, model: m.model,
          disabledReason: m.role_reasons?.[role] ?? (m.selectable ? "" : m.disabled_reason || "模型不可用"),
        }))} onChange={ref => setRoles(previous => ({ ...previous, [role]: ref }))} /></div>)}
    {error && <p className="mp-error" role="alert">{error}</p>}
    <button className="quiet-button" disabled={disabled || saving || !changed} onClick={async () => {
      setSaving(true); setError("");
      try { const result = await api<{ plan: Plan }>(`/api/conversations/${conversationId}/plans/${plan.id}/models`, "PUT", {
        version: plan.version, model_roles: { agent: roles.agent || "", writer: roles.writer || "", image: roles.image || "" },
      }); onChanged(result.plan); } catch (e) { setError(String(e)); } finally { setSaving(false); }
    }}><Save size={15} />应用于本次计划</button>
  </details>;
}
