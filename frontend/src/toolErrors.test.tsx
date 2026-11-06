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

function records(message: ChatMessage): MessageRecord[] {
  return [
    {
      role: "assistant",
      content: "正在检查仓库。",
      metadata: { reasoning: "private reasoning marker" },
      tool_calls: [
        {
          id: "failed-call",
          name: "github_read",
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

describe("tool error explanations", () => {
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
