import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { FeishuEventCard } from "./FeishuPanel";
import { feishuHealthLabel, feishuSourceError, feishuSourceFields } from "./feishuTypes";
import type { FeishuEvent } from "./feishuTypes";

const fields = { name: "", allowed_senders: "", required_keywords: "" };
const event: FeishuEvent = {
  seq: 4,
  message_id: "om_fixture",
  sender_id: "ou_fixture",
  received_at: 1700000000,
  disposition: "accepted",
  reason: null,
  text: '<img src="https://invalid.test"/>\n[链接](https://invalid.test)',
  triage_id: "triage",
  triage: {
    id: "triage",
    status: "completed",
    decision: "process",
    reason: "符合预设范围",
    questions: [],
    provider_id: "fixture-provider",
    model: "fixture-model",
  },
  ticket_id: "ticket",
};

describe("Feishu source controls", () => {
  it("keeps a disabled source editable and requires a complete OpenID for enabled admission", () => {
    expect(feishuSourceFields(fields, false).allowed_senders).toEqual([]);
    expect(() => feishuSourceFields(fields, true)).toThrow("至少一位");
    expect(() =>
      feishuSourceFields({ ...fields, allowed_senders: "@张三" }, false),
    ).toThrow("OpenID");
    expect(
      feishuSourceFields(
        {
          name: " 例会 ",
          allowed_senders: "ou_Alice\nou_Alice，ou_bob",
          required_keywords: "修复, 跟进\n安全\n安全",
        },
        true,
      ),
    ).toEqual({
      name: "例会",
      allowed_senders: ["ou_Alice", "ou_bob"],
      required_keywords: ["修复, 跟进", "安全"],
    });
  });

  it("shows a distinct setup, disconnected, and listening state", () => {
    expect(feishuHealthLabel("setup_required")).toBe("待检查飞书配置");
    expect(feishuSourceError("setup_required")).not.toContain("订阅");
    expect(feishuHealthLabel("disconnected")).toBe("连接已断开");
    expect(feishuHealthLabel("listening")).toBe("正在接收");
  });

  it("renders message content literally alongside the triage decision and ticket link", () => {
    const html = renderToStaticMarkup(
      <FeishuEventCard value={event} onOpenTicket={() => {}} />,
    );
    expect(html).toContain("&lt;img");
    expect(html).toContain("[链接](https://invalid.test)");
    expect(html).not.toContain("<img");
    expect(html).not.toContain("href=");
    expect(html).toContain("符合预设范围");
    expect(html).toContain("查看关联任务");

    const rejected = renderToStaticMarkup(
      <FeishuEventCard
        value={{
          ...event,
          disposition: "rejected",
          reason: "sender_not_allowed",
          text: null,
          triage: null,
          ticket_id: null,
        }}
        onOpenTicket={() => {}}
      />,
    );
    expect(rejected).toContain("发送人没有在此来源的允许名单中");
    expect(rejected).not.toContain("查看消息正文");
    expect(rejected).not.toContain("查看关联任务");
  });
});
