export type PlanJob = { job_id?: string; kind: string; title: string; count: number; prompt?: string; keywords?: string[]; keyword_mode?: "default" | "filter" | "preference";
  search_keywords?: string[]; topic_preferences?: string[]; topic_brief?: string; topic_brief_strength?: "preference" | "requirement" | null;
  content_constraints?: Record<string, unknown>[]; evaluation_viewpoint?: string; legacy_extra_requirements?: string };
export type Plan = {
  id: string;
  version: number;
  status: string;
  job_id?: string;
  resume_job_id?: string;
  agent_run_id?: string;
  executable: boolean;
  jobs: PlanJob[];
  delivery: string;
  platform: string;
  assistant_summary: string;
  source_message_id?: string;
  recognition_source?: "rules" | "llm";
  performance_mode?: string;
  image_score_required?: boolean;
  unresolved_requirements?: string[];
  model_roles?: Record<string, string>;
  semantic_hash?: string;
  configuration_fingerprint?: string;
  plan_schema_version?: string;
  last_editor?: string;
  parent_plan_id?: string;
  execution_request_id?: string;
  field_origins?: Record<string, string>;
  field_errors?: { id: string; code: string; field: string; message: string; content_hash: string }[];
  warnings?: { code: string; field: string; message: string }[];
  requirements?: Record<string, unknown>[];
  field_suggestions?: { field_path: string; value: unknown; reason: string; base_value_hash: string }[];
  clarifications?: unknown[];
};
export type Message = { id: string; role: string; content: string; created_at: number };
export type TaskRecognition = {
  id: string; source_message_id: string; base_plan_id: string; base_plan_version: number;
  status: "running" | "ready" | "needs_input" | "failed" | "interrupted" | "adopted" | "discarded" | "stale";
  model: string; provider: string; started_at: number; ended_at?: number; elapsed_seconds?: number;
  error: string; candidate?: Plan | null;
  changes: { label: string; before: unknown; after: unknown }[];
};
export type Conversation = { id: string; title: string; messages: Message[]; plans: Plan[]; runs: string[]; status: string; conversation_revision?: number; task_recognitions?: TaskRecognition[] };
export type Activity = {
  status: string; status_label: string; active: boolean; headline: string; summary: string;
  stage: string; current_job: string; started_at: number | null; ended_at: number | null;
  elapsed_seconds: number; last_update: number | null; requested: number | null;
  counts: { generated: number | null; reviewed: number | null; saved: number; verified: number; local: number; retained?: number };
  jobs: { kind: string; title: string; requested: number; generated: number | null; reviewed: number | null; saved: number; verified: number; local: number; retained?: number; status: string }[];
  timeline: { id: string; at: number; text: string; status: string; stage: string }[];
  issues: { message: string; action: string }[];
};
export type Run = { id: string; model_snapshots?: Record<string, { upstream_model_id: string; connection_name: string; adapter: string }>; agent_run_id?: string; resume_of?: string; title?: string; status: string; status_label?: string; display_message?: string; message?: string; stage?: string; created_at?: number; started_at?: number; ended_at?: number; events?: { id: number; at: number; message: string }[]; post_rows?: { id: string; title?: string; images?: number; status?: string; readback?: string }[]; activity?: Activity; local_post_ids?: string[]; retained_post_ids?: string[] };
export type DraftSummary = {
  post_id: string;
  title: string;
  status: string;
  uploaded: boolean;
  updated_at: string;
  body_preview: string;
  asset_count: number;
};
export type Draft = {
  id: string;
  title: string;
  body: string;
  status: string;
  updated_at: string;
  assets: { url: string; name: string }[];
  readback: string;
  platform: Record<string, unknown>;
  steps: { name: string; status: string }[];
  evidence: { title: string; source: string; published_at: string; url: string }[];
};
export type Model = { id: string; model: string; provider: string; provider_name?: string; kind: string; selectable: boolean; disabled_reason?: string; role_reasons?: Record<string, string> };
export type Connections = {
  database: { status: string; documents?: number; indexed_documents?: number; error?: string };
  providers: { bindings: Record<string, string>; connections: { id: string; label: string; configured: boolean }[] };
  models: { rows: Model[] };
  profile_configured: boolean;
  profile_login: string;
};

export async function api<T>(path: string, method = "GET", body?: unknown, idempotencyKey?: string): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Workbench": "1", ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail && typeof data.detail === "object" ? data.detail : data;
    const message = detail.message || (typeof data.error === "string" ? data.error : data.error?.message)
      || (typeof data.detail === "string" ? data.detail : undefined) || `HTTP ${response.status}`;
    throw Object.assign(new Error(message), {
      code: detail.code, resource_id: detail.resource_id, next_action: detail.next_action,
      retryable: detail.retryable, revision: detail.revision, status: response.status,
    });
  }
  return data as T;
}

export const startSession = () => api<{ status: string }>("/api/session", "POST");

export function requestTaskCalibration(conversation: Conversation, plan: Plan) {
  const reply = conversation.messages.findIndex(message => (message as Message & { plan_id?: string }).plan_id === plan.id);
  const source = plan.source_message_id || (reply > 0 && conversation.messages[reply - 1].role === 'user'
    ? conversation.messages[reply - 1].id : '');
  if (!source) return Promise.reject(new Error('没有找到该计划的原始消息，请核对任务指令。'));
  return api<TaskRecognition>(`/api/conversations/${conversation.id}/task-recognitions`, 'POST', {
    source_message_id: source, base_plan_id: plan.id, base_plan_version: plan.version,
  }, crypto.randomUUID());
}
