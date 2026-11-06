import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { TaskStatus } from "./TaskBoard";
import type { TaskTicket, WorkerDraft } from "./taskTypes";
import {
  orderTickets,
  taskActions,
  taskLabel,
  taskNeedsAttention,
  validateRules,
  workerPayload,
} from "./taskTypes";

function ticket(id: string, changes: Partial<TaskTicket> = {}): TaskTicket {
  return {
    id,
    source_session_id: "source",
    parent_run_id: null,
    title: id,
    goal: "goal",
    state: "queued",
    revision: 1,
    created_at: 1,
    updated_at: 1,
    summary: "",
    attention: null,
    policy_revision: 1,
    workers: [],
    ...changes,
  };
}
describe("local task board", () => {
  it("keeps human attention before newer running or successful tasks without mutating polling snapshots", () => {
    const source = [
      ticket("done", { state: "completed", updated_at: 100 }),
      ticket("busy", { state: "working", updated_at: 120 }),
      ticket("uncertain", { state: "needs_reconciliation", updated_at: 20 }),
      ticket("question", { state: "waiting", updated_at: 30 }),
    ];
    expect(orderTickets(source).map((item) => item.id)).toEqual([
      "question",
      "uncertain",
      "busy",
      "done",
    ]);
    expect(source[0].id).toBe("done");
    expect(taskNeedsAttention(ticket("failed", { state: "failed" }))).toBe(
      true,
    );
    expect(taskNeedsAttention(ticket("done", { state: "completed" }))).toBe(
      false,
    );
  });
  it("never offers blind retry or followup for an unknown effect and honors runtime action flags", () => {
    expect(
      taskActions(ticket("uncertain", { state: "needs_reconciliation" })),
    ).toEqual({ cancel: false, retry: false, input: false });
    expect(taskActions(ticket("stopped", { state: "cancelled" })).input).toBe(
      false,
    );
    expect(
      taskActions(ticket("known", { state: "failed", can_retry: false })).retry,
    ).toBe(false);
    expect(
      taskActions(ticket("other", { state: "queued", can_cancel: false }))
        .cancel,
    ).toBe(false);
  });
  it("distinguishes execution, approvals, reconciliation and successful completion in readable status", () => {
    expect(taskLabel("working")).toBe("执行中");
    expect(taskLabel("waiting_approval")).toBe("等待批准");
    expect(taskLabel("completed")).toBe("已完成");
    expect(taskLabel("unknown_state")).toBe("状态待确认");
    const html = renderToStaticMarkup(
      <TaskStatus state="needs_reconciliation" />,
    );
    expect(html).toContain("结果待核验");
    expect(html).not.toContain("已完成");
  });
  it("requires separate work goals and sends roles and explicit agent choices without authority fields", () => {
    const drafts: WorkerDraft[] = [
      { title: " 读源码 ", goal: " 检查文件 ", role: "reader", agent_id: "" },
      {
        title: "实现",
        goal: "实现分配模块",
        role: "worker",
        agent_id: "coder",
      },
    ];
    expect(workerPayload(drafts)).toEqual([
      { title: "读源码", goal: "检查文件", role: "reader" },
      {
        title: "实现",
        goal: "实现分配模块",
        role: "worker",
        agent_id: "coder",
      },
    ]);
    expect(() => workerPayload([{ ...drafts[0], goal: " " }])).toThrow(
      "工作目标",
    );
    expect(() => workerPayload([])).toThrow();
  });
  it("rejects duplicate or empty policy identities and match-all blank rules before a revisioned save", () => {
    expect(() =>
      validateRules([{ id: "a", enabled: true, contains: [" "] }]),
    ).toThrow("匹配词");
    expect(() =>
      validateRules([{ id: "a", enabled: false, contains: [] }]),
    ).toThrow("匹配词");
    expect(() =>
      validateRules([
        { id: "a", enabled: true, contains: ["x"] },
        { id: " a ", enabled: true, contains: ["y"] },
      ]),
    ).toThrow("重复");
    expect(
      validateRules([
        { id: " local ", enabled: true, contains: [" 文件 ", "文件", "报告"] },
      ]),
    ).toEqual([{ id: "local", enabled: true, contains: ["文件", "报告"] }]);
  });
});
