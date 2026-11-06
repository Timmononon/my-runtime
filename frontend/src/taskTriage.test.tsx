import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { TaskTriageDecisionCard, TaskTriageFields } from "./TaskTriageSettings";
import type { ModelProvider } from "./types";
import type { TaskTriageDecision, TaskTriageSettings } from "./taskTriageTypes";
import { triageDecisionLabel, triageSettingsPayload } from "./taskTriageTypes";

const settings: TaskTriageSettings = {
  revision: 3,
  enabled: true,
  provider_id: "deepseek",
  model: "deepseek-flash",
  reasoning_effort: "none",
  processing_scope: "仅处理本仓库缺陷修复",
  timeout_seconds: 30,
  max_input_chars: 16000,
  max_output_tokens: 768,
  max_requests_per_hour: 60,
  max_calls_per_poll: 4,
};
const providers: ModelProvider[] = [
  {
    id: "deepseek",
    name: "DeepSeek",
    base_url: "https://api.deepseek.com",
    protocol: "deepseek_responses",
    revision: 1,
    configured: true,
    models: [{ id: "deepseek-flash", effort_levels: ["none", "high"] }],
  },
];
const decision: TaskTriageDecision = {
  id: "triage-one",
  status: "completed",
  decision: "process",
  reason: "请求检查仓库中的缺陷",
  questions: [],
  provider_id: "deepseek",
  model: "deepseek-flash",
};

describe("independent task triage controls", () => {
  it("uses the separate provider/model selection and explains the disabled and read-only boundaries", () => {
    const html = renderToStaticMarkup(
      <TaskTriageFields
        value={{ ...settings, enabled: false }}
        providers={providers}
        disabled={false}
        onChange={() => {}}
      />,
    );
    expect(html).toContain('value="deepseek-flash" selected=""');
    expect(html).toContain('aria-label="判断模型提供商"');
    expect(html).toContain("关闭后暂停从 GitHub 创建新任务");
    expect(html).toContain("只读来源不调用判断模型");
    expect(html).toContain("保存和查看设置都不发送模型请求");
    expect(html).not.toContain('type="password"');
    expect(html).not.toContain('type="checkbox" checked');
  });
  it("saves explicit model settings with the reviewed revision and excludes credentials/other transient fields", () => {
    const payload = triageSettingsPayload({
      ...settings,
      api_key: "never-send",
      revision: 9,
    } as TaskTriageSettings);
    expect(payload.expected_revision).toBe(9);
    expect(payload.model).toBe("deepseek-flash");
    expect(payload.max_calls_per_poll).toBe(4);
    expect(payload).not.toHaveProperty("revision");
    expect(payload).not.toHaveProperty("api_key");
  });
  it("rejects invalid model selection and per-call/hour/poll budgets before saving", () => {
    for (const change of [
      { model: "" },
      { provider_id: "two words" },
      { timeout_seconds: 0 },
      { max_output_tokens: 4097 },
      { max_input_chars: 999 },
      { max_requests_per_hour: 1.5 },
      { max_calls_per_poll: 101 },
    ])
      expect(() => triageSettingsPayload({ ...settings, ...change })).toThrow();
  });
  it("shows binary results and errors separately and preserves legacy receipts", () => {
    const html = renderToStaticMarkup(
      <TaskTriageDecisionCard
        value={{ ...decision, reason: "请求检查缺陷 <script>run()</script>" }}
      />,
    );
    expect(html).toContain("模型判断：处理");
    expect(html).not.toContain("待补充");
    expect(html).toContain("deepseek-flash");
    expect(html).not.toContain("<script>");
    expect(
      triageDecisionLabel({
        ...decision,
        status: "failed",
        decision: null,
      }),
    ).toBe("判断失败");
    expect(triageDecisionLabel({ ...decision, decision: "ignore" })).toBe(
      "忽略",
    );
    expect(triageDecisionLabel({ ...decision, decision: "needs_info" })).toBe(
      "旧版判断（未启动任务）",
    );
  });
});
