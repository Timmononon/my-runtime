import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createReconciliationRefresh } from "./reconciliationRefresh";
import { runRequiresReconciliation } from "./reconciliation";
import { readSessionSnapshot } from "./sessionSnapshot";
import type { Run, SessionDetail } from "./types";

const capability = "execution_reconciliation_status_v1";
const cleanups: (() => void)[] = [];
beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  cleanups.splice(0).forEach((dispose) => dispose());
  vi.useRealTimers();
});

function client(pending = true, supported = true) {
  const server = { pending, capabilities: supported ? [capability] : [] };
  const run = (): Run => ({
    id: "historical",
    session_id: "session",
    status: "needs_reconciliation",
    created_at: 1,
    updated_at: 1,
    requires_reconciliation: server.pending,
  });
  const session = (): SessionDetail => ({
    id: "session",
    title: "temporary test",
    cwd: "C:/fixture",
    agent_id: "default",
    project_id: null,
    updated_at: 1,
    messages: [],
    runs: [run()],
    plan: null,
    requires_reconciliation: server.pending,
  });
  let currentRun: Run | null = run();
  let currentSession = session();
  let capabilities = [...server.capabilities];
  const readCapabilities = vi.fn(async () => [...server.capabilities]);
  const refresh = vi.fn(async () => {
    capabilities = await readCapabilities();
    const snapshot = await readSessionSnapshot(
      async () => session(),
      async () => run(),
      () => true,
    );
    if (!snapshot || "retry" in snapshot)
      throw new Error("Unexpected snapshot");
    currentRun = snapshot.run;
    currentSession = snapshot.detail;
  });
  return {
    server,
    refresh,
    readCapabilities,
    blocked: () =>
      runRequiresReconciliation(currentRun, capabilities, currentSession),
    historicalStatus: () => currentRun?.status,
  };
}

function reader(refresh: () => Promise<void>, options = {}) {
  const value = createReconciliationRefresh({
    refresh,
    isActive: () => true,
    keepPolling: true,
    onError: vi.fn(),
    ...options,
  });
  cleanups.push(value.dispose);
  return value;
}

describe("foreground reconciliation refresh", () => {
  it("unlocks after external terminal reconciliation without reloading or changing history", async () => {
    const state = client();
    const polling = reader(state.refresh);
    polling.wake();
    await vi.advanceTimersByTimeAsync(0);
    expect(state.blocked()).toBe(true);
    state.server.pending = false;
    await vi.advanceTimersByTimeAsync(1800);
    expect(state.blocked()).toBe(false);
    expect(state.historicalStatus()).toBe("needs_reconciliation");
    expect(state.refresh).toHaveBeenCalledTimes(2);
  });

  it("refreshes an old capability cache on focus and then accepts authoritative false", async () => {
    const state = client(false, false);
    const polling = reader(state.refresh, { keepPolling: false });
    expect(state.blocked()).toBe(true);
    polling.wake();
    await vi.advanceTimersByTimeAsync(0);
    expect(state.blocked()).toBe(true);
    state.server.capabilities = [capability];
    polling.wake();
    await vi.advanceTimersByTimeAsync(0);
    expect(state.readCapabilities).toHaveBeenCalledTimes(2);
    expect(state.blocked()).toBe(false);
  });

  it("keeps a genuine unknown blocked while bounding the read cadence", async () => {
    const state = client();
    reader(state.refresh).wake();
    await vi.advanceTimersByTimeAsync(60000);
    expect(state.blocked()).toBe(true);
    expect(state.refresh.mock.calls.length).toBeGreaterThan(1);
    expect(state.refresh.mock.calls.length).toBeLessThanOrEqual(10);
  });

  it("coalesces focus checks and pauses or disposes without starting further reads", async () => {
    let active = true;
    let complete!: () => void;
    const refresh = vi.fn(
      () => new Promise<void>((resolve) => (complete = resolve)),
    );
    const polling = reader(refresh, { isActive: () => active });
    polling.wake();
    polling.wake();
    polling.wake();
    await vi.advanceTimersByTimeAsync(0);
    expect(refresh).toHaveBeenCalledTimes(1);
    active = false;
    polling.pause();
    complete();
    await vi.advanceTimersByTimeAsync(30000);
    expect(refresh).toHaveBeenCalledTimes(1);
    active = true;
    polling.wake();
    await vi.advanceTimersByTimeAsync(0);
    expect(refresh).toHaveBeenCalledTimes(2);
    polling.dispose();
    complete();
    await vi.advanceTimersByTimeAsync(30000);
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it("retains the gate after a failed read and checks again with backoff", async () => {
    const state = client();
    const failure = new Error("temporary read failure");
    const onError = vi.fn();
    const refresh = vi
      .fn()
      .mockRejectedValueOnce(failure)
      .mockImplementation(state.refresh);
    reader(refresh, { onError }).wake();
    await vi.advanceTimersByTimeAsync(0);
    expect(onError).toHaveBeenCalledWith(failure);
    expect(state.blocked()).toBe(true);
    state.server.pending = false;
    await vi.advanceTimersByTimeAsync(1800);
    expect(state.blocked()).toBe(false);
  });
});
