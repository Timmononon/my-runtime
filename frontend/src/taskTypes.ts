import type { Pending } from "./types";
import type { GithubTaskBinding } from "./githubTypes";

export type TaskState =
  | "queued"
  | "working"
  | "waiting"
  | "completed"
  | "failed"
  | "cancelled"
  | "needs_reconciliation";
export type TaskMode = "off" | "observe" | "auto";
export interface TaskRule {
  id: string;
  enabled: boolean;
  contains: string[];
}
export interface TaskSettings {
  revision: number;
  mode: TaskMode;
  rules: TaskRule[];
  max_workers?: number;
}
export interface TaskWorker {
  id: string;
  title: string;
  goal: string;
  agent_id: string;
  role: "worker" | "reader";
  state: string;
  session_id: string | null;
  run_id: string | null;
  cwd: string;
  answer: string;
  error: string | null;
  pending_requests: Pending[];
}
export interface TaskTicket {
  id: string;
  source_session_id: string;
  parent_run_id: string | null;
  title: string;
  goal: string;
  state: TaskState;
  revision: number;
  created_at: number;
  updated_at: number;
  summary: string;
  attention: string | null;
  policy_revision: number;
  workers: TaskWorker[];
  can_retry?: boolean;
  can_cancel?: boolean;
  can_add_input?: boolean;
  github?: GithubTaskBinding | null;
}
export interface TaskOverview {
  counts: {
    total: number;
    active: number;
    attention: number;
    completed: number;
  };
  tickets: TaskTicket[];
}
export interface TaskCandidate {
  id: string;
  decision: "ignored" | "observed" | "accepted";
  reason: string;
  ticket_id?: string | null;
  policy_revision: number;
  text?: string;
  created_at?: number;
}
export interface WorkerDraft {
  title: string;
  goal: string;
  agent_id: string;
  role: "worker" | "reader";
}

const taskLabels: Record<string, string> = {
  queued: "排队中",
  working: "执行中",
  running: "执行中",
  waiting: "等待处理",
  waiting_approval: "等待批准",
  waiting_input: "等待回答",
  completed: "已完成",
  failed: "执行失败",
  cancelled: "已取消",
  cancelling: "正在取消",
  needs_reconciliation: "结果待核验",
  timed_out: "已超时",
  budget_exhausted: "预算已用尽",
};
export const taskLabel = (state: string) => taskLabels[state] || "状态待确认";
export function taskNeedsAttention(ticket: TaskTicket) {
  return (
    !!ticket.attention ||
    ["waiting", "failed", "needs_reconciliation"].includes(ticket.state) ||
    ticket.workers.some((worker) => worker.pending_requests?.length > 0)
  );
}
export function orderTickets(tickets: TaskTicket[]) {
  return [...tickets].sort(
    (a, b) =>
      Number(taskNeedsAttention(b)) - Number(taskNeedsAttention(a)) ||
      b.updated_at - a.updated_at ||
      a.id.localeCompare(b.id),
  );
}
export function taskActions(ticket: TaskTicket) {
  return {
    cancel:
      ticket.can_cancel ??
      ["queued", "working", "waiting"].includes(ticket.state),
    retry: ticket.can_retry ?? ticket.state === "failed",
    input:
      ticket.can_add_input ??
      ["queued", "working", "waiting", "completed", "failed"].includes(
        ticket.state,
      ),
  };
}
export function workerPayload(workers: WorkerDraft[]) {
  if (
    !workers.length ||
    workers.some((worker) => !worker.title.trim() || !worker.goal.trim())
  )
    throw new Error("请为每个 Worker 填写名称和工作目标。");
  return workers.map((worker) => ({
    title: worker.title.trim(),
    goal: worker.goal.trim(),
    role: worker.role,
    ...(worker.agent_id ? { agent_id: worker.agent_id } : {}),
  }));
}
export function validateRules(rules: TaskRule[]) {
  const ids = new Set<string>();
  for (const rule of rules) {
    if (!rule.id.trim() || ids.has(rule.id.trim()))
      throw new Error("规则标识不能为空或重复。");
    ids.add(rule.id.trim());
    if (!rule.contains.length || rule.contains.some((term) => !term.trim()))
      throw new Error("每条规则至少需要一个非空匹配词。");
  }
  return rules.map((rule) => ({
    id: rule.id.trim(),
    enabled: rule.enabled,
    contains: [...new Set(rule.contains.map((term) => term.trim()))],
  }));
}
