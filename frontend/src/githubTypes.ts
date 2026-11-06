import type { TaskTriageDecision } from "./taskTriageTypes";

export interface GithubSource {
  id: string;
  revision: number;
  source_session_id: string;
  repository: string;
  enabled: boolean;
  mode: "read_only" | "tasks";
  allow_writes: boolean;
  poll_seconds: number;
  allowed_actors: string[];
  allowed_bots: string[];
  bot_login: string | null;
  observe_issues: boolean;
  observe_pulls: boolean;
  observe_comments: boolean;
  observe_ci: boolean;
  configured: boolean;
  verified_login: string | null;
  health: "unchecked" | "healthy" | "error" | "rate_limited" | "disabled";
  last_poll_at: number | null;
  next_poll_at: number | null;
  error: string | null;
  identity_generation: number;
  event_count?: number;
  draft_count?: number;
}
export interface GithubEvent {
  id: string;
  seq: number;
  source_id: string;
  kind: string;
  number?: number | null;
  actor: string;
  actor_type: string;
  at: string | number;
  body: string;
  url: string;
  state?: string | null;
  baseline: boolean;
  disposition: string;
  ticket_id?: string | null;
  triage_id?: string | null;
  triage?: TaskTriageDecision | null;
}
export type GithubDraftState =
  "draft" | "sending" | "sent" | "outcome_unknown" | "cancelled" | "rejected";
export interface GithubDraft {
  id: string;
  revision: number;
  source_id: string;
  ticket_id: string | null;
  kind: "comment" | "pull_request";
  number: number | null;
  body: string;
  title: string | null;
  head: string | null;
  base: string | null;
  state: GithubDraftState;
  url: string | null;
  error: string | null;
  created_at: number;
  identity_generation: number;
}
export interface GithubTaskBinding {
  source_id: string;
  repository: string;
  numbers: number[];
  threads: { number: number; url: string; state: string }[];
  urls: string[];
  delivery_state: "waiting_review" | "merged" | "closed" | null;
}
export interface GithubCheck {
  source: GithubSource;
  repository: { full_name: string; private: boolean; default_branch: string };
  account: { login: string } | null;
  status: "ok";
}
export interface GithubPoll {
  source: GithubSource;
  read_only: boolean;
  requests: number;
  events: number;
  accepted: number;
  baseline: boolean;
  pending_pages: boolean | number;
}
export interface GithubSourceDraft {
  repository: string;
  enabled: boolean;
  mode: "read_only" | "tasks";
  allow_writes: boolean;
  poll_seconds: number;
  allowed_actors: string;
  allowed_bots: string;
  bot_login: string;
  observe_issues: boolean;
  observe_pulls: boolean;
  observe_comments: boolean;
  observe_ci: boolean;
}
export const newGithubSource = (): GithubSourceDraft => ({
  repository: "",
  enabled: false,
  mode: "read_only",
  allow_writes: false,
  poll_seconds: 300,
  allowed_actors: "",
  allowed_bots: "",
  bot_login: "",
  observe_issues: true,
  observe_pulls: true,
  observe_comments: true,
  observe_ci: false,
});
export function sourceEditor(source: GithubSource): GithubSourceDraft {
  return {
    ...source,
    allowed_actors: source.allowed_actors.join("\n"),
    allowed_bots: source.allowed_bots.join("\n"),
    bot_login: source.bot_login || "",
  };
}
export function accountList(value: string) {
  const seen = new Set<string>();
  return value
    .split(/[\n,，]/)
    .map((item) => item.trim().replace(/^@/, ""))
    .filter((item) => {
      if (!item) return false;
      if (/\s/.test(item) || item.length > 100)
        throw new Error("每行填写一个完整 GitHub 账号，不包含空格。");
      if (seen.has(item.toLowerCase())) return false;
      seen.add(item.toLowerCase());
      return true;
    });
}
export function sourcePayload(value: GithubSourceDraft) {
  const repository = value.repository.trim();
  if (
    !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository) ||
    [".", ".."].includes(repository.split("/")[1])
  )
    throw new Error("仓库请使用 owner/repo 格式，不要填写网址。");
  if (!Number.isInteger(value.poll_seconds) || value.poll_seconds < 30)
    throw new Error("轮询间隔至少为30秒。");
  return {
    repository,
    enabled: value.enabled,
    mode: value.mode,
    allow_writes: value.allow_writes,
    poll_seconds: value.poll_seconds,
    allowed_actors: accountList(value.allowed_actors),
    allowed_bots: accountList(value.allowed_bots),
    bot_login: value.bot_login.trim().replace(/^@/, "") || null,
    observe_issues: value.observe_issues,
    observe_pulls: value.observe_pulls,
    observe_comments: value.observe_comments,
    observe_ci: value.observe_ci,
  };
}
export function safeGithubUrl(
  value: string | null | undefined,
  repository: string,
) {
  if (!value) return null;
  try {
    const url = new URL(value);
    const parts = url.pathname.split("/").filter(Boolean);
    return url.protocol === "https:" &&
      url.hostname === "github.com" &&
      !url.port &&
      !url.username &&
      !url.password &&
      parts.slice(0, 2).join("/").toLowerCase() === repository.toLowerCase()
      ? url.href
      : null;
  } catch {
    return null;
  }
}
export function githubHealthLabel(source: GithubSource) {
  const labels: Record<string, string> = {
    unchecked: "尚未检查",
    healthy: "最近访问正常",
    error: "访问遇到问题",
    rate_limited: "等待访问额度恢复",
    disabled: "后台轮询已关闭",
  };
  return labels[source.health] || "状态待确认";
}
export function githubDraftLabel(state: string) {
  const labels: Record<string, string> = {
    draft: "待审阅",
    sending: "正在发送",
    sent: "已发送",
    outcome_unknown: "发送结果待核验",
    cancelled: "已取消",
    rejected: "未发送，请检查",
  };
  return labels[state] || "状态待确认";
}
export function githubDeliveryLabel(state: string | null | undefined) {
  const labels: Record<string, string> = {
    waiting_review: "等待 PR 审核",
    merged: "PR 已合并",
    closed: "PR 已关闭",
  };
  return state ? labels[state] || "交付状态待确认" : "尚未关联 PR 交付";
}
export function draftActions(draft: GithubDraft, source: GithubSource) {
  const editable = ["draft", "rejected"].includes(draft.state);
  const sameIdentity = draft.identity_generation === source.identity_generation;
  return {
    edit: editable,
    send:
      editable &&
      sameIdentity &&
      source.enabled &&
      source.mode === "tasks" &&
      source.allow_writes &&
      source.configured &&
      !!source.verified_login,
    reconcile: draft.state === "outcome_unknown",
  };
}
export function githubEventLabel(kind: string) {
  return (
    (
      {
        issue: "Issue",
        pull_request: "Pull request",
        pull: "Pull request",
        issue_comment: "讨论评论",
        comment: "讨论评论",
        review_comment: "代码行评论",
        review: "审核意见",
        workflow_run: "CI 运行",
        ci: "CI 运行",
      } as Record<string, string>
    )[kind] || "仓库事件"
  );
}
