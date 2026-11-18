import { describe, expect, it, vi } from "vitest";
import { readSessionSnapshot } from "./sessionSnapshot";
import { runRequiresReconciliation } from "./reconciliation";
import type { Run, SessionDetail } from "./types";

const run = (id: string, status: string, updated_at: number): Run => ({
  id,
  session_id: "s",
  status,
  created_at: 1,
  updated_at,
});
function detail(runs: Run[], answer = ""): SessionDetail {
  return {
    id: "s",
    title: "snapshot",
    cwd: "C:/fixture",
    agent_id: "default",
    project_id: null,
    updated_at: 1,
    plan: null,
    runs,
    messages: answer
      ? [
          {
            id: "m",
            seq: 1,
            run_id: runs[0].id,
            created_at: 1,
            message: {
              role: "assistant",
              content: answer,
              tool_calls: [],
              metadata: {},
            },
          },
        ]
      : [],
  };
}

describe("terminal transcript consistency", () => {
  it("re-reads live flags when reconciliation leaves historical run timestamps unchanged", async () => {
    const historical = {
      ...run("r", "needs_reconciliation", 1),
      requires_reconciliation: true,
    };
    const resolved = { ...historical, requires_reconciliation: false };
    const before = {
      ...detail([historical]),
      requires_reconciliation: true,
    };
    const after = { ...before, requires_reconciliation: false };
    const readDetail = vi
      .fn()
      .mockResolvedValueOnce(before)
      .mockResolvedValue(after);
    const readRun = vi
      .fn()
      .mockResolvedValueOnce(historical)
      .mockResolvedValue(resolved);
    const snapshot = await readSessionSnapshot(readDetail, readRun, () => true);
    expect(snapshot).toEqual({ detail: after, run: resolved });
    expect(readRun).toHaveBeenCalledTimes(2);
    expect(readDetail).toHaveBeenCalledTimes(3);
    expect(resolved.status).toBe("needs_reconciliation");
  });

  it("bounds a persistent mixed snapshot while preserving its true barrier for the next refresh", async () => {
    const pending = {
      ...run("r", "needs_reconciliation", 1),
      requires_reconciliation: true,
    };
    const resolved = { ...pending, requires_reconciliation: false };
    const session = { ...detail([pending]), requires_reconciliation: false };
    const readDetail = vi.fn(async () => session);
    const readRun = vi.fn(async () => pending);
    const snapshot = await readSessionSnapshot(readDetail, readRun, () => true);
    expect(snapshot).toEqual({ detail: session, run: pending });
    expect(readRun).toHaveBeenCalledTimes(3);
    expect(readDetail).toHaveBeenCalledTimes(4);
    expect(
      runRequiresReconciliation(
        pending,
        ["execution_reconciliation_status_v1"],
        session,
      ),
    ).toBe(true);
    expect(
      await readSessionSnapshot(
        readDetail,
        async () => resolved,
        () => true,
      ),
    ).toEqual({ detail: session, run: resolved });
  });

  it("keeps a genuine terminal unknown without accepting a false projection", async () => {
    const pending = {
      ...run("r", "needs_reconciliation", 1),
      requires_reconciliation: true,
    };
    const session = { ...detail([pending]), requires_reconciliation: true };
    const readRun = vi.fn(async () => pending);
    expect(
      await readSessionSnapshot(
        async () => session,
        readRun,
        () => true,
      ),
    ).toEqual({ detail: session, run: pending });
    expect(readRun).toHaveBeenCalledTimes(1);
    expect(
      runRequiresReconciliation(
        pending,
        ["execution_reconciliation_status_v1"],
        session,
      ),
    ).toBe(true);
  });

  it("keeps the final transcript when a run finishes between detail and run reads", async () => {
    const running = run("r", "running", 1),
      completed = run("r", "completed", 2);
    const before = detail([running]),
      after = detail([completed], "final answer");
    const readDetail = vi
      .fn()
      .mockResolvedValueOnce(before)
      .mockResolvedValueOnce(after);
    const snapshot = await readSessionSnapshot(
      readDetail,
      async () => completed,
      () => true,
    );
    expect(snapshot).toEqual({ detail: after, run: completed });
    expect(readDetail).toHaveBeenCalledTimes(2);
  });

  it("follows a queued successor instead of applying the old completed run", async () => {
    const previous = run("old", "completed", 2),
      queued = run("new", "queued", 3),
      active = run("new", "running", 4);
    const nextDetail = detail([queued, previous], "previous answer");
    const readDetail = vi
      .fn()
      .mockResolvedValueOnce(detail([run("old", "running", 1)]))
      .mockResolvedValueOnce(nextDetail);
    const readRun = vi
      .fn()
      .mockResolvedValueOnce(previous)
      .mockResolvedValueOnce(active);
    expect(await readSessionSnapshot(readDetail, readRun, () => true)).toEqual({
      detail: nextDetail,
      run: active,
    });
    expect(readRun.mock.calls).toEqual([["old"], ["new"]]);
  });

  it("discards a terminal refetch after navigation invalidates the request", async () => {
    const completed = run("r", "completed", 2);
    let valid = true;
    const readDetail = vi
      .fn()
      .mockResolvedValueOnce(detail([completed]))
      .mockImplementationOnce(async () => {
        valid = false;
        return detail([completed], "must not replace another chat");
      });
    expect(
      await readSessionSnapshot(
        readDetail,
        async () => completed,
        () => valid,
      ),
    ).toBeNull();
  });
});
