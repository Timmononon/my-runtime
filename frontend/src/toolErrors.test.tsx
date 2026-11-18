import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import Conversation from "./Conversation";
import { toolErrorSummary } from "./toolErrors";
import type { ChatMessage, MessageRecord } from "./types";

function result(
  category: string,
  content: string,
  extra: Record<string, unknown> = {},
): ChatMessage {
  return {
    role: "tool",
    content,
    tool_calls: [],
    tool_call_id: "failed-call",
    metadata: {
      is_error: true,
      error_category: category,
      outcome: "not_executed",
      ...extra,
    },
  };
}

function records(message: ChatMessage, tool = "github_read"): MessageRecord[] {
  return [
    {
      role: "assistant",
      content: "正在检查仓库。",
      metadata: { reasoning: "private reasoning marker" },
      tool_calls: [
        {
          id: "failed-call",
          name: tool,
          arguments: { token: "secret argument marker" },
        },
        { id: "success-call", name: "directory_list", arguments: {} },
        {
          id: "pending-call",
          name: "shell_exec",
          arguments: { script: "secret script marker" },
        },
      ],
    },
    message,
    {
      role: "tool",
      content: "private success output marker",
      tool_calls: [],
      tool_call_id: "success-call",
      metadata: {},
    },
  ].map((value, index) => ({
    id: String(index),
    seq: index + 1,
    run_id: "run",
    created_at: 0,
    message: value,
  }));
}

const feishuScopeData = {
  ok: false,
  identity: "user",
  error: {
    type: "authorization",
    subtype: "missing_scope",
    identity: "user",
    missing_scopes: ["im:message.send_as_user"],
    message: "private CLI output marker",
    hint: "https://auth.example.invalid/authorize?device_code=synthetic",
    human_recovery: "private recovery hint marker",
    text: "private user text marker",
  },
};

function feishuScopeMessage(extra: Record<string, unknown> = {}): ChatMessage {
  return result("authorization", JSON.stringify(feishuScopeData), {
    data: feishuScopeData,
    ...extra,
  });
}

const feishuBotData = {
  ok: false,
  identity: "bot",
  error: {
    type: "api",
    subtype: "unknown",
    code: 230002,
    log_id: "private log marker",
    message: "private remote message marker",
    hint: "private remote hint marker",
    chat_id: "private chat ID marker",
    text: "private message body marker",
  },
};

function feishuBotMessage({
  metadata = {},
  data = {},
  error = {},
}: {
  metadata?: Record<string, unknown>;
  data?: Record<string, unknown>;
  error?: Record<string, unknown>;
} = {}): ChatMessage {
  const payload = {
    ...feishuBotData,
    ...data,
    error: { ...feishuBotData.error, ...error },
  };
  return result("api", JSON.stringify(payload), { data: payload, ...metadata });
}

const feishuUserRejectionData = {
  ok: false,
  identity: "user",
  error: {
    type: "authorization",
    subtype: "user_unauthorized",
    code: 230027,
    identity: "user",
    log_id: "private send log marker",
    message: "private send message marker",
    hint: "private send hint marker",
    chat_id: "private send chat marker",
  },
};

