export interface TaskTriageSettings {
  revision: number;
  enabled: boolean;
  provider_id: string;
  model: string;
  reasoning_effort: string;
  processing_scope: string;
  timeout_seconds: number;
  max_input_chars: number;
  max_output_tokens: number;
  max_requests_per_hour: number;
  max_calls_per_poll: number;
}

export interface TaskTriageDecision {
  id: string;
  status: "pending" | "completed" | "failed" | "interrupted";
  // needs_info is retained only for receipts written by the previous release.
  decision: "process" | "ignore" | "needs_info" | null;
  reason: string;
  questions: string[];
  provider_id: string;
  model: string;
  error_category?: string | null;
}

export function triageSettingsPayload(value: TaskTriageSettings) {
  const provider = value.provider_id.trim(),
    model = value.model.trim();
  if (!provider || !model || /\s/.test(provider + model))
    throw new Error("请选择判断模型的提供商和模型。");
  const limits = [
    ["判断超时", value.timeout_seconds, 1, 120],
    ["输入字符上限", value.max_input_chars, 1000, 200000],
    ["输出 Token 上限", value.max_output_tokens, 64, 4096],
    ["每小时判断次数", value.max_requests_per_hour, 1, 10000],
    ["每轮询判断次数", value.max_calls_per_poll, 1, 100],
  ] as const;
  for (const [label, number, min, max] of limits)
    if (!Number.isInteger(number) || number < min || number > max)
      throw new Error(`${label}需为 ${min} 至 ${max} 之间的整数。`);
  if (value.processing_scope.length > 8000)
    throw new Error("处理范围最多填写8000个字符。");
  return {
    expected_revision: value.revision,
    enabled: value.enabled,
    provider_id: provider,
    model,
    reasoning_effort: value.reasoning_effort,
    processing_scope: value.processing_scope,
    timeout_seconds: value.timeout_seconds,
    max_input_chars: value.max_input_chars,
    max_output_tokens: value.max_output_tokens,
    max_requests_per_hour: value.max_requests_per_hour,
    max_calls_per_poll: value.max_calls_per_poll,
  };
}

export function triageDecisionLabel(value: TaskTriageDecision) {
  if (value.status === "pending") return "判断中";
  if (value.status === "interrupted") return "判断已中断";
  if (value.status === "failed") return "判断失败";
  if (value.decision === "process") return "处理";
  if (value.decision === "ignore") return "忽略";
  if (value.decision === "needs_info") return "旧版判断（未启动任务）";
  return "判断不可用";
}
