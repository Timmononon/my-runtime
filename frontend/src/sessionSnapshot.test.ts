import { describe, expect, it, vi } from "vitest";
import { readSessionSnapshot } from "./sessionSnapshot";
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
