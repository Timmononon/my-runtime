import type { TaskTriageDecision } from "./taskTriageTypes";

export type FeishuHealth =
  | "disabled"
  | "connecting"
  | "listening"
  | "setup_required"
  | "disconnected"
  | "identity_changed"
  | "group_unavailable";

export interface FeishuSource {
  id: string;
  revision: number;
  source_session_id: string;
  chat_id: string;
  name: string;
  enabled: boolean;
  allowed_senders: string[];
  required_keywords: string[];
  app_id: string;
  bot_open_id: string;
  bot_name: string | null;
  health: FeishuHealth;
  last_event_at: number | null;
  error: string | null;
  error_detail?: string;
}

export interface FeishuDiscovery {
  identity: {
    app_id: string;
    bot_open_id: string;
    bot_name: string | null;
    generation: string;
    user_open_id: string | null;
  };
  groups: { chat_id: string; name: string }[];
}

export interface FeishuEvent {
  seq: number;
  message_id: string;
  sender_id: string | null;
  received_at: number;
  disposition: string;
  reason: string | null;
  text: string | null;
  triage_id: string | null;
  triage: TaskTriageDecision | null;
  ticket_id: string | null;
}

export interface FeishuEventPage {
  events: FeishuEvent[];
  next_before: number | null;
}

export interface FeishuTaskBinding {
  source_id: string;
  chat_id: string;
  message_id: string;
  name: string;
  blocked: boolean;
  attention: string | null;
}

export interface FeishuSourceForm {
  name: string;
  allowed_senders: string;
  required_keywords: string;
}

export const FEISHU_TASK_SOURCES = "feishu_task_sources_v1";
const FEISHU_NAME_LIMIT = 100;
const OPEN_ID = /^ou_[A-Za-z0-9][A-Za-z0-9_-]{0,92}$/;

const entries = (value: string, separator: RegExp) =>
  [...new Set(value.split(separator).map((entry) => entry.trim()))].filter(
    Boolean,
  );
const length = (value: string) => Array.from(value).length;

export function feishuSourceFields(value: FeishuSourceForm, enabled: boolean) {
  const name = value.name.trim();
  const allowed_senders = entries(value.allowed_senders, /[\s,，]+/);
  const required_keywords = entries(value.required_keywords, /[\r\n]+/);
  if (length(name) > FEISHU_NAME_LIMIT)
    throw new Error(`来源名称最多 ${FEISHU_NAME_LIMIT} 个字符。`);
  if (allowed_senders.some((id) => !OPEN_ID.test(id)))
    throw new Error("请填写以 ou_ 开头的完整飞书用户 OpenID。");
  if (allowed_senders.length > 100)
    throw new Error("一个来源最多可添加 100 位发送人。");
  if (enabled && !allowed_senders.length)
    throw new Error("启用的来源需要至少一位允许的发送人。");
  if (
    required_keywords.length > 30 ||
    required_keywords.some((item) => length(item) > 120)
  )
    throw new Error("最多可添加 30 个关键词，每个不超过 120 个字符。");
  return { name, allowed_senders, required_keywords };
}

const healthLabels: Record<string, string> = {
  disabled: "已暂停",
  connecting: "正在连接",
  listening: "正在接收",
  setup_required: "待检查飞书配置",
  disconnected: "连接已断开",
  identity_changed: "机器人身份已变更",
  group_unavailable: "机器人已无法访问该群",
};
export const feishuHealthLabel = (health: string) =>
  Object.hasOwn(healthLabels, health) ? healthLabels[health] : "连接状态待确认";

const eventLabels: Record<string, string> = {
  pending: "等待处理",
  evaluating: "模型判断中",
  rejected: "未通过来源规则",
  triage_ignored: "模型判定忽略",
  triage_failed: "模型判断未完成",
  accepted: "已接入任务",
  interrupted: "处理已中断",
};
export const feishuEventLabel = (disposition: string) =>
  Object.hasOwn(eventLabels, disposition)
    ? eventLabels[disposition]
    : "接收结果待确认";

const eventReasons: Record<string, string> = {
  outside_activity_window: "消息不在本次接收时间范围内。",
  non_text: "此来源只接收文本消息。",
  bot_sender: "机器人发送的消息不进入任务判断。",
  sender_not_allowed: "发送人没有在此来源的允许名单中。",
  bot_not_mentioned: "消息没有提及此来源的机器人。",
  invalid_content: "消息正文无法接收。",
  keyword_not_matched: "消息未包含来源配置的关键词。",
  source_changed: "接收期间来源配置发生变化。",
  identity_changed: "接收期间机器人身份发生变化。",
  policy_changed: "接收期间任务规则发生变化。",
  capacity: "待处理消息已满。",
  triage_ignored: "判断模型未接入该消息。",
  triage_failed: "共用判断模型未完成本次判断。",
  interrupted: "处理已中断。",
  admission_failed: "运行时未创建任务。",
};
export const feishuEventReason = (reason: string) =>
  Object.hasOwn(eventReasons, reason) ? eventReasons[reason] : reason;

const sourceErrors: Record<string, string> = {
  identity_changed: "请核对本地飞书 CLI 的应用与机器人身份，再重新启用来源。",
  group_unavailable: "请确认机器人已加入来源群聊，再重新启用来源。",
  setup_required: "请重新读取机器人所在群聊，查看当前连接详情。",
  disconnected: "长连接暂时断开，服务将重连。",
  listener_stopped: "接收进程已停止，服务将重连。",
  listener_unavailable: "接收进程暂时不可用。",
  invalid_discovery: "服务未能确认机器人身份或所在群聊。",
};
export const feishuSourceError = (error: string) =>
  Object.hasOwn(sourceErrors, error) ? sourceErrors[error] : error;
