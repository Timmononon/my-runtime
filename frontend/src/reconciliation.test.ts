import { describe, expect, it } from "vitest";
import { runRequiresReconciliation } from "./reconciliation";
import type { Run, SessionDetail } from "./types";

const capabilities = ["execution_reconciliation_status_v1"];
const historical = (flag?: unknown): Run =>
  ({
    id: "historical-run",
    session_id: "session",
    status: "needs_reconciliation",
    requires_reconciliation: flag,
    created_at: 1,
    updated_at: 1,
  }) as Run;

describe("current reconciliation gate", () => {
  it("allows a new request after live reconciliation without rewriting historical status", () => {
    const run = Object.freeze(historical(false));
    expect(runRequiresReconciliation(run, capabilities)).toBe(false);
    expect(run.status).toBe("needs_reconciliation");
    expect(run.requires_reconciliation).toBe(false);
  });

  it("keeps an ancestor's live barrier even when this completed run has no executions", () => {
    const run: Run = {
      ...historical(true),
      status: "completed",
      tool_executions: [],
    };
    expect(runRequiresReconciliation(run, capabilities)).toBe(true);
  });

  it("protects an empty side chat using the session's inherited barrier", () => {
    expect(
      runRequiresReconciliation(null, capabilities, {
        requires_reconciliation: true,
      }),
    ).toBe(true);
    expect(
      runRequiresReconciliation(null, capabilities, {
        requires_reconciliation: false,
      }),
    ).toBe(false);
  });

  it.each([
    [true, false],
    [false, true],
  ])(
    "keeps either snapshot's true barrier during refresh races (%s, %s)",
    (runFlag, sessionFlag) => {
      expect(
        runRequiresReconciliation(
          { ...historical(runFlag), status: "completed" },
          capabilities,
          { requires_reconciliation: sessionFlag },
        ),
      ).toBe(true);
    },
  );

  it("accepts an authoritative session false when the run has no live flag", () => {
    expect(
      runRequiresReconciliation(historical(), capabilities, {
        requires_reconciliation: false,
      }),
    ).toBe(false);
  });

  it("does not let one false hide a malformed value from the other snapshot", () => {
    expect(
      runRequiresReconciliation(historical("false"), capabilities, {
        requires_reconciliation: false,
      }),
    ).toBe(true);
    expect(
      runRequiresReconciliation(historical(false), capabilities, {
        requires_reconciliation: null,
      } as unknown as SessionDetail),
    ).toBe(true);
  });

  it("retains an explicit true even if the other snapshot is malformed", () => {
    expect(
      runRequiresReconciliation(
        { ...historical("true"), status: "completed" },
        capabilities,
        { requires_reconciliation: true },
      ),
    ).toBe(true);
  });

  it.each([undefined, null, "false", 0, {}, []])(
    "preserves the historical barrier for malformed or absent flag %j",
    (flag) => {
      expect(runRequiresReconciliation(historical(flag), capabilities)).toBe(
        true,
      );
    },
  );

  it.each([undefined, [], ["execution_reconciliation_status_v2"]])(
    "does not clear the barrier without the exact capability %j",
    (available) => {
      expect(runRequiresReconciliation(historical(false), available)).toBe(
        true,
      );
    },
  );

  it("fails closed for malformed capability payloads", () => {
    for (const available of [
      "execution_reconciliation_status_v1",
      ["execution_reconciliation_status_v1", 1],
    ])
      expect(
        runRequiresReconciliation(
          historical(false),
          available as unknown as string[],
        ),
      ).toBe(true);
  });

  it("retains legacy terminal and new-conversation behavior", () => {
    expect(
      runRequiresReconciliation({ ...historical(true), status: "completed" }),
    ).toBe(false);
    expect(runRequiresReconciliation(null, capabilities)).toBe(false);
    expect(runRequiresReconciliation(undefined, capabilities)).toBe(false);
  });
});