describe("tool error explanations", () => {
  it.each(["not_executed", "known", "unknown"])(
    "renders a %s user 230027 result without upstream details",
    (outcome) => {
      const message = result(
        "authorization",
        JSON.stringify(feishuUserRejectionData),
        {
          outcome,
          data: feishuUserRejectionData,
        },
      );
      const html = renderToStaticMarkup(
        <Conversation
          records={records(message, "feishu_cli")}
          stream=""
          runId={null}
          busy={false}
          report={() => {}}
        />,
      );
      if (outcome === "unknown") {
        expect(html).toContain("结果待核验");
        expect(html).toContain("操作可能已经发生");
        expect(html).not.toContain("飞书拒绝本人发送（230027）");
        expect(html).not.toContain("本次消息未发送");
      } else {
        expect(html).toContain("飞书拒绝本人发送（230027）");
        expect(html).toContain(
          "本次消息未发送。请核对应用权限和目标会话的发送限制。",
        );
      }
      for (const marker of [
        "private send log marker",
        "private send message marker",
        "private send hint marker",
        "private send chat marker",
        "缺少飞书权限",
        "OAuth",
        "加群",
        "重试",
      ])
        expect(html).not.toContain(marker);
    },
  );

  const otherUserResults: {
    name: string;
    tool?: string;
    metadata?: Record<string, unknown>;
    data?: Record<string, unknown>;
    error?: Record<string, unknown>;
    expected?: string | null;
  }[] = [
    { name: "another tool", tool: "shell_exec" },
    { name: "another category", metadata: { error_category: "api" } },
    { name: "a missing outcome", metadata: { outcome: undefined } },
    { name: "a non-boolean error flag", metadata: { is_error: "true" } },
    {
      name: "a successful tool result",
      metadata: { is_error: false },
      expected: null,
    },
    { name: "raw output only", metadata: { data: undefined } },
    { name: "a successful envelope", data: { ok: true } },
    { name: "a bot envelope", data: { identity: "bot" } },
    { name: "another error type", error: { type: "api" } },
    { name: "another subtype", error: { subtype: "missing_scope" } },
    { name: "another code", error: { code: 230002 } },
    { name: "a string code", error: { code: "230027" } },
    { name: "a missing error identity", error: { identity: undefined } },
    { name: "a conflicting error identity", error: { identity: "bot" } },
    { name: "a missing log ID", error: { log_id: undefined } },
    { name: "a blank log ID", error: { log_id: " \t " } },
    { name: "a non-string log ID", error: { log_id: 501 } },
  ];
  it.each(otherUserResults)(
    "does not apply the 230027 explanation to $name",
    ({
      tool = "feishu_cli",
      metadata,
      data,
      error,
      expected = "工具调用未完成",
    }) => {
      const payload = {
        ...feishuUserRejectionData,
        ...data,
        error: { ...feishuUserRejectionData.error, ...error },
      };
      const message = result("authorization", JSON.stringify(payload), {
        data: payload,
        ...metadata,
      });
      expect(toolErrorSummary(message, tool)?.title ?? null).toBe(expected);
    },
  );

  it.each(["not_executed", "known"])(
    "explains a %s bot 230002 response using fixed text only",
    (outcome) => {
      const message = feishuBotMessage({ metadata: { outcome } });
      expect(toolErrorSummary(message, "feishu_cli")).toEqual({
        title: "当前机器人不在目标聊天中",
        detail:
          "个人身份能看到该聊天，不代表机器人可以发送。请先核实发送身份与目标聊天。",
      });
      const html = renderToStaticMarkup(
        <Conversation
          records={records(message, "feishu_cli")}
          stream=""
          runId={null}
          busy={false}
          report={() => {}}
        />,
      );
      expect(html).toContain("当前机器人不在目标聊天中");
      expect(html).toContain("个人身份能看到该聊天");
      for (const marker of [
        "private log marker",
        "private remote message",
        "private remote hint",
        "private chat ID",
        "private message body",
        "OAuth",
        "加群",
        "自动重试",
      ])
        expect(html).not.toContain(marker);
    },
  );

  it("accepts a matching optional bot error identity", () => {
    expect(
      toolErrorSummary(
        feishuBotMessage({ error: { identity: "bot" } }),
        "feishu_cli",
      )?.title,
    ).toBe("当前机器人不在目标聊天中");
  });

  it("keeps unknown outcomes ahead of the bot 230002 explanation", () => {
    const message = feishuBotMessage({ metadata: { outcome: "unknown" } });
    const html = renderToStaticMarkup(
      <Conversation
        records={records(message, "feishu_cli")}
        stream=""
        runId={null}
        busy={false}
        report={() => {}}
      />,
    );
    expect(html).toContain("结果待核验");
    expect(html).toContain("操作可能已经发生");
    expect(html).not.toContain("当前机器人不在目标聊天中");
    expect(html).not.toContain("private log marker");
    expect(html).not.toContain("private remote message");
  });

  const otherBotResults: {
    name: string;
    tool?: string;
    metadata?: Record<string, unknown>;
    data?: Record<string, unknown>;
    error?: Record<string, unknown>;
    expected?: string | null;
  }[] = [
    { name: "another code", error: { code: 230001 } },
    { name: "string code", error: { code: "230002" } },
    { name: "user identity", data: { identity: "user" } },
    { name: "missing envelope identity", data: { identity: undefined } },
    { name: "a successful envelope", data: { ok: true } },
    { name: "missing envelope status", data: { ok: undefined } },
    { name: "another error type", error: { type: "authorization" } },
    { name: "another subtype", error: { subtype: "missing_scope" } },
    { name: "missing code", error: { code: undefined } },
    { name: "missing log ID", error: { log_id: undefined } },
    { name: "blank log ID", error: { log_id: " \t " } },
    { name: "conflicting error identity", error: { identity: "user" } },
    { name: "another tool", tool: "shell_exec" },
    { name: "another category", metadata: { error_category: "authorization" } },
    { name: "missing outcome", metadata: { outcome: undefined } },
    { name: "non-boolean error flag", metadata: { is_error: "true" } },
    { name: "success", metadata: { is_error: false }, expected: null },
  ];
  it.each(otherBotResults)(
    "does not apply the bot-membership explanation to $name",
    ({
      tool = "feishu_cli",
      metadata,
      data,
      error,
      expected = "工具调用未完成",
    }) => {
      expect(
        toolErrorSummary(feishuBotMessage({ metadata, data, error }), tool)
          ?.title ?? null,
      ).toBe(expected);
    },
  );

  it.each(["not_executed", "known"])(
    "explains a %s user send scope failure with fixed safe text",
    (outcome) => {
      const message = feishuScopeMessage({ outcome });
      expect(toolErrorSummary(message, "feishu_cli")).toEqual({
        title: "缺少飞书权限",
        detail:
          "当前发送操作需要飞书“以本人身份发送消息”权限，请通过人工授权入口补充授权后再继续。",
      });
      const html = renderToStaticMarkup(
        <Conversation
          records={records(message, "feishu_cli")}
          stream=""
          runId={null}
          busy={false}
          report={() => {}}
        />,
      );
      expect(html).toContain("缺少飞书权限");
      expect(html).toContain("以本人身份发送消息");
      for (const marker of [
        "private CLI output",
        "auth.example.invalid",
        "synthetic",
        "private recovery hint",
        "private user text",
      ])
        expect(html).not.toContain(marker);
    },
  );

  it("keeps a Feishu unknown outcome ahead of its scope metadata", () => {
    const message = feishuScopeMessage({ outcome: "unknown" });
    const html = renderToStaticMarkup(
      <Conversation
        records={records(message, "feishu_cli")}
        stream=""
        runId={null}
        busy={false}
        report={() => {}}
      />,
    );
    expect(html).toContain("结果待核验");
    expect(html).toContain("操作可能已经发生");
    expect(html).not.toContain("缺少飞书权限");
    expect(html).not.toContain("auth.example.invalid");
    expect(html).not.toContain("private CLI output");
  });

  it.each([
    { name: "another tool", tool: "shell_exec", metadata: {} },
    {
      name: "another category",
      tool: "feishu_cli",
      metadata: { error_category: "authentication" },
    },
    {
      name: "another subtype",
      tool: "feishu_cli",
      metadata: {
        data: {
          ...feishuScopeData,
          error: { ...feishuScopeData.error, subtype: "access_denied" },
        },
      },
    },
    {
      name: "a scope prefix",
      tool: "feishu_cli",
      metadata: {
        data: {
          ...feishuScopeData,
          error: {
            ...feishuScopeData.error,
            missing_scopes: ["im:message.send_as_user_extra"],
          },
        },
      },
    },
    {
      name: "raw output without structured data",
      tool: "feishu_cli",
      metadata: { data: undefined },
    },
    {
      name: "bot identity",
      tool: "feishu_cli",
      metadata: {
        data: {
          ...feishuScopeData,
          identity: "bot",
          error: { ...feishuScopeData.error, identity: "bot" },
        },
      },
    },
  ])(
    "does not apply the user-send explanation to $name",
    ({ tool, metadata }) => {
      expect(toolErrorSummary(feishuScopeMessage(metadata), tool)?.title).toBe(
        "工具调用未完成",
      );
    },
  );

  it("shows a precise GitHub source recovery hint without raw arguments or payloads", () => {
    const message = result(
      "validation",
      "Provider payload sk-secret-secret-secret; Authorization: Bearer unknown-secret",
      {
        data: { reason: "source_not_found", secret: "private data marker" },
      },
    );
    const html = renderToStaticMarkup(
      <Conversation
        records={records(message)}
        stream=""
        runId="run"
        busy
        report={() => {}}
      />,
    );
    expect(html).toContain("github_read 错误原因");
    expect(html).toContain("仓库名和 Issue ID 不能代替来源 ID");
    expect(html).toContain("返回错误");
    expect(html).toContain("已返回");
    expect(html).toContain("等待结果");
    for (const privateText of [
      "sk-secret",
      "unknown-secret",
      "secret argument marker",
      "secret script marker",
      "private data marker",
      "private success output marker",
      "private reasoning marker",
    ])
      expect(html).not.toContain(privateText);
    expect(html.indexOf("github_read")).toBeLessThan(
      html.indexOf("directory_list"),
    );
    expect(html.indexOf("directory_list")).toBeLessThan(
      html.indexOf("shell_exec"),
    );
  });

  it("distinguishes missing approval, invalid parameters and missing local files", () => {
    expect(
      toolErrorSummary(
        result(
          "permission",
          "Approval is required but no attended approval channel is available",
        ),
        "shell_exec",
      )?.detail,
    ).toContain("没有可用的审批通道");
    expect(
      toolErrorSummary(
        result(
          "validation",
          "Invalid arguments: Additional properties are not allowed ('scope' was unexpected)",
        ),
        "shell_exec",
      )?.title,
    ).toBe("工具参数校验未通过");
    expect(
      toolErrorSummary(result("io", "[WinError 2] private path"), "file_read")
        ?.detail,
    ).toContain("文件或目录不存在");
  });

  it("explains the empty repository 404 without displaying the response body", () => {
    const error = toolErrorSummary(
      result(
        "http_status",
        'Source: https://api.github.com/repos/example/repo/contents/\nHTTP: 404\n\n{"message":"This repository is empty.","secret":"private payload"}',
      ),
      "web_fetch",
    );
    expect(error).toEqual({
      title: "远端返回 HTTP 404",
      detail: "GitHub 仓库尚无已提交内容，读取文件列表会返回 404。",
    });
    expect(JSON.stringify(error)).not.toContain("private payload");
    expect(
      toolErrorSummary(result("http_status", "HTTP: 999"), "web_fetch")?.title,
    ).toBe("远端请求未成功");
  });

  it("keeps unknown outcomes separate from ordinary errors and does not advise automatic retry", () => {
    const message = result("timeout", "private remote response", {
      outcome: "unknown",
    });
    const html = renderToStaticMarkup(
      <Conversation
        records={records(message)}
        stream=""
        runId={null}
        busy={false}
        report={() => {}}
      />,
    );
    expect(html).toContain("结果待核验");
    expect(html).toContain("操作可能已经发生，请先核验原执行记录");
    expect(html).not.toContain("private remote response");
    expect(html).not.toContain("返回错误");
  });

  it("only displays canonical process status and preserves success or pending rows", () => {
    expect(
      toolErrorSummary(
        result("process", "stdout: password=private-value", {
          data: { exit_code: 2 },
        }),
        "shell_exec",
      )?.title,
    ).toBe("命令执行失败（退出码 2）");
    expect(
      toolErrorSummary(
        result("configuration", "private configured endpoint"),
        "web_search",
      )?.detail,
    ).toContain("搜索服务是否启用");
    expect(
      toolErrorSummary(
        { ...result("io", "hidden"), metadata: {} },
        "file_read",
      ),
    ).toBeNull();
    expect(toolErrorSummary(undefined, "file_read")).toBeNull();
    for (const reason of ["source_unavailable", "source_ambiguous"])
      expect(
        toolErrorSummary(
          result("validation", "safe backend message", { data: { reason } }),
          "github_prepare",
        )?.title,
      ).toBe("GitHub 来源或任务绑定不可用");
  });
});
